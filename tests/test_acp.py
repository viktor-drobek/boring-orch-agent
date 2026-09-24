import signal
import tempfile
import unittest
from pathlib import Path

from boring_agent.acp import (
    ACPError,
    BudgetCapabilities,
    CancellationSupervisor,
    IsolationError,
    IsolationPlanner,
    ProgressRecorder,
    TokenAccounting,
    WorkspaceCallback,
    adapter_capabilities,
    evaluate_budgets,
    negotiate,
    prepare_launch,
    secure_agent,
)


class ACPIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def task(self, **changes):
        value = {"workspace": str(self.workspace), "state_path": str(self.state), "sandbox": "read-only"}
        value.update(changes)
        return value

    def test_tier_a_binds_workspace_and_hides_store(self):
        planner = IsolationPlanner("available", which=lambda name: "/usr/bin/bwrap")
        decision = planner.evaluate(self.task())
        command = planner.command(["acp-agent", "--stdio"], self.task(), decision)
        self.assertEqual(decision.tier, "A")
        self.assertIn("--ro-bind", command)
        self.assertIn(str(self.workspace), command)
        self.assertIn("--unshare-net", command)
        self.assertNotIn("store", " ".join(command))

    def test_workspace_write_is_the_only_writable_tier_a_bind(self):
        planner = IsolationPlanner("available", which=lambda name: "/usr/bin/bwrap")
        command = planner.command(["agent"], self.task(sandbox="workspace-write"))
        pairs = [command[i:i + 3] for i in range(len(command) - 2)]
        self.assertIn(["--bind", str(self.workspace), str(self.workspace)], pairs)
        # Every mount option consumes its arguments: no orphaned --ro-bind may remain.
        options = {"--ro-bind": 2, "--bind": 2, "--tmpfs": 1, "--proc": 1, "--dev": 1, "--chdir": 1}
        index = 1
        while command[index] != "--":
            token = command[index]
            if token in options:
                for argument in command[index + 1:index + 1 + options[token]]:
                    self.assertFalse(argument.startswith("--"), command)
                index += 1 + options[token]
            else:
                self.assertTrue(token.startswith("--"), command)
                index += 1
        self.assertNotIn("--ro-bind", command[:command.index("--bind")], "no read-only bind precedes the writable one")

    def test_tmpfs_root_precedes_every_other_mount(self):
        planner = IsolationPlanner("available", which=lambda name: "/usr/bin/bwrap")
        command = planner.command(["agent"], self.task())
        root = command.index("/", command.index("--tmpfs"))
        for path in ("/proc", "/dev", "/tmp", "/run", "/home", str(self.workspace)):
            self.assertGreater(command.index(path), root, path)

    def test_store_home_is_never_a_workspace_or_agent_home(self):
        planner = IsolationPlanner("available", which=lambda name: "/usr/bin/bwrap", store_home=self.root / "store")
        (self.root / "store").mkdir()
        with self.assertRaisesRegex(IsolationError, "store home"):
            planner.evaluate(self.task(workspace=str(self.root / "store" / "artifacts")))
        with self.assertRaisesRegex(IsolationError, "store home"):
            planner.evaluate(self.task(state_path=str(self.root / "store")))

    def test_unavailable_and_unknown_fail_closed_for_read_only(self):
        for capability in ("unavailable", "unknown"):
            with self.subTest(capability=capability), self.assertRaisesRegex(IsolationError, "refused"):
                IsolationPlanner(capability).evaluate(self.task())

    def test_security_rejects_bypass_and_disables_extensions(self):
        for mode in ("bypass", "bypassPermissions", "--dangerously-skip-permissions"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ACPError, "bypass"):
                secure_agent({"permission_mode": mode}, str(self.state))
        with self.assertRaisesRegex(ACPError, "bypass"):
            secure_agent({"permission_mode": "default", "dangerously_skip_permissions": True}, str(self.state))
        security = secure_agent({"permission_mode": "default"}, str(self.state))
        self.assertEqual(security.restrictions, {"hooks": False, "mcp_servers": False,
                                                  "subagents": False, "skills": False})


class ACPOperationTests(unittest.TestCase):
    def test_unenforceable_budget_names_each_field_and_opt_in_is_explicit(self):
        task = {"budget": {"max_steps": 2, "per_call_seconds": 1}}
        decision = evaluate_budgets(task, BudgetCapabilities())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.unenforceable, ("max_steps", "per_call_seconds"))
        weaker = evaluate_budgets({**task, "weaker_contract_opt_in": True}, BudgetCapabilities())
        self.assertTrue(weaker.allowed)
        self.assertEqual(weaker.unenforceable, decision.unenforceable)

    def test_openai_shaped_usage_counts_and_missing_usage_stays_unknown(self):
        usage = TokenAccounting()
        self.assertFalse(usage.known)
        self.assertIsNone(usage.as_dict()["total_tokens"])
        usage.record({})
        self.assertFalse(usage.known)
        usage.record({"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.known), (10, 5, 15, True))

    def test_budget_capabilities_come_from_the_adapter_not_the_task(self):
        self.assertTrue(adapter_capabilities(["/usr/bin/coddy", "acp"]).token_reporting)
        self.assertFalse(adapter_capabilities(["claude-agent-acp"]).step_count)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ws").mkdir(); (root / "state").mkdir()
            task = {"workspace": str(root / "ws"), "state_path": str(root / "state"), "sandbox": "workspace-write",
                    "budget": {"max_steps": 3}, "budget_capabilities": {"step_count": True}}
            with self.assertRaisesRegex(ACPError, "property of the adapter"):
                prepare_launch(task, ["agent"], {"modes": [], "models": []}, capability="available")
            task.pop("budget_capabilities")
            with self.assertRaisesRegex(ACPError, "max_steps"):
                prepare_launch(task, ["agent"], {"modes": [], "models": []}, capability="available")

    def test_cumulative_usage_is_not_double_counted(self):
        usage = TokenAccounting()
        usage.record({"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        usage.record({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        self.assertEqual(usage.total_tokens, 15)
        self.assertEqual(usage.as_dict()["accounting"], "latest_cumulative_max")
        usage.record({"total_tokens": 4})
        self.assertTrue(usage.inconsistent)
        self.assertEqual(usage.total_tokens, 15)

    def test_absolute_callback_is_mapped_and_escape_is_invalid_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("note")
            callback = WorkspaceCallback(root)
            self.assertEqual(callback.relative(str(root / "note.txt")), "note.txt")
            with self.assertRaisesRegex(ACPError, "invalid_request"):
                callback.relative(str(root.parent / "outside.txt"))

    def test_plan_notification_is_progress_only(self):
        recorder = ProgressRecorder()
        evidence = recorder.record_plan({"tasks": [{"id": "child", "objective": "do not create"}]})
        self.assertEqual(evidence["kind"], "progress")
        self.assertEqual(evidence["children_created"], 0)
        self.assertEqual(recorder.as_dict()["children_created"], 0)

    def test_modes_and_models_are_negotiated(self):
        route = negotiate({"modes": ["interactive"], "models": ["fixture-1"]}, "interactive", "fixture-1")
        self.assertEqual(route.as_dict(), {"mode": "interactive", "model": "fixture-1", "advertised": True})
        with self.assertRaisesRegex(ACPError, "not advertised"):
            negotiate({"modes": ["interactive"], "models": ["fixture-1"]}, "bypass", "fixture-1")

    def test_prepare_launch_records_all_contracts_and_requires_a_private_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ws").mkdir(); (root / "state").mkdir()
            task = {"workspace": str(root / "ws"), "state_path": str(root / "state"), "sandbox": "workspace-write",
                    "mode": "interactive", "model": "fixture-1", "budget": {"deadline_seconds": 3}}
            plan = prepare_launch(task, ["agent", "--stdio"], {"modes": ["interactive"], "models": ["fixture-1"]},
                                  capability="available")
            self.assertEqual(plan.negotiated.model, "fixture-1")
            self.assertEqual(plan.isolation.tier, "A")
            self.assertEqual(plan.environment["HOME"], str((root / "state").resolve()))
            task.pop("state_path")
            with self.assertRaisesRegex(ACPError, "private state_path"):
                prepare_launch(task, ["agent"], {"modes": ["interactive"], "models": ["fixture-1"]}, capability="unavailable")


class ACPCancellationTests(unittest.TestCase):
    def test_cancellation_requires_both_evidence_sources_and_escalates(self):
        now = [100.0]
        exists = [True]
        sent = []
        supervisor = CancellationSupervisor(
            123, lambda: sent.append("cooperative"), grace_seconds=2, now=lambda: now[0],
            group_exists=lambda pgid: exists[0], send_signal=lambda pgid, value: sent.append(value),
        )
        self.assertEqual(supervisor.request()["status"], "Unknown")
        self.assertEqual(sent, ["cooperative"])
        now[0] = 102.1
        supervisor.poll()
        self.assertEqual(sent[-1], signal.SIGTERM)
        now[0] = 104.2
        supervisor.poll()
        self.assertEqual(sent[-1], signal.SIGKILL)
        exists[0] = False
        self.assertEqual(supervisor.poll()["status"], "Unknown")
        self.assertTrue(supervisor.poll()["reserved"])
        self.assertEqual(supervisor.adapter_stopped("cancelled")["status"], "Cancelled")
        self.assertFalse(supervisor.poll()["reserved"])

    def test_adapter_reason_without_gone_group_remains_unknown(self):
        supervisor = CancellationSupervisor(123, lambda: None, group_exists=lambda _: True)
        supervisor.request()
        self.assertEqual(supervisor.adapter_stopped("cancelled")["status"], "Unknown")

    def test_a_late_poll_escalates_through_both_signals(self):
        now, sent = [0.0], []
        supervisor = CancellationSupervisor(7, lambda: None, grace_seconds=1, now=lambda: now[0],
                                            group_exists=lambda _: True, send_signal=lambda pgid, value: sent.append(value))
        supervisor.request()
        now[0] = 10.0
        supervisor.poll()
        self.assertEqual(sent, [signal.SIGTERM])
        now[0] = 11.5
        supervisor.poll()
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])

    def test_acp_end_turn_after_cancel_counts_as_terminal_evidence(self):
        supervisor = CancellationSupervisor(7, lambda: None, group_exists=lambda _: False)
        supervisor.request()
        self.assertEqual(supervisor.adapter_stopped("end_turn")["status"], "Cancelled")


if __name__ == "__main__":
    unittest.main()
