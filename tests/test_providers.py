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
from boring_agent.session_lifecycle import SessionLifecycle
from boring_agent.store import Store


from tests.support.http_provider import coddy_stream, completion, json_response, server


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

    def test_coddy_nonstream_responses_preserve_session_and_parse_chat_shape(self):
        session_id = "sess_0123456789abcdef01234567"
        body = {"id": "resp_fixture", "choices": [{"message": {"content": '{"ok":true}'},
                                                      "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                "metadata": {"model": "fixture-model"}}
        with server([json_response(200, body, {"X-Coddy-Session-ID": session_id})]) as (base, requests, _, _):
            provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                session_id=session_id, stream=False)
            result = provider.complete([{"role": "system", "content": "policy"},
                                        {"role": "user", "content": "work"}])
        self.assertEqual((result.text, result.tokens, result.truncated), ('{"ok":true}', 10, False))
        self.assertEqual(requests[0]["path"], "/v1/responses")
        self.assertEqual(requests[0]["body"], {"model": "fixture-model", "input": "policy\n\nwork",
                                                "stream": False, "max_output_tokens": 2048})
        self.assertEqual(requests[0]["headers"]["x-coddy-session-id"], session_id)

    def test_coddy_stream_parses_text_usage_terminal_signal_and_metadata(self):
        session_id = "sess_0123456789abcdef01234567"
        with server([coddy_stream('{"ok":true}', session_id=session_id)]) as (base, requests, _, _):
            provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                session_id=session_id, stream=True)
            result = provider.complete([{"role": "user", "content": "work"}])
        self.assertEqual((result.text, result.tokens, result.truncated), ('{"ok":true}', 30, False))
        self.assertEqual(result.session_id, session_id)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(requests[0]["body"]["stream"], True)

    def test_coddy_stream_requires_done_and_maps_structured_error_without_echoing_message(self):
        session_id = "sess_0123456789abcdef01234567"
        incomplete = (200, "text/event-stream",
                      'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n', {})
        error = (200, "text/event-stream",
                 'event: error\ndata: {"error":{"message":"fixture-secret","type":"upstream_error",'
                 '"upstream_status":400,"code":"bad_request"}}\n\n', {})
        for response, kind in ((incomplete, "unknown"), (error, "permanent")):
            with self.subTest(kind=kind), server([response]) as (base, _, _, _):
                provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                    session_id=session_id, stream=True)
                with self.assertRaises(ExecutionError) as raised:
                    provider.complete([{"role": "user", "content": "work"}])
                self.assertEqual(raised.exception.kind, kind)
                self.assertNotIn("fixture-secret", str(raised.exception))

    def test_coddy_stream_requires_explicit_terminal_reason_and_never_replays_after_tool_activity(self):
        session_id = "sess_0123456789abcdef01234567"
        done_only = (200, "text/event-stream", "data: [DONE]\n\n", {})
        tool_then_error = (200, "text/event-stream",
                           'event: tool_call\ndata: {"name":"write_file"}\n\n'
                           'event: error\ndata: {"error":{"upstream_status":400}}\n\n', {})
        for response in (done_only, tool_then_error):
            with self.subTest(response=response[2]), server([response]) as (base, _, _, _):
                provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                    session_id=session_id, stream=True)
                with self.assertRaises(ExecutionError) as raised:
                    provider.complete([{"role": "user", "content": "work"}])
                self.assertEqual(raised.exception.kind, "unknown")

    def test_coddy_stream_rejects_malformed_terminal_reason_values(self):
        session_id = "sess_0123456789abcdef01234567"
        malformed = (False, "", "   ", {}, [], 0, None)
        for source in ("finish_reason", "stop_reason"):
            for value in malformed:
                reasons = {"finish_reason": None, "stop_reason": None, source: value}
                with self.subTest(source=source, value=value), server([
                    coddy_stream("complete", session_id=session_id, **reasons),
                ]) as (base, _, _, _):
                    provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                        session_id=session_id, stream=True)
                    with self.assertRaises(ExecutionError) as raised:
                        provider.complete([{"role": "user", "content": "work"}])
                    self.assertEqual(raised.exception.kind, "unknown")

    def test_prepared_warmup_evidence_requires_explicit_success_or_adjacent_roles(self):
        explicit = {"messages": [
            {"command": "/compact", "status": "succeeded"},
            {"command": "/rpa-init", "metadata": {"status": "completed"}},
        ]}
        adjacent = {"messages": [
            {"role": "user", "content": "/compact"},
            {"role": "assistant", "success": True},
            {"role": "user", "command": "/rpa-init"},
            {"role": "assistant", "status": "success"},
        ]}
        nonadjacent = {"messages": [
            {"role": "user", "content": "/compact"},
            {"role": "user", "content": "unrelated work"},
            {"role": "assistant", "status": "succeeded"},
            {"role": "user", "content": "/rpa-init"},
            {"role": "assistant", "status": "succeeded"},
        ]}
        wrong_roles = {"messages": [
            {"role": "assistant", "content": "/compact"},
            {"role": "assistant", "status": "succeeded"},
        ]}
        explicit_wrong_roles = {"messages": [
            {"role": role, "command": command, "status": "succeeded"}
            for role in ("assistant", "system", "tool")
            for command in ("/compact", "/rpa-init")
        ]}
        self.assertEqual(Provider.prepared_warmup_evidence(explicit), ("/compact", "/rpa-init"))
        self.assertEqual(Provider.prepared_warmup_evidence(adjacent), ("/compact", "/rpa-init"))
        self.assertEqual(Provider.prepared_warmup_evidence(nonadjacent), ("/rpa-init",))
        self.assertEqual(Provider.prepared_warmup_evidence(wrong_roles), ())
        self.assertEqual(Provider.prepared_warmup_evidence(explicit_wrong_roles), ())

    def test_prepared_warmup_evidence_rejects_failures_and_command_shaped_replies(self):
        for status in ("failed", "failure", "cancelled", "canceled", "error"):
            for command in ("/compact", "/rpa-init"):
                with self.subTest(status=status, command=command):
                    snapshot = {"messages": [
                        {"role": "user", "command": command, "status": status},
                        {"role": "assistant", "status": "succeeded"},
                    ]}
                    self.assertEqual(Provider.prepared_warmup_evidence(snapshot), ())

        rejected = (
            {"messages": [
                {"role": "user", "content": "/compact", "success": False},
                {"role": "assistant", "status": "succeeded"},
            ]},
            {"messages": [
                {"command": "/compact", "status": "error", "success": True},
            ]},
            {"messages": [
                {"role": "user", "content": "/compact"},
                {"role": "assistant", "command": "/rpa-init", "status": "succeeded"},
            ]},
            {"messages": [
                {"role": "user", "content": "/compact"},
                {"role": "assistant", "content": "/rpa-init", "status": "succeeded"},
            ]},
        )
        for snapshot in rejected:
            with self.subTest(snapshot=snapshot):
                self.assertEqual(Provider.prepared_warmup_evidence(snapshot), ())

    def test_prepared_warmup_evidence_failure_from_either_status_source_dominates(self):
        conflicting = (
            {"command": "/compact", "status": "succeeded", "metadata": {"status": "failed"}},
            {"command": "/compact", "status": "completed", "metadata": {"status": "cancelled"}},
            {"command": "/compact", "status": "failed", "metadata": {"status": "succeeded"}},
            {"command": "/compact", "status": None,
             "metadata": {"status": "failure"}, "success": True},
            {"command": "/compact", "status": {"unexpected": "value"},
             "metadata": {"status": "error"}, "success": True},
            {"command": "/compact", "metadata": {"status": "success"}, "success": False},
        )
        for message in conflicting:
            with self.subTest(message=message):
                self.assertEqual(Provider.prepared_warmup_evidence({"messages": [message]}), ())

        metadata_success = {"messages": [
            {"command": "/compact", "metadata": {"status": "success"}},
            {"role": "user", "command": "/rpa-init", "metadata": {"status": "completed"}},
        ]}
        wrong_role = {"messages": [
            {"role": "assistant", "command": "/compact", "metadata": {"status": "succeeded"}},
        ]}
        self.assertEqual(Provider.prepared_warmup_evidence(metadata_success), ("/compact", "/rpa-init"))
        self.assertEqual(Provider.prepared_warmup_evidence(wrong_role), ())

    def test_coddy_model_catalog_exposes_only_explicit_context_metadata(self):
        body = {"data": [
            {"id": "fixture-model", "max_context_tokens": 131072},
            {"id": "missing-context"},
            {"id": "bad-context", "max_context_tokens": True},
        ]}
        with server([json_response(200, body)]) as (base, requests, _, _):
            provider = Provider("coddy", base + "/v1", "fixture-model", "secret")
            contexts = provider.model_contexts()
        self.assertEqual(contexts, {"fixture-model": 131072})
        self.assertEqual(requests[0]["path"], "/v1/models")

    def test_coddy_mention_passes_spawn_options_and_only_narrows_parent_permission(self):
        session_id = "sess_0123456789abcdef01234567"
        responses = [
            json_response(200, {"settings": {"permissionMode": "ask"}}),
            coddy_stream('{"delegated":true}', session_id=session_id),
        ]
        mention = {"agent": "exec", "prompt": "Do the work", "description": "Do fixture work",
                   "background": False, "expected_seconds": 30, "timeout_seconds": 60,
                   "model": "child-model", "reasoning": "high", "notify_on_finish": False,
                   "permission_mode": "bypass"}
        with server(responses) as (base, requests, _, _):
            provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                session_id=session_id, permission_mode="ask", stream=True)
            provider.complete([{"role": "user", "content": "delegate"}], mention=mention)
        self.assertEqual(requests[0]["method"], "PATCH")
        self.assertEqual(requests[0]["body"], {"permissionMode": "ask"})
        self.assertEqual(requests[1]["body"]["model"], "agent")
        self.assertEqual(requests[1]["body"]["metadata"]["model"], "fixture-model")
        self.assertNotIn("max_output_tokens", requests[1]["body"])
        text = requests[1]["body"]["input"]
        self.assertIn("@agent:exec", text)
        self.assertIn("permission_mode: ask", text)
        for key in ("agent", "prompt", "description", "background", "expected_seconds",
                    "timeout_seconds", "model", "reasoning", "notify_on_finish"):
            self.assertIn(f'"{key}"', text)

    def test_coddy_task_dispatches_mention_after_one_durable_warmup(self):
        mention = {"agent": "exec", "prompt": "Read input.txt and return the answer",
                   "description": "Read fixture answer", "background": False,
                   "expected_seconds": 30, "timeout_seconds": 60,
                   "model": "fixture-child-model", "reasoning": "high",
                   "notify_on_finish": False, "permission_mode": "bypass"}
        task_id, attempt_id = self.submit(coddy={"permission_mode": "accept_edits",
                                                  "stream": True, "mention": mention})
        from boring_agent.runner import Controller
        session_id = Controller._coddy_session_id(task_id)
        responses = [
            json_response(200, {"data": [{"id": "fixture-model", "max_context_tokens": 131072}]}),
            coddy_stream("session compacted", session_id=session_id),
            coddy_stream("project initialized", session_id=session_id),
            json_response(200, {"settings": {"permissionMode": "accept_edits"}}),
            coddy_stream(json.dumps({"action": "final", "result": {"answer": 42}}),
                         session_id=session_id),
        ]
        with server(responses) as (base, requests, _, _), self.environment("coddy", base):
            run_attempt(self.store, attempt_id, "llm")
        self.manager.tick()
        self.assertEqual(self.store.task(task_id)["status"], "Succeeded")
        self.assertEqual(requests[0]["path"], "/v1/models")
        self.assertEqual([request["body"]["input"] for request in requests[1:3]],
                         ["/compact", "/rpa-init"])
        self.assertEqual(requests[1]["body"]["metadata"]["model"], "fixture-model")
        self.assertEqual(requests[3]["body"], {"permissionMode": "accept_edits"})
        self.assertIn("@agent:exec", requests[4]["body"]["input"])
        self.assertNotIn("max_output_tokens", requests[4]["body"])
        self.assertEqual({request["headers"]["x-coddy-session-id"] for request in requests[1:]},
                         {session_id})

    def test_missing_resumed_session_cannot_inherit_bypass_permission(self):
        session_id = "sess_0123456789abcdef01234567"
        persisted = SessionLifecycle(self.store).ensure_session(
            session_id=session_id, model="fixture-model", cwd=str(self.workspace),
            permission_mode="bypass", inherited_permission=True,
        )
        self.assertEqual(persisted["permission_mode"], "bypass")
        task_id, attempt_id = self.submit(coddy={
            "session": "@session:" + session_id,
            "permission_mode": "bypass",
            "stream": True,
        })
        responses = [
            json_response(200, {"data": [{"id": "fixture-model", "max_context_tokens": 131072}]}),
            json_response(404, {"error": {"type": "session_not_found"}}),
        ]
        with server(responses) as (base, requests, _, _), self.environment("coddy", base):
            run_attempt(self.store, attempt_id, "llm")
        self.manager.tick()
        task = self.store.task(task_id)
        self.assertEqual(task["status"], "Failed")
        self.assertIn("bypass", task["reason"])
        self.assertEqual([request["method"] for request in requests], ["GET", "GET"])

    def test_coddy_task_options_are_strict_and_do_not_accept_connection_secrets(self):
        invalid = [
            {"url": "http://example.invalid"},
            {"session": "@session:not-a-coddy-session"},
            {"permission_mode": "bypass"},
            {"stream": "yes"},
            {"mention": {"agent": "Bad Agent"}},
            {"mention": {"agent": "exec", "timeout_seconds": 0}},
        ]
        for index, coddy in enumerate(invalid):
            with self.subTest(coddy=coddy), self.assertRaises(Invalid):
                self.store.submit({"objective": "invalid", "runtime": "llm", "coddy": coddy},
                                  f"invalid-coddy-{index}")

    def test_coddy_http_error_mapping_uses_envelope_status_without_leaking_body(self):
        cases = [(400, "permanent"), (409, "transient"), (429, "transient"),
                 (500, "unknown"), (502, "unknown"), (504, "unknown")]
        session_id = "sess_0123456789abcdef01234567"
        for status, kind in cases:
            body = {"error": {"message": "fixture-secret", "type": "upstream_error",
                              "upstream_status": status}}
            with self.subTest(status=status), server([json_response(status, body)]) as (base, _, _, _):
                provider = Provider("coddy", base + "/v1", "fixture-model", "secret",
                                    session_id=session_id, stream=False)
                with self.assertRaises(ExecutionError) as raised:
                    provider.complete([{"role": "user", "content": "work"}])
                self.assertEqual(raised.exception.kind, kind)
                self.assertNotIn("fixture-secret", str(raised.exception))

    def test_provider_configuration_is_explicit(self):
        for url in ("http://remote.example/v1", "https://user:secret@example.com", "file:///tmp/model", "https://example.com/?key=secret"):
            with self.assertRaises(Invalid):
                Provider("openai", url, "model")
        with self.assertRaises(Invalid):
            Provider("openai", "https://example.com/v1", "")


if __name__ == "__main__":
    unittest.main()
