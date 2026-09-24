"""Invariants behind features/review_workflows.feature that the scenarios do not pin."""
import os
from pathlib import Path
import tempfile
import unittest

from boring_agent.manager import Manager
from boring_agent.model import Conflict, Invalid
from boring_agent.process import identity
from boring_agent.runner import run_attempt
from boring_agent.store import Store
from boring_agent.workflows import WorkflowStore


SESSION = "@session:sess_" + "c" * 24


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.store = Store(root / "state")
        self.store.initialize(self.workspace, max_active=4)
        self.store.register_worker("worker", ["demo"], 4)
        self.manager = Manager(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def demo_root(self, key="root", **workflow):
        return self.store.create_workflow({
            "objective": "Plan bounded work", "runtime": "demo",
            "demo": {"delay_seconds": 0, "result": {"children": []}},
            "workflow": {"enabled": True, "max_attempts": 10, **workflow}}, key)

    def llm_root(self, coddy, key="llm-root"):
        return self.store.create_workflow({"objective": "Plan bounded work", "runtime": "llm", "coddy": coddy,
                                           "workflow": {"enabled": True}}, key)

    @staticmethod
    def child(child_id, **task):
        return {"id": child_id, "task": {"objective": "child " + child_id, "runtime": "demo",
                                         "demo": {"delay_seconds": 0}, **task}}

    def settle(self, receipt, *children, **options):
        return self.store.settle_workflow_plan(receipt["workflow_id"], {"children": list(children)}, **options)

    # --- F2: authority allowlist -------------------------------------------------

    def test_llm_escalations_are_refused_for_their_authority_reason(self):
        mention = {"agent": "exec", "prompt": "do it", "permission_mode": "ask"}
        receipt = self.llm_root({"session": SESSION, "permission_mode": "accept_edits", "mention": mention})
        for label, coddy, reason in (
            ("new session", {"session": "@session:sess_" + "d" * 24}, "coddy session"),
            ("bypass", {"permission_mode": "bypass"}, "permission mode"),
            ("other agent", {"mention": {"agent": "other"}}, "coddy.mention.agent"),
            ("mention model", {"mention": {"model": "planner-model"}}, "coddy.mention.model"),
            ("mention bypass", {"mention": {"permission_mode": "bypass"}}, "mention permission mode"),
        ):
            with self.subTest(label):
                result = self.settle(receipt, {"id": "c", "task": {"objective": "x", "runtime": "llm", "coddy": coddy}})
                self.assertEqual(result["state"], "rejected")
                self.assertIn(reason, result["reason"])
        self.assertEqual(self.store.workflow_children(receipt["workflow_id"]), [])

    def test_llm_child_may_narrow_and_inherits_root_coddy(self):
        mention = {"agent": "exec", "prompt": "do it", "permission_mode": "accept_edits"}
        receipt = self.llm_root({"session": SESSION, "permission_mode": "accept_edits", "mention": mention})
        result = self.settle(receipt,
                             {"id": "inherit", "task": {"objective": "x", "runtime": "llm"}},
                             {"id": "narrow", "task": {"objective": "y", "runtime": "llm",
                                                       "coddy": {"mention": {"prompt": "narrower", "permission_mode": "ask"}}}})
        self.assertEqual(result["state"], "accepted", result)
        inherit, narrow = (self.store.task(c["task_id"])["spec"] for c in self.store.workflow_children(receipt["workflow_id"]))
        self.assertEqual((inherit["coddy"]["session"], inherit["coddy"]["mention"]["agent"]), (SESSION, "exec"))
        self.assertEqual((narrow["coddy"]["mention"]["prompt"], narrow["coddy"]["mention"]["permission_mode"]),
                         ("narrower", "ask"))

    def test_unset_root_permission_only_allows_ask(self):
        receipt = self.llm_root({"session": SESSION})
        widened = self.settle(receipt, {"id": "c", "task": {"objective": "x", "runtime": "llm",
                                                             "coddy": {"permission_mode": "accept_edits"}}})
        self.assertEqual(widened["state"], "rejected")
        narrowed = self.settle(receipt, {"id": "c", "task": {"objective": "x", "runtime": "llm",
                                                              "coddy": {"permission_mode": "ask"}}})
        self.assertEqual(narrowed["state"], "accepted", narrowed)

    def test_schema_version_is_not_a_plan_field(self):
        result = self.settle(self.demo_root(), self.child("c", schema_version=1))
        self.assertEqual(result["state"], "rejected")
        self.assertIn("schema_version", result["reason"])

    # --- F7: workflow token allocation and claiming ------------------------------

    def test_explicit_null_ceiling_under_a_bounded_workflow_is_refused(self):
        result = self.settle(self.demo_root(max_tokens=100), self.child("c", budget={"max_tokens": None}))
        self.assertEqual(result["state"], "rejected")
        self.assertIn("workflow token ceiling", result["reason"])

    def test_implicit_share_is_what_explicit_siblings_leave(self):
        receipt = self.demo_root(max_tokens=100)
        result = self.settle(receipt, self.child("big", budget={"max_tokens": 60}), self.child("a"), self.child("b"))
        self.assertEqual(result["state"], "accepted", result)
        ceilings = [self.store.task(c["task_id"])["spec"]["budget"]["max_tokens"]
                    for c in self.store.workflow_children(receipt["workflow_id"])]
        self.assertEqual(ceilings, [60, 20, 20])

    def test_no_share_left_rejects_a_child_without_a_ceiling(self):
        result = self.settle(self.demo_root(max_tokens=100), self.child("big", budget={"max_tokens": 100}), self.child("a"))
        self.assertEqual(result["state"], "rejected")

    def test_replan_budget_keeps_the_allocation_of_a_running_child(self):
        receipt = self.demo_root(max_tokens=100)
        self.assertEqual(self.settle(receipt, self.child("slow", budget={"max_tokens": 80}))["state"], "accepted")
        self.launch(self.store.workflow_children(receipt["workflow_id"])[0]["task_id"])
        over = self.store.replan_workflow(receipt["workflow_id"], {"children": [self.child("next", budget={"max_tokens": 30})]})
        self.assertEqual(over["state"], "rejected", over)
        fits = self.store.replan_workflow(receipt["workflow_id"], {"children": [self.child("next", budget={"max_tokens": 20})]})
        self.assertEqual(fits["state"], "accepted", fits)

    def test_failed_workflow_admits_no_further_child_attempts(self):
        receipt = self.demo_root()
        self.settle(receipt, self.child("c"))
        with self.store.transaction() as db:
            db.execute("UPDATE workflow_roots SET state='failed',reason='operator_stopped' WHERE id=?",
                       (receipt["workflow_id"],))
        self.manager.tick()
        task = self.store.task(self.store.workflow_children(receipt["workflow_id"])[0]["task_id"])
        self.assertEqual((task["status"], task["reason"], task["attempts"]), ("Failed", "operator_stopped", []))

    # --- F9: plan command idempotency ----------------------------------------------

    def test_key_from_another_command_or_workflow_conflicts(self):
        first, second = self.demo_root("first"), self.demo_root("second")
        self.settle(first, self.child("a"), key="plan-key")
        with self.assertRaises(Conflict):
            self.settle(second, self.child("a"), key="plan-key")
        with self.assertRaises(Conflict):
            self.settle(second, self.child("a"), key="first")  # the first workflow's create key
        self.store.submit({"objective": "plain", "runtime": "demo"}, "plain-key")
        with self.assertRaises(Conflict):
            self.settle(second, self.child("a"), key="plain-key")
        self.assertEqual(self.store.workflow_children(second["workflow_id"]), [])

    def test_empty_key_is_invalid_and_rejected_receipts_replay(self):
        receipt = self.demo_root()
        with self.assertRaises(Invalid):
            self.settle(receipt, self.child("a"), key="")
        cycle = [{**self.child("x"), "dependencies": ["y"]}, {**self.child("y"), "dependencies": ["x"]}]
        rejected = self.settle(receipt, *cycle, key="bad-plan")
        again = self.settle(receipt, *cycle, key="bad-plan")
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(again, {**rejected, "duplicate": True})
        self.assertEqual(len(self.store.workflow(receipt["workflow_id"])["plans"]), 1)

    def test_planner_finishing_after_an_api_plan_does_not_add_a_revision(self):
        plan = {"children": [self.child("from-planner")]}
        receipt = self.store.create_workflow({"objective": "Plan", "runtime": "demo",
                                              "demo": {"delay_seconds": 0, "result": plan},
                                              "workflow": {"enabled": True, "max_attempts": 10}}, "late-planner")
        self.assertEqual(self.settle(receipt, self.child("from-api"), key="api-plan")["state"], "accepted")
        self.manager.tick()
        planner = self.store.task(receipt["task_id"])
        run_attempt(self.store, planner["current_attempt_id"], "worker")
        self.manager.tick()
        self.assertEqual(self.store.task(receipt["task_id"])["status"], "Succeeded")
        root = self.store.workflow(receipt["workflow_id"])
        self.assertEqual((root["plan_revision"], [c["child_key"] for c in root["children"]]), (1, ["from-api"]))

    # --- F7b: replan and launched children -------------------------------------------

    def launch(self, task_id, state="Launching"):
        self.manager.tick()
        attempt = self.store.task(task_id)["attempts"][-1]
        self.store.observe(attempt["id"], attempt["worker_id"], 1, "Launching",
                           runner_pid=os.getpid(), runner_start=identity(os.getpid()))
        if state == "Unknown":
            self.store.observe(attempt["id"], attempt["worker_id"], 2, "Unknown")
        return attempt

    def test_replan_keeps_an_unknown_child_reserved(self):
        receipt = self.demo_root()
        self.settle(receipt, self.child("slow"))
        task_id = self.store.workflow_children(receipt["workflow_id"])[0]["task_id"]
        attempt = self.launch(task_id, "Unknown")
        self.assertEqual(self.store.replan_workflow(receipt["workflow_id"], {"children": [self.child("new")]})["state"],
                         "accepted")
        task = self.store.task(task_id)
        self.assertEqual((task["status"], task["desired_action"]), ("Scheduled", "Cancel"))
        self.assertEqual(task["attempts"][-1]["reserved"], 1)
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["attempts"][-1]["id"], attempt["id"])
        self.assertEqual(self.store.task(task_id)["attempts"][-1]["reserved"], 1)

    def test_replan_cancels_a_queued_child_and_releases_its_slot(self):
        receipt = self.demo_root()
        self.settle(receipt, self.child("queued"))
        task_id = self.store.workflow_children(receipt["workflow_id"])[0]["task_id"]
        self.manager.tick()  # also schedules the planner task
        used = self.store.capacity()["used"]
        self.assertEqual(self.store.task(task_id)["attempts"][-1]["state"], "Queued")
        self.store.replan_workflow(receipt["workflow_id"], {"children": [self.child("new")]})
        task = self.store.task(task_id)
        self.assertEqual((task["status"], task["attempts"][-1]["state"], task["attempts"][-1]["reserved"]),
                         ("Cancelled", "Cancelled", 0))
        self.assertEqual(self.store.capacity()["used"], used - 1)

    # --- F8: schema setup inside a transaction -------------------------------------

    def test_schema_setup_does_not_end_the_callers_transaction(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                db.execute("INSERT INTO settings(key,value) VALUES('unit_marker','1')")
                WorkflowStore._ensure_schema_locked(db)
                self.assertTrue(db.in_transaction)
                raise RuntimeError("injected")
        with self.store.reading() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM settings WHERE key='unit_marker'").fetchone())


if __name__ == "__main__":
    unittest.main()
