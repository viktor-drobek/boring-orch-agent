import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from boring_agent.api import make_server
from boring_agent.model import Invalid
from boring_agent.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / "state")
        self.store.initialize(root)
        self.server = make_server(self.store, port=0, auth_token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload)
        headers = {"Authorization": "Bearer test-token", **(headers or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    def test_health_requires_bearer_authentication(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        self.assertEqual(response.getheader("WWW-Authenticate"), 'Bearer realm="boring-orch-agent"')
        connection.close()
        status, value = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(value["status"], "ok")

    def test_submit_is_durable_idempotent_and_queryable(self):
        spec = {"objective": "An API task", "runtime": "demo"}
        status, receipt = self.request("POST", "/api/v1/tasks", spec, {"Idempotency-Key": "api-001"})
        self.assertEqual(status, 202)
        task_id = receipt["task_id"]
        status, duplicate = self.request("POST", "/api/v1/tasks", spec, {"Idempotency-Key": "api-001"})
        self.assertEqual(status, 202)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(task_id, duplicate["task_id"])
        status, task = self.request("GET", f"/api/v1/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(task["status"], "Pending")
        status, events = self.request("GET", f"/api/v1/tasks/{task_id}/events")
        self.assertEqual(status, 200)
        self.assertEqual(events["events"][0]["kind"], "task.accepted")

    def test_invalid_or_missing_idempotency_key_is_rejected(self):
        status, value = self.request("POST", "/api/v1/tasks", {"objective": "no key", "runtime": "demo"})
        self.assertEqual(status, 400)
        self.assertEqual(value["error"], "invalid_request")
        status, value = self.request("POST", "/api/v1/tasks", {"objective": "bad", "runtime": "not-a-runtime"},
                                     {"Idempotency-Key": "bad-spec"})
        self.assertEqual(status, 400)
        self.assertEqual(value["error"], "invalid_request")

    def test_cancel_has_its_own_idempotency_key(self):
        _, receipt = self.request("POST", "/api/v1/tasks", {"objective": "cancel", "runtime": "demo"},
                                  {"Idempotency-Key": "submit"})
        task_id = receipt["task_id"]
        status, cancelled = self.request("POST", f"/api/v1/tasks/{task_id}/cancel", {}, {"Idempotency-Key": "cancel"})
        self.assertEqual(status, 202)
        self.assertFalse(cancelled["duplicate"])
        status, duplicate = self.request("POST", f"/api/v1/tasks/{task_id}/cancel", {}, {"Idempotency-Key": "cancel"})
        self.assertEqual(status, 202)
        self.assertTrue(duplicate["duplicate"])
        _, task = self.request("GET", f"/api/v1/tasks/{task_id}")
        self.assertEqual(task["status"], "Cancelled")

    def test_remote_listener_without_a_token_is_refused_before_bind(self):
        with self.assertRaises(Invalid):
            make_server(self.store, host="0.0.0.0", port=8089)


if __name__ == "__main__":
    unittest.main()
