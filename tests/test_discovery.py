import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from boring_agent.discovery import Discovery, sanitize_evidence
from boring_agent.model import Conflict, Invalid
from boring_agent.store import Store


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = Store(self.root / "state")
        self.store.initialize(self.workspace)
        self.discovery = Discovery(self.store, max_output_bytes=256)

    def tearDown(self):
        self.temp.cleanup()

    def executable_route(self, route_id="fixture", **changes):
        route = {"id": route_id, "executable": sys.executable, "args": ["--version"]}
        route.update(changes)
        return route

    def test_init_seeds_passive_metadata_without_starting_process_or_network(self):
        with patch("boring_agent.discovery.subprocess.Popen", side_effect=AssertionError("active probe")), \
                patch("boring_agent.discovery.urllib", side_effect=AssertionError("network"), create=True):
            inventory = self.store.discovery_inventory()
        self.assertTrue(inventory)
        self.assertTrue(all(item["tier"] == "passive" for item in inventory))
        self.assertIn("coddy", {item["route"] for item in inventory})

    def test_handshake_uses_isolated_state_and_kills_descendant_group_on_timeout(self):
        pid_file = self.root / "child.pid"
        script = (
            "import pathlib, subprocess, sys, time; "
            f"p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(30)"
        )
        route = self.executable_route(args=["-c", script], version="fixture-1.2")
        approval = self.discovery.approve(route)
        result = self.discovery.handshake(route, approval["approval_id"], timeout=.2)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["process_group_terminated"])
        self.assertTrue(result["isolated_state"])
        end = time.monotonic() + 2
        while time.monotonic() < end and not pid_file.exists():
            time.sleep(.01)
        self.assertTrue(pid_file.exists())
        child_pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_handshake_approval_binds_environment_and_executable_identity(self):
        route = self.executable_route(env_overrides={"BOA_DISCOVERY_TEST": "one"})
        approval = self.discovery.approve(route)
        with self.assertRaises(Conflict):
            self.discovery.handshake({**route, "env_overrides": {"BOA_DISCOVERY_TEST": "two"}},
                                     approval["approval_id"], timeout=.2)
        changed = self.root / "changed-executable"
        changed.write_bytes(Path(sys.executable).read_bytes())
        changed.chmod(0o755)
        with self.assertRaises(Conflict):
            self.discovery.handshake({**route, "executable": str(changed)},
                                     approval["approval_id"], timeout=.2)

    def test_generative_requires_cost_policy_and_sends_one_bounded_request(self):
        requester = Mock(return_value="version fixture https://provider.example/v1 api_key=secret-value")
        route = self.executable_route(model="fixture-model")
        with self.assertRaises(Conflict):
            self.discovery.generative(route, requester=requester)
        self.assertEqual(requester.call_count, 0)
        approval = self.discovery.approve(route, "generative", cost_policy={"max_requests": 1, "operator": "test"})
        result = self.discovery.generative(route, approval_id=approval["approval_id"], requester=requester,
                                           output_tokens=32, timeout=1)
        self.assertEqual(requester.call_count, 1)
        self.assertEqual(result["request_count"], 1)
        self.assertNotIn("secret-value", json.dumps(self.store.discovery_evidence()))
        self.assertIn("provider.example/v1", json.dumps(self.store.discovery_evidence()))

    def test_probe_evidence_is_bounded_and_sanitized(self):
        self.assertIn("https://example.test/v1", sanitize_evidence(
            "x" * 1000 + " api_key=top-secret https://example.test/v1", 128))
        route = self.executable_route(model="fixture-model", version="v3")
        approval = self.discovery.approve(route, "generative", cost_policy="one request")
        result = self.discovery.generative(
            route, approval_id=approval["approval_id"],
            requester=lambda *args: "credential=do-not-store " + "x" * 1000)
        stored = json.dumps(self.store.discovery_evidence())
        self.assertNotIn("do-not-store", stored)
        self.assertLess(len(stored), 10000)
        self.assertEqual(result["metadata"]["version"], "v3")

    def test_unlisted_escape_is_recorded_per_invocation_and_not_persisted_as_approval(self):
        route = self.executable_route()
        with self.assertRaises(Conflict):
            self.discovery.handshake(route, timeout=.2)
        result = self.discovery.handshake(route, timeout=.2, allow_unlisted=True)
        self.assertEqual(result["status"], "completed")
        with self.assertRaises(Conflict):
            self.discovery.handshake(route, timeout=.2)
        self.assertEqual(self.store.discovery_approvals(), [])
        audit = self.store.discovery_audit()
        self.assertTrue(any(item["action"] == "unlisted_invocation" for item in audit))

    def test_only_credential_references_are_accepted(self):
        with self.assertRaises(Invalid):
            self.discovery.approve({**self.executable_route(), "api_key": "raw-secret"})
        route = {**self.executable_route(), "credential_ref": "env:BOA_API_KEY"}
        approval = self.discovery.approve(route)
        self.assertEqual(approval["route"], "fixture")
        stored = json.dumps(self.store.discovery_approvals())
        self.assertIn("env:BOA_API_KEY", stored)
        self.assertNotIn("raw-secret", stored)


if __name__ == "__main__":
    unittest.main()
