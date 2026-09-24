import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from boring_agent.manager import Manager
from boring_agent.model import Invalid
from boring_agent.providers import ExecutionError, Provider
from boring_agent.runner import run_attempt
from boring_agent.store import Store


from tests.support.http_provider import completion, server


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "input.txt").write_text("The required answer is 42.")
        self.store = Store(self.root / "state")
        self.store.initialize(self.workspace)
        self.store.register_worker("llm", ["llm"], 1)
        self.manager = Manager(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, **changes):
        spec = {"objective": "Read input.txt and give the answer", "runtime": "llm",
                "output_schema": {"type": "object", "required": ["answer"],
                                  "properties": {"answer": {"type": "integer"}}}, **changes}
        task_id = self.store.submit(spec, str(time.monotonic_ns()))["task_id"]
        self.manager.tick()
        return task_id, self.store.task(task_id)["current_attempt_id"]

    def environment(self, kind, base):
        return patch.dict(os.environ, {"BOA_PROVIDER": kind, "BOA_BASE_URL": base,
                                       "BOA_MODEL": "fixture-model", "BOA_API_KEY": "fixture-secret"})

    def test_all_providers_run_real_http_tool_loop_and_account_usage(self):
        for kind, path in (("openai", "/chat/completions"), ("anthropic", "/messages"), ("ollama", "/api/chat")):
            with self.subTest(kind=kind), server([
                (200, completion(kind, {"action": "read_file", "path": "input.txt"})),
                (200, completion(kind, {"action": "final", "result": {"answer": 42}})),
            ]) as (base, requests, _, _), self.environment(kind, base):
                task_id, aid = self.submit()
                run_attempt(self.store, aid, "llm")
                self.manager.tick()
                task = self.store.task(task_id)
                self.assertEqual(task["status"], "Succeeded", task["reason"])
                self.assertEqual(task["tokens_used"], 60)
                self.assertFalse(task["usage_unknown"])
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0]["path"], path)
                self.assertIn("required answer is 42", requests[1]["body"]["messages"][-1]["content"])
                self.assertNotIn("fixture-secret", json.dumps(self.store.events(task_id)))
                self.assertEqual(requests[0]["body"]["model"], "fixture-model")
                if kind == "anthropic":
                    self.assertIn("system", requests[0]["body"])
                    self.assertEqual(requests[0]["headers"]["x-api-key"], "fixture-secret")
                elif kind == "openai":
                    self.assertEqual(requests[0]["headers"]["authorization"], "Bearer fixture-secret")

    def test_rate_limit_is_confirmed_rejection_and_retry_is_manager_owned(self):
        with server([(429, {"error": "busy"}), (200, completion("openai", {"action": "final", "result": {"answer": 42}}))]) as (base, requests, _, _), self.environment("openai", base):
            task_id, aid = self.submit(retry={"replay_safe": True, "max_attempts": 2, "backoff_seconds": .01})
            run_attempt(self.store, aid, "llm")
            self.assertEqual(len(requests), 1)
            self.manager.tick()
            self.assertEqual(self.store.task(task_id)["status"], "Pending")
            time.sleep(.015)
            self.manager.tick()
            new_aid = self.store.task(task_id)["current_attempt_id"]
            self.assertNotEqual(aid, new_aid)
            run_attempt(self.store, new_aid, "llm")
            self.manager.tick()
            self.assertEqual(self.store.task(task_id)["status"], "Succeeded")

    def test_remote_timeout_preserves_unknown_and_capacity(self):
        with server(["wait"]) as (base, requests, _, _), self.environment("openai", base):
            task_id, aid = self.submit(budget={"request_seconds": .1},
                                       retry={"replay_safe": True, "max_attempts": 3})
            run_attempt(self.store, aid, "llm")
            self.manager.tick()
            task = self.store.task(task_id)
            self.assertEqual(task["observation_condition"], "Unknown")
            self.assertEqual(self.store.capacity()["used"], 1)
            self.assertEqual(len(requests), 1)
            self.assertIsNone(task["attempts"][0]["tokens"])

    def test_cancel_waits_for_inflight_response_and_blocks_further_tools(self):
        with server(["wait"]) as (base, requests, entered, release), self.environment("openai", base):
            task_id, aid = self.submit()
            thread = threading.Thread(target=run_attempt, args=(self.store, aid, "llm"))
            thread.start()
            self.assertTrue(entered.wait(2))
            self.store.cancel(task_id, "cancel")
            self.manager.tick()
            self.assertNotEqual(self.store.task(task_id)["status"], "Cancelled")
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.manager.tick()
            task = self.store.task(task_id)
            self.assertEqual(task["status"], "Cancelled")
            self.assertEqual(self.store.capacity()["used"], 0)
            self.assertEqual(task["tokens_used"], 30)
            self.assertEqual(len(requests), 1)

    def test_runner_killed_during_remote_call_is_not_relaunched(self):
        with server(["wait"]) as (base, requests, entered, release), self.environment("openai", base):
            task_id, aid = self.submit()
            child = subprocess.Popen([sys.executable, "-m", "boring_agent.runner", "--home", str(self.store.home),
                                      "--attempt", aid, "--worker", "llm"])
            try:
                self.assertTrue(entered.wait(3))
                child.kill()
                child.wait(timeout=2)
                Manager(Store(self.store.home)).tick()
                run_attempt(self.store, aid, "llm")
                self.assertEqual(self.store.task(task_id)["observation_condition"], "Unknown")
                self.assertEqual(len(requests), 1)
                self.assertEqual(self.store.capacity()["used"], 1)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
                release.set()

    def test_missing_usage_blocks_next_budgeted_model_call(self):
        with server([(200, completion("openai", {"action": "read_file", "path": "input.txt"}, usage=False))]) as (base, requests, _, _), self.environment("openai", base):
            task_id, aid = self.submit(budget={"max_tokens": 100})
            run_attempt(self.store, aid, "llm")
            self.manager.tick()
            task = self.store.task(task_id)
            self.assertEqual(task["status"], "Failed")
            self.assertTrue(task["usage_unknown"])
            self.assertEqual(len(requests), 1)

    def test_partial_known_usage_remains_visible_when_later_usage_is_missing(self):
        with server([
            (200, completion("openai", {"action": "read_file", "path": "input.txt"})),
            (200, completion("openai", {"action": "final", "result": {"answer": 42}}, usage=False)),
        ]) as (base, _, _, _), self.environment("openai", base):
            task_id, aid = self.submit()
            run_attempt(self.store, aid, "llm")
            self.manager.tick()
            task = self.store.task(task_id)
            self.assertEqual(task["status"], "Succeeded")
            self.assertEqual(task["tokens_used"], 30)
            self.assertTrue(task["usage_unknown"])
            self.assertIsNone(task["attempts"][0]["tokens"])

    def test_truncated_completions_are_explained(self):
        # Empty and cut off: the reasoning budget was exhausted; deterministic, so permanent.
        with server([completion("openai", None, truncated=True) and (200, completion("openai", None, truncated=True))]) as (base, _, _, _), self.environment("openai", base):
            task_id, aid = self.submit(retry={"replay_safe": True, "max_attempts": 2, "backoff_seconds": .01})
            run_attempt(self.store, aid, "llm")
            self.manager.tick()
            task = self.store.task(task_id)
            self.assertEqual((task["status"], task["attempts"][0]["error_kind"]), ("Failed", "permanent"))
            self.assertIn("truncated the completion before any content", task["reason"])
        # Cut off mid-object: still a validation failure, but it says why.
        body = {"choices": [{"message": {"content": '{"action":"final","result":{"answer":'}, "finish_reason": "length"}]}
        with server([(200, body)]) as (base, _, _, _), self.environment("openai", base):
            task_id, aid = self.submit()
            run_attempt(self.store, aid, "llm")
            self.manager.tick()
            self.assertIn("cut off at the output limit", self.store.task(task_id)["reason"])

    def test_malformed_action_and_step_budget_are_failures(self):
        for body, budget in (({"choices": [{"message": {"content": "not JSON"}}]}, {}),
                             (completion("openai", {"action": "read_file", "path": ".env"}), {"max_steps": 1})):
            with server([(200, body)]) as (base, requests, _, _), self.environment("openai", base):
                task_id, aid = self.submit(budget=budget)
                run_attempt(self.store, aid, "llm")
                self.manager.tick()
                self.assertEqual(self.store.task(task_id)["status"], "Failed")

    def test_http_errors_distinguish_confirmed_rejection_from_ambiguous_execution(self):
        # features/providers.feature: a 5xx response can come from an intermediary
        # while model execution continues. It is not a confirmed zero-cost rejection.
        cases = [(401, "Failed", "permanent"), (429, "Failed", "transient")]
        cases += [(code, "Unknown", "unknown") for code in (408, 500, 502, 503, 504, 529)]
        for status, expected, kind in cases:
            with server([(status, {"error": "sensitive echo fixture-secret"})]) as (base, _, _, _), self.environment("openai", base):
                task_id, aid = self.submit()
                run_attempt(self.store, aid, "llm")
                self.manager.tick()
                task = self.store.task(task_id)
                self.assertEqual((task["attempts"][0]["state"], task["attempts"][0]["error_kind"]), (expected, kind), status)
                self.assertNotIn("fixture-secret", json.dumps(task))
                if expected == "Unknown":
                    self.store.resolve(aid, "Fixture HTTP handler has returned and no remote operation exists", True)
                    self.manager.tick()

    def test_redirects_are_not_followed(self):
        with server([(302, {})]) as (base, requests, _, _):
            provider = Provider("openai", base, "fixture", "secret")
            with self.assertRaises(ExecutionError):
                provider.complete([{"role": "system", "content": "test"}, {"role": "user", "content": "test"}])
            self.assertEqual(len(requests), 1)

    def test_json_mode_is_requested_by_default_and_can_be_disabled(self):
        # The agent loop requires one JSON object per turn. Ollama is already asked for JSON;
        # an OpenAI-compatible server is asked with the standard response_format field.
        for env, expected in (({}, {"type": "json_object"}), ({"BOA_JSON_MODE": "off"}, None)):
            with server([(200, completion("openai", {"action": "final", "result": {"answer": 42}}))]) as (base, requests, _, _), \
                    self.environment("openai", base), patch.dict(os.environ, env):
                task_id, aid = self.submit()
                run_attempt(self.store, aid, "llm")
                self.manager.tick()
                self.assertEqual(self.store.task(task_id)["status"], "Succeeded")
                self.assertEqual(requests[0]["body"].get("response_format"), expected, env)

    def test_json_mode_is_not_sent_to_providers_without_that_field(self):
        for kind, absent in (("anthropic", "response_format"), ("ollama", "response_format")):
            with server([(200, completion(kind, {"action": "final", "result": {"answer": 42}}))]) as (base, requests, _, _), \
                    self.environment(kind, base):
                task_id, aid = self.submit()
                run_attempt(self.store, aid, "llm")
                self.manager.tick()
                self.assertEqual(self.store.task(task_id)["status"], "Succeeded")
                self.assertNotIn(absent, requests[0]["body"])
        self.assertEqual(requests[0]["body"]["format"], "json")  # Ollama's own JSON switch

    def test_provider_configuration_is_explicit(self):
        for url in ("http://remote.example/v1", "https://user:secret@example.com", "file:///tmp/model", "https://example.com/?key=secret"):
            with self.assertRaises(Invalid):
                Provider("openai", url, "model")
        with self.assertRaises(Invalid):
            Provider("openai", "https://example.com/v1", "")


if __name__ == "__main__":
    unittest.main()
