"""Invariants behind the HTTP discovery-consent and API error-shape review fixes."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from boring_agent.api import _loopback_host_header
from boring_agent.discovery import Discovery, ensure_schema
from boring_agent.model import Conflict
from boring_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent


class ReviewDiscoveryInvariants(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "workspace").mkdir()
        self.store = Store(root / "state")
        self.store.initialize(root / "workspace")

    def tearDown(self):
        self.temp.cleanup()

    def test_ensure_schema_keeps_the_callers_transaction_open(self):
        with self.store.transaction() as db:
            self.assertTrue(db.in_transaction)
            ensure_schema(db)
            self.assertTrue(db.in_transaction, "ensure_schema committed the caller's BEGIN IMMEDIATE")

    def test_approval_binds_the_environment_fallback_base_url(self):
        # A route without base_url uses the operator's BOA_BASE_URL. Moving that
        # endpoint must invalidate the approval before any credential is sent.
        route = {"id": "fallback", "executable": sys.executable, "model": "fixture-model",
                 "credential_ref": "env:BOA_REVIEW_KEY"}
        requests = []
        requester = lambda *args: requests.append(args) or "ok"
        with patch.dict(os.environ, {"BOA_BASE_URL": "http://127.0.0.1:1/v1", "BOA_REVIEW_KEY": "value"}):
            approval = Discovery(self.store).approve(route, "generative", cost_policy="one request")
        with patch.dict(os.environ, {"BOA_BASE_URL": "https://elsewhere.example/v1", "BOA_REVIEW_KEY": "value"}):
            with self.assertRaises(Conflict):
                Discovery(self.store).generative(route, approval_id=approval["approval_id"], requester=requester)
        self.assertEqual(requests, [])

    def test_handshake_rejects_non_integer_bounds_before_running(self):
        route = {"id": "bounds", "executable": sys.executable, "args": ["--version"]}
        approval = Discovery(self.store).approve(route)
        for value in ("x", True, 64.5):
            with self.subTest(value=value), self.assertRaises(Exception) as caught:
                Discovery(self.store).handshake(route, approval["approval_id"], max_output_bytes=value)
            self.assertEqual(type(caught.exception).__name__, "Invalid")
        self.assertEqual(self.store.discovery_evidence(), [])


class ReviewApiInvariants(unittest.TestCase):
    def test_loopback_host_header(self):
        for value in ("127.0.0.1:8088", "localhost", "LOCALHOST:1", "[::1]:8088", "127.0.0.2"):
            self.assertTrue(_loopback_host_header(value), value)
        for value in ("", "attacker.example", "127.0.0.1.attacker.example", "localhost.attacker.example",
                      "[::1", "10.0.0.1:8088"):
            self.assertFalse(_loopback_host_header(value), value)

    def test_api_document_lists_the_native_job_route_and_operator_only_approval(self):
        text = (ROOT / "docs" / "api-v1.md").read_text()
        self.assertIn("`/api/v1/native/jobs/{job_id}`", text)
        self.assertIn("operator_only", text)


if __name__ == "__main__":
    unittest.main()
