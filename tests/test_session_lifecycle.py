import sqlite3
from pathlib import Path
import tempfile
import unittest

from boring_agent.model import Conflict, Invalid, SessionConflict
from boring_agent.session_lifecycle import (
    DEFAULT_WARMUP_MODEL,
    SessionLifecycle,
)
from boring_agent.store import Store


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        self.store = Store(root / "state")
        self.store.initialize(workspace)
        self.lifecycle = SessionLifecycle(self.store)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def job(job_id, objective="work", dependencies=None, **extra):
        return {
            "id": job_id,
            "objective": objective,
            "runtime": "coddy_native",
            "model": "codex/gpt-5.6-luna",
            "dependencies": dependencies or [],
            **extra,
        }

    def warm(self, session_id, calls=None):
        calls = calls if calls is not None else []

        def execute(command, model, received_session, key):
            calls.append((command, model, received_session, key))
            return {"ok": True}

        self.lifecycle.warm_session(session_id, execute)
        return calls

    def test_native_job_requires_explicit_model_and_exact_session_mention(self):
        with self.assertRaises(Invalid):
            self.lifecycle.register_job({"id": "bad", "objective": "x", "runtime": "coddy_native"})
        with self.assertRaises(Invalid):
            self.lifecycle.register_job({**self.job("bad"), "session": "session:not-a-mention"})
        root = self.lifecycle.register_job(self.job("root"))
        child = self.lifecycle.register_job(self.job(
            "child", dependencies=["root"], session="@session:" + root["session_id"]))
        self.assertEqual(child["session_id"], root["session_id"])

    def test_digest_is_read_only_attachment_with_24kib_limit(self):
        job = self.lifecycle.register_job(self.job("root"))
        session_id = job["session_id"]
        self.lifecycle.attach_digest(session_id, "verified context")
        self.assertEqual(self.lifecycle.session(session_id)["digest"], "verified context")
        with self.assertRaises(Invalid):
            self.lifecycle.attach_digest(session_id, "x" * (24 * 1024 + 1))

    def test_new_session_warms_compact_then_rpa_init_once_with_large_context_model(self):
        job = self.lifecycle.register_job(self.job("root"))
        calls = self.warm(job["session_id"])
        self.assertEqual([call[0] for call in calls], ["/compact", "/rpa-init"])
        self.assertTrue(all(call[1] == DEFAULT_WARMUP_MODEL for call in calls))
        self.assertEqual(self.lifecycle.session(job["session_id"])["state"], "ready")
        self.assertEqual(self.warm(job["session_id"]), [])

    def test_linear_dependency_reuses_completed_session_and_records_transfer(self):
        root = self.lifecycle.register_job(self.job("root"))
        child = self.lifecycle.register_job(self.job("child", dependencies=["root"]))
        self.warm(root["session_id"])
        run = self.lifecycle.start_job("root")
        self.lifecycle.complete_run(run["id"], "succeeded", result={"answer": 42})
        child = self.lifecycle.job("child")
        self.assertEqual(child["state"], "ready")
        self.assertEqual(child["session_id"], root["session_id"])
        transfers = self.lifecycle.transfers("child")
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0]["state"], "delivered")
        next_run = self.lifecycle.start_job("child")
        self.assertEqual(next_run["session_id"], root["session_id"])
        self.lifecycle.complete_run(next_run["id"], "succeeded", result={"done": True})

    def test_parallel_dependents_get_independent_lineage_sessions(self):
        root = self.lifecycle.register_job(self.job("root"))
        self.lifecycle.register_job(self.job("left", dependencies=["root"]))
        self.lifecycle.register_job(self.job("right", dependencies=["root"]))
        self.warm(root["session_id"])
        run = self.lifecycle.start_job("root")
        self.lifecycle.complete_run(run["id"], "succeeded", result={"answer": 1})
        left = self.lifecycle.job("left")
        right = self.lifecycle.job("right")
        self.assertNotEqual(left["session_id"], right["session_id"])
        self.assertEqual(self.lifecycle.session(left["session_id"])["parent_session_id"], root["session_id"])
        self.assertEqual(self.lifecycle.session(right["session_id"])["parent_session_id"], root["session_id"])
        self.assertEqual([branch["ordinal"] for branch in self.lifecycle.branches(root["session_id"])], [1, 2])
        left_calls = self.warm(left["session_id"])
        right_calls = self.warm(right["session_id"])
        self.assertEqual(len(left_calls), 2)
        self.assertEqual(len(right_calls), 2)
        left_run = self.lifecycle.start_job("left")
        with self.assertRaises(SessionConflict):
            self.lifecycle.start_job("left")
        self.lifecycle.complete_run(left_run["id"], "succeeded", result={"side": "left"})

    def test_restart_marks_running_run_unknown_and_never_replays_it(self):
        job = self.lifecycle.register_job(self.job("root"))
        self.warm(job["session_id"])
        run = self.lifecycle.start_job("root")
        restarted = SessionLifecycle(self.store)
        report = restarted.recover()
        self.assertEqual(report["runs"], [run["id"]])
        self.assertEqual(restarted.job("root")["state"], "needs_operator")
        self.assertEqual(restarted.session(job["session_id"])["state"], "recovering")
        self.assertEqual(len(restarted.run_history("root")), 1)
        with self.assertRaises(SessionConflict):
            restarted.start_job("root")

    def test_warmup_restart_requires_operator_and_reuses_same_command_key(self):
        job = self.lifecycle.register_job(self.job("root"))
        session_id = job["session_id"]
        with self.store.transaction() as db:
            db.execute("UPDATE lifecycle_sessions SET state='warming',warmup_step='/compact',compact_status='running' WHERE id=?",
                       (session_id,))
        restarted = SessionLifecycle(self.store)
        report = restarted.recover()
        self.assertEqual(report["sessions"], [session_id])
        calls = []
        with self.assertRaises(SessionConflict):
            restarted.warm_session(session_id, lambda *args: calls.append(args))
        result = restarted.retry_warmup(session_id, lambda *args: calls.append(args) or True,
                                        operator_confirmed=True)
        self.assertEqual(result.session_id, session_id)
        self.assertEqual(calls[0][0], "/compact")
        self.assertEqual(calls[0][3], f"warmup:{session_id}:/compact")
        self.assertEqual([call[0] for call in calls], ["/compact", "/rpa-init"])

    def test_failed_warmup_can_retry_with_its_same_command_key(self):
        job = self.lifecycle.register_job(self.job("root"))
        session_id = job["session_id"]
        failed_calls = []

        def fail(command, model, received_session, key):
            failed_calls.append((command, model, received_session, key))
            raise RuntimeError("temporary warm-up failure")

        with self.assertRaises(SessionConflict):
            self.lifecycle.warm_session(session_id, fail)
        self.assertEqual(self.lifecycle.session(session_id)["state"], "failed")
        self.assertEqual(failed_calls[0][3], f"warmup:{session_id}:/compact")

        retry_calls = []
        result = self.lifecycle.retry_warmup(
            session_id,
            lambda *args: retry_calls.append(args) or True,
        )
        self.assertEqual(result.session_id, session_id)
        self.assertEqual(retry_calls[0][3], failed_calls[0][3])
        self.assertEqual([call[0] for call in retry_calls], ["/compact", "/rpa-init"])
        self.assertEqual(self.lifecycle.session(session_id)["state"], "ready")

    def test_failed_replay_safe_job_can_retry_without_rewarming_session(self):
        job = self.lifecycle.register_job(self.job("root", metadata={"replay_safe": True}))
        calls = self.warm(job["session_id"])
        run = self.lifecycle.start_job("root")
        self.lifecycle.complete_run(run["id"], "failed", error_kind="transient", error_message="temporary")
        self.lifecycle.retry_job("root")
        retry = self.lifecycle.start_job("root")
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(len(calls), 2)

    def test_registration_rolls_back_session_and_job_together(self):
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER lifecycle_fail AFTER INSERT ON lifecycle_jobs "
                       "BEGIN SELECT RAISE(ABORT, 'injected lifecycle failure'); END")
        with self.assertRaises(Conflict):
            self.lifecycle.register_job(self.job("atomic"))
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER lifecycle_fail")
        self.assertEqual(self.lifecycle.jobs(), [])
        self.assertEqual(self.lifecycle.sessions(), [])

    def test_workflow_registration_rejects_cycles_atomically(self):
        with self.assertRaises(Invalid):
            self.lifecycle.register_workflow([
                self.job("a", dependencies=["b"]),
                self.job("b", dependencies=["a"]),
            ])
        self.assertEqual(self.lifecycle.jobs(), [])
        self.assertEqual(self.lifecycle.sessions(), [])


if __name__ == "__main__":
    unittest.main()
