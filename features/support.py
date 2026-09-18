"""Scenario lifecycle and fault injection, using the production Store/Manager/runner.

SQL is restricted to deliberate fault setup and elapsed-time fixtures. Expected
outcomes are checked through the public Store/CLI/result interfaces in steps.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from boring_agent import manager as manager_module
from boring_agent.manager import Manager
from boring_agent.model import AgentError
from boring_agent.process import identity
from boring_agent.runner import run_attempt
from boring_agent.store import Store


class AgentFixture:
    def __init__(self, resources):
        self.resources = resources
        self.root = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="boa-feature-")))
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = Store(self.root / "state")
        self.store.initialize(self.workspace)
        self.manager = Manager(self.store)
        self.raw = {"objective": "Produce an auditable result", "runtime": "demo", "demo": {"delay_seconds": 0}}
        self.task_id = None
        self.error = None
        self.value = None
        self.processes = []
        resources.callback(self.stop_processes)

    def capture(self, operation):
        self.error = None
        try:
            self.value = operation()
        except AgentError as exc:
            self.error = exc
        return self.value

    def submit(self, key="scenario", **changes):
        self.raw = {**self.raw, **changes}
        self.key = key
        self.receipt = self.store.submit(self.raw, key)
        self.task_id = self.receipt["task_id"]
        return self.receipt

    def task(self):
        return self.store.task(self.task_id)

    def attempt(self):
        return self.task()["attempts"][-1]

    def tick(self, at=None):
        if at is None:
            self.manager.tick()
        else:
            # Advance only the manager's clock. Never globally patch Python's time module.
            with patch.object(manager_module, "time", SimpleNamespace(time=lambda: at)):
                self.manager.tick()

    def deliver(self):
        attempt = self.attempt()
        run_attempt(self.store, attempt["id"], attempt["worker_id"])

    def running(self):
        self.tick()
        attempt = self.attempt()
        self.store.observe(attempt["id"], attempt["worker_id"], 1, "Launching",
                           runner_pid=os.getpid(), runner_start=identity(os.getpid()))
        self.store.observe(attempt["id"], attempt["worker_id"], 2, "Running", tokens=0)
        self.tick()

    def observe(self, state, **fields):
        attempt = self.attempt()
        return self.store.observe(attempt["id"], attempt["worker_id"], attempt["sequence"] + 1, state, **fields)

    def cli(self, *args, payload=None, code=0):
        command = [sys.executable, "-m", "boring_agent", "--home", str(self.store.home), *args]
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == code, result.stdout + result.stderr
        return json.loads(result.stdout if code != 2 else result.stderr)

    def start_processes(self):
        for args in (("manager",), ("worker", "--id", "process-worker", "--runtime", "demo")):
            log = self.resources.enter_context((self.root / f"{args[0]}.log").open("w+"))
            self.processes.append(subprocess.Popen(
                [sys.executable, "-m", "boring_agent", "--home", str(self.store.home), *args],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log))

    def stop_processes(self):
        # All real subprocess scenarios wait for a terminal task; on failure also cancel
        # any unfinished work before stopping worker delivery and removing its store.
        if self.processes:
            for task in self.store.tasks():
                if task["status"] not in ("Succeeded", "Failed", "Cancelled"):
                    self.store.cancel(task["id"], "cleanup-" + task["id"])
            end = time.monotonic() + 3
            while time.monotonic() < end:
                self.manager.tick()
                if all(t["status"] in ("Succeeded", "Failed", "Cancelled") for t in self.store.tasks()):
                    break
                time.sleep(.02)
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self.processes.clear()
