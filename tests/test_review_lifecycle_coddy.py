"""Regression invariants for fail-closed Coddy permission and lifecycle evidence."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from boring_agent.manager import Manager
from boring_agent.model import SessionConflict
from boring_agent.providers import ExecutionError, Provider
from boring_agent.runner import Controller, run_attempt
from boring_agent.session_lifecycle import SessionLifecycle
from boring_agent.store import Store
from tests.support.http_provider import coddy_stream, json_response, server


SESSION = "sess_0123456789abcdef01234567"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.store = Store(root / "state")
        self.store.initialize(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, job_id, dependencies=None, **extra):
        return {"id": job_id, "objective": "work", "runtime": "coddy_native",
                "model": "codex/gpt-5.6-luna", "workspace": str(self.workspace),
                "dependencies": dependencies or [], **extra}


class CoddyPermissionTests(Base):
    def test_snapshot_without_permission_mode_fails_closed_to_ask(self):
        for snapshot in ({"messages": []}, {"settings": {"permissionMode": "unexpected"}},
                         {"settings": "not-an-object"}):
            with self.subTest(snapshot=snapshot), server([json_response(200, snapshot)]) as (base, _, _, _):
                provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                    session_id=SESSION, permission_mode="bypass")
                provider.session_snapshot()
                self.assertEqual(provider.permission_mode, "ask")

    def test_observed_snapshot_mode_is_inherited(self):
        with server([json_response(200, {"settings": {"permissionMode": "accept_edits"}})]) as (base, _, _, _):
            provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                session_id=SESSION, permission_mode="bypass")
            provider.session_snapshot()
        self.assertEqual(provider.permission_mode, "accept_edits")

    def test_mention_after_unreported_snapshot_never_requests_bypass(self):
        responses = [json_response(200, {"messages": []}), json_response(200, {}),
                     coddy_stream('{"delegated":true}', session_id=SESSION)]
        with server(responses) as (base, requests, _, _):
            provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                session_id=SESSION, permission_mode="bypass")
            provider.session_snapshot()
            provider.complete([{"role": "user", "content": "delegate"}],
                              mention={"agent": "exec", "prompt": "go", "permission_mode": "bypass"})
        self.assertEqual(requests[1]["method"], "PATCH")
        self.assertEqual(requests[1]["body"], {"permissionMode": "ask"})
        self.assertIn("permission_mode: ask", requests[2]["body"]["input"])


class WarmupOutcomeTests(Base):
    def setUp(self):
        super().setUp()
        self.lifecycle = SessionLifecycle(self.store)

    def session_for(self, job_id):
        return self.lifecycle.register_job(self.job(job_id))["session_id"]

    def test_unknown_and_unclassified_errors_become_recovering(self):
        for index, error in enumerate((ExecutionError("unknown", "stream ended before [DONE]"),
                                       RuntimeError("executor crashed"))):
            with self.subTest(error=error):
                session_id = self.session_for(f"uncertain-{index}")

                def raise_error(*args, error=error):
                    raise error

                with self.assertRaises(SessionConflict):
                    self.lifecycle.warm_session(session_id, raise_error)
                session = self.lifecycle.session(session_id)
                self.assertEqual(session["state"], "recovering")
                self.assertEqual(session["compact_status"], "running")
                self.assertEqual(session["recovery"]["retry_key"], f"warmup:{session_id}:/compact")
                self.assertEqual(session["recovery"]["action"], "needs_operator")

    def test_confirmed_failures_become_failed(self):
        failures = [ExecutionError(kind, "rejected") for kind in ("permanent", "transient", "validation")]
        for index, error in enumerate(failures):
            with self.subTest(kind=error.kind):
                session_id = self.session_for(f"failed-{index}")

                def raise_error(*args, error=error):
                    raise error

                with self.assertRaises(SessionConflict):
                    self.lifecycle.warm_session(session_id, raise_error)
                self.assertEqual(self.lifecycle.session(session_id)["state"], "failed")
        session_id = self.session_for("refused")
        with self.assertRaises(SessionConflict):
            self.lifecycle.warm_session(session_id, lambda *args: False)
        self.assertEqual(self.lifecycle.session(session_id)["state"], "failed")

    def test_uncertain_rpa_init_retry_needs_operator_and_keeps_compact(self):
        session_id = self.session_for("uncertain-init")
        calls = []

        def unknown_init(command, model, received, key):
            calls.append((command, key))
            if command == "/rpa-init":
                raise ExecutionError("unknown", "connection interrupted")
            return True

        with self.assertRaises(SessionConflict):
            self.lifecycle.warm_session(session_id, unknown_init)
        with self.assertRaises(SessionConflict):
            self.lifecycle.retry_warmup(session_id, lambda *args: calls.append(args) or True)
        with self.assertRaises(SessionConflict):
            self.lifecycle.start_job("uncertain-init", lambda *args: calls.append(args) or True)
        self.assertEqual(len(calls), 2)
        retry = []
        self.lifecycle.retry_warmup(session_id, lambda *args: retry.append(args) or True,
                                    operator_confirmed=True)
        self.assertEqual([(call[0], call[3]) for call in retry],
                         [("/rpa-init", f"warmup:{session_id}:/rpa-init")])
        self.assertEqual(self.lifecycle.session(session_id)["state"], "ready")


class StartJobPreparationTests(Base):
    def setUp(self):
        super().setUp()
        self.lifecycle = SessionLifecycle(self.store)

    def test_failed_warmup_session_cannot_start_a_job_until_rewarmed(self):
        job = self.lifecycle.register_job(self.job("root"))

        def reject(*args):
            raise ExecutionError("permanent", "bad command")

        with self.assertRaises(SessionConflict):
            self.lifecycle.start_job("root", reject)
        calls = []
        with self.assertRaisesRegex(SessionConflict, "/compact then /rpa-init"):
            self.lifecycle.start_job("root", lambda *args: calls.append(args) or True)
        with self.assertRaisesRegex(SessionConflict, "/compact then /rpa-init"):
            self.lifecycle.start_job("root")
        self.assertEqual(calls, [])
        self.assertEqual(self.lifecycle.run_history("root"), [])
        self.lifecycle.retry_warmup(job["session_id"], lambda *args: True)
        run = self.lifecycle.start_job("root")
        self.assertEqual(run["state"], "running")

    def test_new_session_without_executor_is_refused(self):
        self.lifecycle.register_job(self.job("root"))
        with self.assertRaisesRegex(SessionConflict, "warmed"):
            self.lifecycle.start_job("root")
        self.assertEqual(self.lifecycle.run_history("root"), [])


class ExplicitSessionDependencyTests(Base):
    def test_dependent_with_explicit_session_becomes_ready_and_keeps_that_session(self):
        lifecycle = SessionLifecycle(self.store)
        root = lifecycle.register_job(self.job("root"))
        other = lifecycle.create_session(model="codex/gpt-5.6-luna", cwd=str(self.workspace))
        child = lifecycle.register_job(self.job("child", dependencies=["root"],
                                                session="@session:" + other["id"]))
        self.assertEqual(child["state"], "pending")
        lifecycle.warm_session(root["session_id"], lambda *args: True)
        run = lifecycle.start_job("root")
        lifecycle.complete_run(run["id"], "succeeded", result={"answer": 42})
        child = lifecycle.job("child")
        self.assertEqual((child["state"], child["session_id"]), ("ready", other["id"]))
        transfers = lifecycle.transfers("child")
        self.assertEqual([(t["target_session_id"], t["state"]) for t in transfers],
                         [(other["id"], "delivered")])
        self.assertEqual(lifecycle.branches(), [])
        # The explicit session is new, so it still must warm before the job.
        with self.assertRaises(SessionConflict):
            lifecycle.start_job("child")
        calls = []
        lifecycle.start_job("child", lambda *args: calls.append(args[0]) or True)
        self.assertEqual(calls, ["/compact", "/rpa-init"])


class SchemaTransactionTests(Base):
    def test_schema_setup_keeps_the_immediate_transaction_and_rolls_back(self):
        observed = {}
        real = self.store.transaction

        class Wrapper:
            home = self.store.home

            @staticmethod
            def transaction():
                from contextlib import contextmanager

                @contextmanager
                def managed():
                    with real() as db:
                        yield db
                        observed["open"] = db.in_transaction
                        raise RuntimeError("injected")
                return managed()

        with self.assertRaisesRegex(RuntimeError, "injected"):
            SessionLifecycle(Wrapper())
        self.assertIs(observed["open"], True)
        with self.store.reading() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='lifecycle_meta'").fetchone())
        lifecycle = SessionLifecycle(self.store)
        with self.store.reading() as db:
            self.assertEqual(db.execute(
                "SELECT value FROM lifecycle_meta WHERE key='schema_version'").fetchone()[0], "1")
        self.assertEqual(lifecycle.jobs(), [])


class RunnerUnknownWarmupTests(Base):
    def test_unknown_coddy_warmup_is_unknown_and_never_replayed_by_the_runner(self):
        self.store.register_worker("llm", ["llm"], 1)
        manager = Manager(self.store)
        spec = {"objective": "Answer", "runtime": "llm", "output_schema": {"type": "object"},
                "budget": {"request_seconds": 1},
                "retry": {"replay_safe": True, "max_attempts": 2, "backoff_seconds": .01}}
        task_id = self.store.submit(spec, str(time.monotonic_ns()))["task_id"]
        manager.tick()
        attempt_id = self.store.task(task_id)["current_attempt_id"]
        session_id = Controller._coddy_session_id(task_id)
        truncated = (200, "text/event-stream",
                     'data: {"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n\n',
                     {"X-Coddy-Session-ID": session_id})
        responses = [json_response(200, {"data": [{"id": "fixture-model", "max_context_tokens": 131072}]}),
                     truncated]
        with server(responses) as (base, requests, _, _), patch.dict(os.environ, {
                "BOA_PROVIDER": "coddy", "BOA_BASE_URL": base, "BOA_MODEL": "fixture-model",
                "BOA_API_KEY": "fixture-secret"}):
            run_attempt(self.store, attempt_id, "llm")
        manager.tick()
        self.assertEqual(self.store.task(task_id)["observation_condition"], "Unknown")
        self.assertEqual([r["body"]["input"] for r in requests if r["method"] == "POST"], ["/compact"])
        session = SessionLifecycle(self.store).session(session_id)
        self.assertEqual(session["state"], "recovering")
        self.assertEqual(json.loads(json.dumps(session["recovery"]))["retry_key"],
                         f"warmup:{session_id}:/compact")


if __name__ == "__main__":
    unittest.main()
