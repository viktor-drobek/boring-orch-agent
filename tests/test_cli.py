import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from boring_agent.process import lock
from boring_agent.store import Store


class CLITests(unittest.TestCase):
    def test_independent_processes_submit_wait_result_cancel_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = [sys.executable, "-m", "boring_agent", "--home", str(Path(directory) / "state")]

            def cli(*args, payload=None, code=0):
                result = subprocess.run([*prefix, *args], input=json.dumps(payload) if payload is not None else None,
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, code, result.stderr + result.stdout)
                return json.loads(result.stdout if code != 2 else result.stderr)

            cli("init", "--workspace", directory)
            processes = [subprocess.Popen([*prefix, "manager"]),
                         subprocess.Popen([*prefix, "worker", "--id", "demo", "--runtime", "demo"])]
            try:
                receipt = cli("submit", "-", "--key", "first", payload={"objective": "hello", "runtime": "demo"})
                task_id = receipt["task_id"]
                task = cli("wait", task_id, "--timeout", "5")
                self.assertEqual(task["status"], "Succeeded")
                self.assertTrue(cli("result", task_id)["demo"])
                self.assertTrue(cli("events", task_id))
                self.assertEqual(cli("capacity")["used"], 0)
                # Process restart preserves task identity and durable receipt.
                processes[0].terminate()
                processes[0].wait(timeout=3)
                processes[0] = subprocess.Popen([*prefix, "manager"])
                duplicate = cli("submit", "-", "--key", "first", payload={"objective": "hello", "runtime": "demo"})
                self.assertTrue(duplicate["duplicate"])
                self.assertEqual(duplicate["task_id"], task_id)
                self.assertEqual(cli("status", task_id)["status"], "Succeeded")
                slow_id = cli("submit", "-", "--key", "slow", payload={"objective": "slow", "runtime": "demo", "demo": {"delay_seconds": 5}})["task_id"]
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    if cli("status", slow_id)["status"] == "Running":
                        break
                    time.sleep(.05)
                else:
                    self.fail("Slow task never reached Running")
                cli("cancel", slow_id, "--key", "stop")
                self.assertEqual(cli("wait", slow_id, "--timeout", "5", code=1)["status"], "Cancelled")
                error = cli("result", slow_id, code=2)
                self.assertEqual(error["error"], "invalid_request")
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=3)

    def test_offline_demo_from_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "boring_agent", "--home", directory, "demo"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "Succeeded")
            self.assertEqual(payload["attempts"], 2)

    def test_demo_conflict_does_not_leave_an_unreported_task(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state")
            store.initialize(directory)
            with lock(store.home, "manager"):
                result = subprocess.run([sys.executable, "-m", "boring_agent", "--home", str(store.home), "demo"],
                                        capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(store.tasks(), [])


if __name__ == "__main__":
    unittest.main()
