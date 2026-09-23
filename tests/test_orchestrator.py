import concurrent.futures
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import boring_agent.manager as manager_module
from boring_agent.artifacts import read_result
from boring_agent.manager import Manager
from boring_agent.model import Conflict, Invalid, NotFound, StorageError, strict_json
from boring_agent.process import identity, lock
from boring_agent.runner import run_attempt
from boring_agent.store import Store
from boring_agent.worker import Worker
from boring_agent.workspace import Workspace


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = Store(self.root / "state")
        self.store.initialize(self.workspace, max_active=2)
        self.manager = Manager(self.store)
        self.store.register_worker("w", ["demo"], 2)
        self.ids = []

    def tearDown(self):
        # Any detached test runners receive cancel before fixture files disappear.
        for task_id in self.ids:
            task = self.store.task(task_id)
            if task["status"] not in ("Succeeded", "Failed", "Cancelled"):
                self.store.cancel(task_id, "cleanup-" + task_id)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            self.manager.tick()
            active = [a for task_id in self.ids for a in self.store.task(task_id)["attempts"]
                      if a["state"] in ("Running", "Launching")]
            if not active:
                break
            time.sleep(.02)
        self.temp.cleanup()

    def submit(self, key=None, **changes):
        spec = {"objective": "Test a durable task", "runtime": "demo", "demo": {"delay_seconds": 0}, **changes}
        receipt = self.store.submit(spec, key or str(len(self.ids)))
        self.ids.append(receipt["task_id"])
        return receipt["task_id"]

    def execute_attempt(self, task_id):
        self.manager.tick()
        task = self.store.task(task_id)
        attempt = task["attempts"][-1]
        run_attempt(self.store, attempt["id"], attempt["worker_id"])
        self.manager.tick()
        return self.store.task(task_id)

    def test_lost_ack_retry_returns_original_receipt_across_restart(self):
        raw = {"objective": "same", "runtime": "demo"}
        first = self.store.submit(raw, "stable")
        second = Store(self.store.home).submit(raw, "stable")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["command_id"], second["command_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.store.tasks()), 1)
        with self.assertRaises(Conflict):
            self.store.submit({**raw, "objective": "changed"}, "stable")

    def test_bad_input_never_accepted(self):
        for changes in ({"objective": ""}, {"schema_version": True}, {"tools": ["shell"]},
                        {"workspace": "/tmp"}, {"status": "Succeeded"},
                        {"budget": {"max_attempts": 0}}, {"retry": {"max_attempts": 0}},
                        {"output_schema": {"$ref": "https://example.com/schema"}},
                        {"sandbox": "workspace-write"}, {"expect_files": "reports/a.md"},
                        {"expect_files": ["../escape.md"]}, {"expect_files": ["/etc/passwd"]},
                        {"expect_files": [".hidden/a.md"]}, {"expect_files": [""]}):
            with self.subTest(changes=changes), self.assertRaises(Invalid):
                self.submit(**changes)
        self.assertEqual(self.store.tasks(), [])
        for value in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(Invalid):
                strict_json(value)

    def test_acceptance_and_dispatch_transactions_roll_back(self):
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_intake BEFORE INSERT ON commands BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Conflict):
            self.submit()
        self.assertEqual(self.store.tasks(), [])
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER fail_intake")
        task_id = self.submit()
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_dispatch BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Conflict):
            self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Pending")
        self.assertEqual(self.store.capacity()["used"], 0)
        self.assertEqual(self.store.task(task_id)["attempts"], [])
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER fail_dispatch")

    def test_manager_restart_recovers_intake_and_reserved_dispatch(self):
        task_id = self.submit()
        Manager(Store(self.store.home)).tick()
        original = self.store.task(task_id)["current_attempt_id"]
        Manager(Store(self.store.home)).tick()
        self.assertEqual(self.store.task(task_id)["current_attempt_id"], original)
        self.assertEqual(self.execute_attempt(task_id)["status"], "Succeeded")

    def test_repeated_delivery_executes_one_logical_attempt(self):
        task_id = self.submit()
        self.manager.tick()
        attempt = self.store.task(task_id)["attempts"][0]
        run_attempt(self.store, attempt["id"], "w")
        run_attempt(self.store, attempt["id"], "w")
        self.manager.tick()
        events = self.store.events(task_id)
        self.assertEqual(sum(e["kind"] == "attempt.claimed" for e in events), 1)
        self.assertEqual(self.store.task(task_id)["status"], "Succeeded")

    def test_cancel_before_dispatch(self):
        task_id = self.submit()
        first = self.store.cancel(task_id, "cancel")
        second = self.store.cancel(task_id, "cancel")
        self.assertEqual(first["command_id"], second["command_id"])
        self.manager.tick()
        task = self.store.task(task_id)
        self.assertEqual(task["status"], "Cancelled")
        self.assertEqual(task["attempts"], [])

    def test_cancel_after_reservation_prevents_execution(self):
        task_id = self.submit()
        self.manager.tick()
        self.store.cancel(task_id, "stop")
        task = self.execute_attempt(task_id)
        self.assertEqual(task["status"], "Cancelled")
        self.assertIsNone(task["attempts"][0]["started_at"])
        self.assertEqual(self.store.capacity()["used"], 0)

    def test_queued_cancellation_does_not_need_a_live_worker(self):
        task_id = self.submit()
        self.manager.tick()
        with self.store.transaction() as db:
            db.execute("UPDATE workers SET last_seen=0")
        self.store.cancel(task_id, "stop")
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Cancelled")
        self.assertEqual(self.store.capacity()["used"], 0)

    def test_changed_worker_capabilities_prevent_reserved_launch(self):
        task_id = self.submit()
        self.manager.tick()
        self.store.register_worker("w", ["llm"], 2)
        self.assertEqual(self.execute_attempt(task_id)["status"], "Failed")
        self.assertEqual(self.store.capacity()["used"], 0)

    def test_cancel_committed_before_merge_blocks_late_success(self):
        task_id = self.submit()
        self.manager.tick()
        attempt = self.store.task(task_id)["attempts"][0]
        run_attempt(self.store, attempt["id"], "w")
        self.store.cancel(task_id, "stop")
        self.manager.tick()
        task = self.store.task(task_id)
        self.assertEqual(task["status"], "Cancelled")
        self.assertEqual(task["attempts"][0]["state"], "Succeeded")
        self.assertIsNone(task["result_path"])
        self.assertIsNotNone(task["attempts"][0]["result_path"])

    def test_success_committed_first_is_terminal(self):
        task_id = self.submit()
        self.execute_attempt(task_id)
        self.store.cancel(task_id, "late")
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Succeeded")

    def test_retry_requires_replay_safety_and_terminal_failure(self):
        task_id = self.submit(demo={"delay_seconds": 0, "fail_attempts": 1},
                              retry={"max_attempts": 2, "replay_safe": True, "backoff_seconds": .01})
        first = self.execute_attempt(task_id)
        self.assertEqual(first["status"], "Pending")
        old_id = first["attempts"][0]["id"]
        time.sleep(.015)
        second = self.execute_attempt(task_id)
        self.assertEqual(second["status"], "Succeeded")
        self.assertEqual(len(second["attempts"]), 2)
        self.assertFalse(self.store.observe(old_id, "w", 999, "Running"))
        unsafe = self.submit(demo={"delay_seconds": 0, "fail_attempts": 1}, retry={"max_attempts": 2})
        self.assertEqual(self.execute_attempt(unsafe)["status"], "Failed")

    def test_permanent_failure_never_retries(self):
        task_id = self.submit(demo={"fail_attempts": 3, "failure_kind": "permanent", "delay_seconds": 0},
                              retry={"max_attempts": 3, "replay_safe": True})
        self.assertEqual(self.execute_attempt(task_id)["status"], "Failed")
        self.assertEqual(len(self.store.task(task_id)["attempts"]), 1)

    def test_output_validation_and_checksum(self):
        bad = self.submit(demo={"delay_seconds": 0, "result": {"answer": "wrong type"}},
                          output_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer"}}})
        self.assertEqual(self.execute_attempt(bad)["status"], "Failed")
        good = self.submit()
        task = self.execute_attempt(good)
        artifact = self.store.home / task["result_path"]
        artifact.write_text('{"tampered":true}')
        with self.assertRaisesRegex(Invalid, "checksum"):
            read_result(self.store, task["attempts"][0], task["spec"])

    def _succeeded_attempt_awaiting_settlement(self):
        task_id = self.submit()
        self.manager.tick()
        attempt = self.store.task(task_id)["attempts"][0]
        run_attempt(self.store, attempt["id"], "w")
        return task_id

    def test_artifact_validation_releases_writer_for_submit_and_heartbeat(self):
        task_id = self._succeeded_attempt_awaiting_settlement()
        entered = threading.Event()
        release = threading.Event()
        original = manager_module.prevalidate_result

        def blocked(*args, **kwargs):
            verdict = original(*args, **kwargs)
            entered.set()
            self.assertTrue(release.wait(2))
            return verdict

        with patch.object(manager_module, "prevalidate_result", blocked):
            thread = threading.Thread(target=self.manager.tick)
            thread.start()
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            submitted = Store(self.store.home).submit({"objective": "concurrent", "runtime": "demo"}, "concurrent")
            Store(self.store.home).heartbeat_worker("w")
            self.assertLess(time.monotonic() - started, 2)
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.task(task_id)["status"], "Succeeded")
        self.ids.append(submitted["task_id"])

    def test_stale_prevalidated_verdict_never_accepts_changed_identity(self):
        mutations = ("artifact", "path", "checksum", "spec", "intent")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                task_id = self._succeeded_attempt_awaiting_settlement()
                entered = threading.Event()
                release = threading.Event()
                original = manager_module.prevalidate_result

                def blocked(*args, **kwargs):
                    verdict = original(*args, **kwargs)
                    entered.set()
                    self.assertTrue(release.wait(2))
                    return verdict

                with patch.object(manager_module, "prevalidate_result", blocked):
                    thread = threading.Thread(target=self.manager.tick)
                    thread.start()
                    self.assertTrue(entered.wait(2))
                    task = self.store.task(task_id)
                    attempt = task["attempts"][0]
                    artifact = self.store.home / attempt["result_path"]
                    if mutation == "artifact":
                        artifact.write_text('{"changed":true}')
                    elif mutation == "path":
                        with self.store.transaction() as db:
                            db.execute("UPDATE attempts SET result_path='artifacts/changed.json' WHERE id=?",
                                       (attempt["id"],))
                    elif mutation == "checksum":
                        with self.store.transaction() as db:
                            db.execute("UPDATE attempts SET result_sha256='changed' WHERE id=?",
                                       (attempt["id"],))
                    elif mutation == "spec":
                        changed = dict(task["spec"])
                        changed["objective"] = "changed after validation"
                        with self.store.transaction() as db:
                            db.execute("UPDATE tasks SET spec=? WHERE id=?",
                                       (json.dumps(changed, sort_keys=True, separators=(",", ":")), task_id))
                    else:
                        self.store.cancel(task_id, "stale-intent-" + task_id)
                    release.set()
                    thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                final = self.store.task(task_id)
                self.assertNotEqual(final["status"], "Succeeded" if mutation != "intent" else "Succeeded")
                if mutation == "intent":
                    self.assertEqual(final["status"], "Cancelled")
                else:
                    self.assertEqual(final["status"], "Failed")

    def test_unresolvable_schema_is_rejected_without_crashing_manager(self):
        task_id = self.submit(output_schema={"$ref": "#/$defs/missing"})
        self.assertEqual(self.execute_attempt(task_id)["status"], "Failed")

    def test_observations_reject_wrong_owner_sequence_and_regression(self):
        task_id = self.submit()
        self.manager.tick()
        aid = self.store.task(task_id)["current_attempt_id"]
        self.assertFalse(self.store.observe(aid, "imposter", 1, "Launching"))
        self.assertTrue(self.store.observe(aid, "w", 1, "Launching", runner_pid=os.getpid(), runner_start=identity(os.getpid())))
        self.assertFalse(self.store.observe(aid, "w", 1, "Running"))
        self.assertTrue(self.store.observe(aid, "w", 2, "Running"))
        with self.assertRaises(Conflict):
            self.store.observe(aid, "w", 3, "Launching")
        self.store.observe(aid, "w", 4, "Cancelled", tokens=0)

    def test_unknown_keeps_reservation_across_restart_until_operator_resolution(self):
        task_id = self.submit(retry={"max_attempts": 3, "replay_safe": True})
        self.manager.tick()
        aid = self.store.task(task_id)["current_attempt_id"]
        self.store.observe(aid, "w", 1, "Launching", runner_pid=2147483647, runner_start="missing")
        Manager(Store(self.store.home)).tick()
        task = self.store.task(task_id)
        self.assertEqual(task["observation_condition"], "Unknown")
        self.assertEqual(self.store.capacity()["used"], 1)
        self.store.cancel(task_id, "cancel")
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Scheduled")
        with self.assertRaises(Invalid):
            self.store.resolve(aid, "not verified")
        self.store.resolve(aid, "Test fixture proves no process was started", True)
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Cancelled")
        self.assertEqual(self.store.capacity()["used"], 0)

    def test_shared_last_slot_is_not_double_reserved(self):
        with self.store.transaction() as db:
            db.execute("UPDATE settings SET value='1' WHERE key='max_active'")
        self.store.register_worker("other", ["demo"], 3)
        for _ in range(8):
            self.submit()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: Manager(Store(self.store.home)).tick(), range(8)))
        self.assertEqual(self.store.capacity()["used"], 1)
        self.assertEqual(sum(t["status"] == "Scheduled" for t in self.store.tasks()), 1)

    def test_stale_worker_cannot_receive_work(self):
        task_id = self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE workers SET last_seen=0")
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Pending")
        self.assertFalse(self.store.capacity()["workers"][0]["valid"])

    def test_deadline_before_dispatch_and_attempt_timeout(self):
        task_id = self.submit(budget={"deadline_seconds": .1})
        time.sleep(.11)
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Failed")
        running = self.submit(demo={"delay_seconds": .4}, budget={"attempt_seconds": .1})
        self.assertEqual(self.execute_attempt(running)["status"], "Failed")
        self.assertEqual(self.store.capacity()["used"], 0)

    def test_known_and_unknown_usage_separately_block_retry(self):
        for tokens in (None, 10):
            task_id = self.submit(budget={"max_tokens": 10}, retry={"max_attempts": 2, "replay_safe": True})
            self.manager.tick()
            aid = self.store.task(task_id)["current_attempt_id"]
            self.store.observe(aid, "w", 1, "Launching")
            self.store.observe(aid, "w", 2, "Failed", error_kind="transient", tokens=tokens)
            self.manager.tick()
            task = self.store.task(task_id)
            self.assertEqual(task["status"], "Failed")
            self.assertEqual(bool(task["usage_unknown"]), tokens is None)

    def test_decreasing_usage_cannot_refund_a_consumed_retry_budget(self):
        # features/retry_budgets.feature: a higher observation sequence cannot make
        # cumulative usage smaller; conflicting totals are unknown, not free tokens.
        task_id = self.submit(budget={"max_tokens": 10},
                              retry={"max_attempts": 2, "replay_safe": True})
        self.manager.tick()
        aid = self.store.task(task_id)["current_attempt_id"]
        self.store.observe(aid, "w", 1, "Launching")
        self.store.observe(aid, "w", 2, "Running", tokens=10)
        self.store.observe(aid, "w", 3, "Failed", error_kind="transient", tokens=1)
        self.manager.tick()
        task = self.store.task(task_id)
        self.assertEqual(task["status"], "Failed")
        self.assertEqual(task["tokens_used"], 10)
        self.assertTrue(task["usage_unknown"])
        self.assertIsNone(task["attempts"][0]["tokens"])
        self.assertIn("attempt.usage_inconsistent", [e["kind"] for e in self.store.events(task_id)])

    def test_missing_usage_report_keeps_the_known_lower_bound(self):
        task_id = self.submit()
        self.manager.tick()
        aid = self.store.task(task_id)["current_attempt_id"]
        self.store.observe(aid, "w", 1, "Launching")
        self.store.observe(aid, "w", 2, "Running", tokens=10)
        self.store.observe(aid, "w", 3, "Failed", error_kind="permanent", tokens=None, known_tokens=1)
        self.manager.tick()
        task = self.store.task(task_id)
        self.assertEqual(task["tokens_used"], 10)
        self.assertTrue(task["usage_unknown"])

    def test_worker_exit_does_not_kill_detached_execution(self):
        task_id = self.submit(demo={"delay_seconds": .3})
        self.manager.tick()
        worker = Worker(self.store, "w", ["demo"])
        worker.tick()
        children = list(worker.children.values())
        del worker
        end = time.monotonic() + 5
        while time.monotonic() < end:
            Manager(Store(self.store.home)).tick()
            if self.store.task(task_id)["status"] == "Succeeded":
                break
            time.sleep(.02)
        self.assertEqual(self.store.task(task_id)["status"], "Succeeded")
        for child in children:
            child.wait(timeout=2)

    def test_crash_after_claim_before_runtime_start_is_not_replayed(self):
        task_id = self.submit()
        self.manager.tick()
        aid = self.store.task(task_id)["current_attempt_id"]
        script = ("import os; from unittest.mock import patch; from boring_agent.runner import run_attempt; "
                  "from boring_agent.store import Store; "
                  f"p=patch('boring_agent.runner.Controller.checkpoint',side_effect=lambda:os._exit(17)); p.start(); "
                  f"run_attempt(Store({str(self.store.home)!r}),{aid!r},'w')")
        child = subprocess.run([sys.executable, "-c", script], timeout=5)
        self.assertEqual(child.returncode, 17)
        self.manager.tick()
        run_attempt(self.store, aid, "w")
        self.assertEqual(self.store.task(task_id)["observation_condition"], "Unknown")
        self.assertEqual(self.store.capacity()["used"], 1)
        self.store.resolve(aid, "Injected pre-launch exit; no runtime was started", True)

    def test_crash_after_artifact_before_observation_does_not_infer_success(self):
        task_id = self.submit()
        self.manager.tick()
        aid = self.store.task(task_id)["current_attempt_id"]
        script = "\n".join([
            "import os", "from unittest.mock import patch", "from boring_agent import runner",
            "from boring_agent.store import Store", "original = runner.publish",
            "def crashed(*args):", "    original(*args)", "    os._exit(18)",
            "with patch.object(runner, 'publish', crashed):",
            f"    runner.run_attempt(Store({str(self.store.home)!r}),{aid!r},'w')",
        ])
        child = subprocess.run([sys.executable, "-c", script], timeout=5)
        self.assertEqual(child.returncode, 18)
        self.manager.tick()
        self.assertTrue((self.store.home / "artifacts" / f"{aid}.json").exists())
        self.assertEqual(self.store.task(task_id)["observation_condition"], "Unknown")
        self.assertIsNone(self.store.task(task_id)["result_path"])
        self.store.resolve(aid, "Offline fixture exited after writing its artifact", True)

    def test_read_only_database_write_is_storage_error_and_has_no_receipt(self):
        original = self.store.connect
        def read_only():
            db = original()
            db.execute("PRAGMA query_only=ON")
            return db
        with patch.object(self.store, "connect", read_only), self.assertRaises(StorageError):
            self.submit()
        self.assertEqual(self.store.tasks(), [])

    def test_storage_errors_are_distinct(self):
        with self.assertRaises(NotFound):
            Store(self.root / "absent").settings()
        with self.assertRaises(NotFound):
            self.store.task("missing")
        with self.assertRaises(Conflict):
            self.store.initialize(self.workspace)
        with self.store.transaction() as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaises(StorageError):
            self.store.settings()
        # An unsupported future schema is intentionally rejected before Store
        # can return a managed connection; reset the fixture through SQLite
        # directly so teardown can continue on the supported version.
        db = sqlite3.connect(self.store.path)
        db.execute("PRAGMA user_version=1")
        db.close()

    def test_retention_keeps_submit_tombstone_after_task_deletion(self):
        raw = {"objective": "Test a durable task", "runtime": "demo", "demo": {"delay_seconds": 0}}
        receipt = self.store.submit(raw, "retention-unit")["task_id"]
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='Succeeded', finished_at=0 WHERE id=?", (receipt,))
        result = self.store.retain(now=time.time())
        self.assertEqual(result["deleted"], [receipt])
        duplicate = self.store.submit(raw, "retention-unit")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["task_id"], receipt)
        self.assertEqual(self.store.tasks(), [])

    def test_future_schema_rejection_does_not_mutate_database(self):
        with self.store.transaction() as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaises(StorageError):
            self.store.settings()
        db = sqlite3.connect(self.store.path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 999)
        db.execute("PRAGMA user_version=2")
        db.commit()
        db.close()

    def test_concurrent_openers_complete_one_durable_migration(self):
        with self.store.transaction() as db:
            db.execute("PRAGMA user_version=1")

        def open_store(_):
            opened = Store(self.store.home)
            opened.settings()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(open_store, range(2)))
        with self.store.reading() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE outcome='migrated'").fetchone()[0], 1)

    def test_single_manager_lock(self):
        with lock(self.store.home, "manager"):
            with self.assertRaises(Conflict):
                with lock(self.store.home, "manager"):
                    pass

    def test_worker_lost_before_launch_requeues_without_consuming_an_attempt(self):
        task_id = self.submit()  # default policy: one attempt, no replay safety
        self.manager.tick()
        first = self.store.task(task_id)["current_attempt_id"]
        with self.store.transaction() as db:
            db.execute("UPDATE workers SET last_seen=0")
        self.manager.tick()
        task = self.store.task(task_id)
        self.assertEqual(task["status"], "Pending")
        self.assertEqual((task["attempts"][0]["state"], task["attempts"][0]["error_kind"]), ("Cancelled", "not_started"))
        self.assertEqual(self.store.capacity()["used"], 0)
        self.assertIn("task.requeued", [e["kind"] for e in self.store.events(task_id)])
        self.store.register_worker("replacement", ["demo"], 1)
        task = self.execute_attempt(task_id)
        self.assertEqual(task["status"], "Succeeded")
        self.assertNotEqual(task["attempts"][-1]["id"], first)
        self.assertEqual(task["attempts"][-1]["worker_id"], "replacement")
        # The demo failure counter sees the replacement as the first execution.
        again = self.submit(demo={"delay_seconds": 0, "fail_attempts": 1}, retry={"max_attempts": 2, "replay_safe": True})
        self.manager.tick()
        with self.store.transaction() as db:
            db.execute("UPDATE workers SET last_seen=0 WHERE id='replacement'")
            db.execute("UPDATE workers SET last_seen=? WHERE id='w'", (time.time(),))
        self.manager.tick()
        self.assertEqual(self.execute_attempt(again)["status"], "Pending")  # execution 1 failed, retry scheduled
        time.sleep(1.1)
        self.assertEqual(self.execute_attempt(again)["status"], "Succeeded")
        self.assertEqual(len(self.store.task(again)["attempts"]), 3)

    def test_queued_attempt_is_waiting_not_stale(self):
        task_id = self.submit()
        self.manager.tick()
        with self.store.transaction() as db:
            db.execute("UPDATE attempts SET heartbeat=heartbeat-60")
        self.assertEqual(self.store.task(task_id)["observation_condition"], "Fresh")
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["observation_condition"], "Fresh")
        aid = self.store.task(task_id)["current_attempt_id"]
        self.store.observe(aid, "w", 1, "Launching", runner_pid=os.getpid(), runner_start=identity(os.getpid()))
        with self.store.transaction() as db:
            db.execute("UPDATE attempts SET heartbeat=heartbeat-60")
        self.assertEqual(self.store.task(task_id)["observation_condition"], "Stale")
        self.store.observe(aid, "w", 2, "Failed", error_kind="permanent", error_message="fixture end", tokens=0)
        self.manager.tick()

    def test_cancel_of_pending_task_records_finish(self):
        task_id = self.submit()
        self.store.cancel(task_id, "cancel")
        kinds = [e["kind"] for e in self.store.events(task_id)]
        self.assertEqual(kinds[-2:], ["task.cancel_requested", "task.finished"])
        self.assertEqual(self.store.task(task_id)["observation_condition"], "Fresh")

    def test_runner_without_process_identity_fails_closed(self):
        task_id = self.submit()
        self.manager.tick()
        aid = self.store.task(task_id)["current_attempt_id"]
        with patch("boring_agent.runner.identity", return_value=None):
            run_attempt(self.store, aid, "w")
        attempt = self.store.task(task_id)["attempts"][0]
        self.assertEqual((attempt["state"], attempt["runner_start"]), ("Failed", None))
        self.assertIn("attempt.rejected", [e["kind"] for e in self.store.events(task_id)])
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Failed")
        self.assertEqual(self.store.capacity()["used"], 0)

    def test_worker_backs_off_relaunching_a_runner_that_exits_before_claiming(self):
        task_id = self.submit()
        self.manager.tick()
        aid = self.store.task(task_id)["current_attempt_id"]

        class Exited:
            def poll(self):
                return 1

        spawns = []
        with patch("boring_agent.worker.subprocess.Popen", side_effect=lambda *a, **k: spawns.append(k) or Exited()):
            worker = Worker(self.store, "w", ["demo"], 2)
            for _ in range(5):
                worker.tick()
            self.assertEqual(len(spawns), 1)
            worker.launches[aid] = (worker.launches[aid][0], 0.0)  # backoff elapsed
            worker.tick()
            self.assertEqual(len(spawns), 2)
        log = self.store.home / "logs" / f"{aid}.log"
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
        self.assertEqual(log.read_text().count("launch"), 2)
        self.assertIn(spawns[0]["stdout"], (spawns[0]["stderr"],))

    def test_failed_initialize_leaves_no_half_written_store(self):
        home = self.root / "broken"
        with patch.object(Store, "connect", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(StorageError):
                Store(home).initialize(self.workspace)
        self.assertFalse((home / "agent.db").exists())
        Store(home).initialize(self.workspace)
        self.assertTrue((home / "agent.db").exists())
        self.assertTrue((home / "logs").is_dir())

    def test_reads_do_not_wait_for_a_writer(self):
        task_id = self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE settings SET value='1' WHERE key='observation_ttl'")  # hold the write lock
            started = time.monotonic()
            self.assertEqual(self.store.task(task_id)["status"], "Pending")
            self.assertEqual(len(self.store.tasks()), 1)
            self.assertEqual(self.store.capacity()["used"], 0)
            self.assertLess(time.monotonic() - started, 5)


class WorkspaceTests(unittest.TestCase):
    def test_boundaries_limits_and_explicit_write_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "safe.txt").write_text("hello")
            (root / ".env").write_text("secret")
            (root / "alias").symlink_to("/etc")
            (root / "large").write_bytes(b"x" * 65537)
            tools = Workspace(root, ["list_files", "read_file"], root / "state")
            self.assertEqual(tools.call({"action": "read_file", "path": "safe.txt"}), {"content": "hello"})
            for path in ("../etc/passwd", "/etc/passwd", ".env", "alias/passwd", "large"):
                with self.subTest(path=path), self.assertRaises(Invalid):
                    tools.call({"action": "read_file", "path": path})
            with self.assertRaises(Invalid):
                tools.call({"action": "write_file", "path": "safe.txt", "content": "changed"})
            writable = Workspace(root, ["write_file"], root / "state")
            writable.call({"action": "write_file", "path": "safe.txt", "content": "changed"})
            self.assertEqual((root / "safe.txt").read_text(), "changed")
            # Missing parents are created inside the workspace; escapes and hidden parents are not.
            writable.call({"action": "write_file", "path": "new/deep/dir/schema.json", "content": "{}"})
            self.assertEqual((root / "new/deep/dir/schema.json").read_text(), "{}")
            for path in ("../outside/x.json", ".git/config", "alias/x.json"):
                with self.subTest(path=path), self.assertRaises(Invalid):
                    writable.call({"action": "write_file", "path": path, "content": "x"})
            self.assertFalse((root.parent / "outside").exists())
            self.assertEqual(writable.missing(["safe.txt", "new/deep/dir/schema.json", "absent.md", "../x"]),
                             ["absent.md", "../x"])


if __name__ == "__main__":
    unittest.main()
