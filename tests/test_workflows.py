import json
from pathlib import Path
import tempfile
import unittest

from boring_agent.manager import Manager
from boring_agent.model import Conflict, Invalid
from boring_agent.runner import run_attempt
from boring_agent.store import Store


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.store = Store(root / "state")
        self.store.initialize(self.workspace, max_active=4)
        self.store.register_worker("planner", ["demo"], 4)

    def tearDown(self):
        self.temp.cleanup()

    def root(self, *, result=None, **workflow):
        return {
            "objective": "Plan bounded work",
            "runtime": "demo",
            "demo": {"delay_seconds": 0, "result": result or {"children": []}},
            "workflow": {"enabled": True, **workflow},
        }

    @staticmethod
    def plan(*children):
        return {"children": list(children)}

    def create(self, result=None, key="root", **workflow):
        return self.store.create_workflow(self.root(result=result, **workflow), key)

    def test_planner_and_children_are_separate_ordinary_lifecycles(self):
        plan = self.plan({"id": "one", "order": 0,
                          "task": {"objective": "Execute one", "runtime": "demo",
                                    "demo": {"delay_seconds": 0}}})
        receipt = self.create(result=plan)
        manager = Manager(self.store)
        manager.tick()
        planner = self.store.task(receipt["task_id"])
        self.assertEqual(planner["status"], "Scheduled")
        run_attempt(self.store, planner["current_attempt_id"], "planner")
        manager.tick()
        self.assertEqual(self.store.task(receipt["task_id"])["status"], "Succeeded")
        root = self.store.workflow(receipt["workflow_id"])
        self.assertEqual((root["state"], root["plan_revision"], len(root["children"])),
                         ("executing", 1, 1))
        self.assertEqual(self.store.task(root["children"][0]["task_id"])["status"], "Pending")
        self.assertNotEqual(root["planner_task_id"], root["children"][0]["task_id"])

    def test_caller_key_is_independent_from_internal_child_identity(self):
        receipt = self.create(key="visible-child-label")
        accepted = self.store.settle_workflow_plan(
            receipt["workflow_id"], self.plan({"id": "visible-child-label"}))
        self.assertEqual(self.store.create_workflow(self.root(), "visible-child-label-2")["duplicate"], False)
        child = self.store.workflow_children(receipt["workflow_id"])[0]
        self.assertTrue(child["internal_id"].startswith("workflow:"))
        self.assertNotEqual(child["internal_id"], "visible-child-label")
        self.assertEqual(len(accepted["children"]), 1)

    def test_invalid_graph_plan_creates_no_children(self):
        for bad in (
            self.plan({"id": "a", "dependencies": ["b"]}, {"id": "b", "dependencies": ["a"]}),
            self.plan({"id": "a", "dependencies": ["missing"]}),
            {"children": [{"id": "a", "order": 0}, {"id": "b", "order": 0}]},
            {"children": []},
        ):
            with self.subTest(plan=bad):
                receipt = self.create(key="bad-" + str(len(self.store.workflows())))
                result = self.store.settle_workflow_plan(receipt["workflow_id"], bad)
                self.assertEqual(result["state"], "rejected")
                self.assertEqual(self.store.workflow_children(receipt["workflow_id"]), [])

    def test_child_expansion_rolls_back_when_storage_fails(self):
        receipt = self.create()
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER workflow_insert_failure BEFORE INSERT ON workflow_children "
                       "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(Conflict):
            self.store.settle_workflow_plan(receipt["workflow_id"], self.plan({"id": "a"}))
        with self.store.reading() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM workflow_plans").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM workflow_children").fetchone()[0], 0)
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER workflow_insert_failure")

    def test_authority_can_only_narrow(self):
        receipt = self.create()
        result = self.store.settle_workflow_plan(receipt["workflow_id"], self.plan({
            "id": "write", "task": {"sandbox": "workspace-write", "tools": ["write_file"]}}))
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(self.store.workflow_children(receipt["workflow_id"]), [])

    def test_dependency_delivery_is_explicit_and_bounded(self):
        (self.workspace / "facts.txt").write_text("verified facts", encoding="utf-8")
        receipt = self.create()
        result = self.store.settle_workflow_plan(receipt["workflow_id"], self.plan(
            {"id": "source", "deliver": {"files": ["facts.txt"]}},
            {"id": "consumer", "dependencies": ["source"]},
        ))
        self.assertEqual(result["state"], "accepted")
        children = self.store.workflow_children(receipt["workflow_id"])
        source, consumer = children
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='Succeeded',result_path=NULL WHERE id=?", (source["task_id"],))
        delivery = self.store.deliver_workflow_dependencies(consumer["task_id"])
        self.assertEqual(delivery["state"], "ready")
        self.assertGreater(delivery["bytes"], 0)
        self.assertEqual(self.store.workflow_children(receipt["workflow_id"])[1]["context_bytes"], delivery["bytes"])

    def test_missing_declared_dependency_output_blocks_consumer(self):
        receipt = self.create()
        self.store.settle_workflow_plan(receipt["workflow_id"], self.plan(
            {"id": "source", "deliver": {"result": True}}, {"id": "consumer", "dependencies": ["source"]}))
        source, consumer = self.store.workflow_children(receipt["workflow_id"])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='Succeeded' WHERE id=?", (source["task_id"],))
        result = self.store.deliver_workflow_dependencies(consumer["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.store.task(consumer["task_id"])["status"], "Failed")
        self.assertTrue(self.store.workflow( receipt["workflow_id"])["reason"])

    def test_child_cannot_drop_or_stretch_root_budget(self):
        root = self.root(max_tokens=5000)
        root["budget"] = {"max_tokens": 5000, "deadline_seconds": 60}
        receipt = self.store.create_workflow(root, "ceiling")
        for label, budget in (("null ceiling", {"max_tokens": None}), ("longer deadline", {"deadline_seconds": 3600}),
                              ("more tokens", {"max_tokens": 6000})):
            with self.subTest(label=label):
                result = self.store.settle_workflow_plan(receipt["workflow_id"], self.plan(
                    {"id": "child", "task": {"budget": budget}}))
                self.assertEqual(result["state"], "rejected", result)
        accepted = self.store.settle_workflow_plan(receipt["workflow_id"], self.plan(
            {"id": "child", "task": {"budget": {"deadline_seconds": 30}}}))
        self.assertEqual(accepted["state"], "accepted")
        child = self.store.workflow_children(receipt["workflow_id"])[0]
        spec = self.store.task(child["task_id"])["spec"]
        self.assertEqual((spec["budget"]["max_tokens"], spec["budget"]["deadline_seconds"]), (5000, 30))

    def test_replan_gives_a_kept_pending_child_a_fresh_task(self):
        receipt = self.create(max_attempts=10)
        self.store.settle_workflow_plan(receipt["workflow_id"], self.plan({"id": "done"}, {"id": "keep"}))
        done, keep = self.store.workflow_children(receipt["workflow_id"])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='Succeeded' WHERE id=?", (done["task_id"],))
        result = self.store.replan_workflow(receipt["workflow_id"], self.plan({"id": "done"}, {"id": "keep"}))
        self.assertEqual(result["state"], "accepted")
        second = {c["child_key"]: c for c in self.store.workflow_children(receipt["workflow_id"], revision=2)}
        self.assertEqual(second["done"]["task_id"], done["task_id"])
        self.assertNotEqual(second["keep"]["task_id"], keep["task_id"])
        self.assertEqual(self.store.task(second["keep"]["task_id"])["status"], "Pending")
        self.assertEqual(self.store.task(keep["task_id"])["status"], "Cancelled")

    def test_key_used_by_a_plain_submission_conflicts_instead_of_crashing(self):
        raw = self.root()
        self.store.submit(raw, "shared-key")
        with self.assertRaises(Conflict):
            self.store.create_workflow(raw, "shared-key")

    def test_replan_carries_success_and_cancels_obsolete_pending_child(self):
        receipt = self.create(max_attempts=10)
        self.store.settle_workflow_plan(receipt["workflow_id"], self.plan({"id": "done"}, {"id": "obsolete"}))
        done, obsolete = self.store.workflow_children(receipt["workflow_id"])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='Succeeded' WHERE id=?", (done["task_id"],))
        result = self.store.replan_workflow(receipt["workflow_id"], self.plan(
            {"id": "done"}, {"id": "replacement"}))
        self.assertEqual((result["state"], result["revision"]), ("accepted", 2))
        self.assertEqual(self.store.task(obsolete["task_id"])["status"], "Cancelled")
        revisions = self.store.workflow_children(receipt["workflow_id"])
        carried = [child for child in revisions if child["revision"] == 2 and child["child_key"] == "done"][0]
        self.assertEqual(carried["task_id"], done["task_id"])
        self.assertEqual(carried["carried_from_task_id"], done["task_id"])


if __name__ == "__main__":
    unittest.main()
