"""Repeated delivery to independently living, durably deduplicated attempt runners."""
import os
from pathlib import Path
import subprocess
import sys
import time


class Worker:
    # A runner that exits without claiming is relaunched with exponential backoff, so a
    # broken environment costs one process per half-second at first and one per 30 s at most.
    BACKOFF_START, BACKOFF_MAX = .5, 30.0

    def __init__(self, store, worker_id, runtimes, slots=1, allow_write=False):
        self.store, self.id = store, worker_id
        store.register_worker(worker_id, runtimes, slots, allow_write)
        self.children = {}
        self.launches = {}  # attempt_id -> (launch count, earliest next launch on the monotonic clock)
        self.logs = store.home / "logs"
        self.logs.mkdir(exist_ok=True, mode=0o700)

    def tick(self):
        self.store.heartbeat_worker(self.id)
        with self.store.reading() as db:
            queued = [row["id"] for row in db.execute(
                """SELECT a.id FROM attempts a JOIN outbox o ON o.attempt_id=a.id
                   WHERE o.worker_id=? AND a.state='Queued'""", (self.id,))]
        now = time.monotonic()
        for attempt_id in queued:
            existing = self.children.get(attempt_id)
            if existing and existing.poll() is None:
                continue
            count, not_before = self.launches.get(attempt_id, (0, 0.0))
            if now < not_before:
                continue
            self.children[attempt_id] = self.launch(attempt_id, count + 1)
            self.launches[attempt_id] = (count + 1, now + min(self.BACKOFF_MAX, self.BACKOFF_START * 2 ** count))
        # Reap finished children without terminating runners when the worker exits.
        self.children = {key: child for key, child in self.children.items() if child.poll() is None}
        self.launches = {key: value for key, value in self.launches.items() if key in queued}

    def launch(self, attempt_id, count):
        env = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
        # The runner's stderr, including any traceback behind a later "Runner lost", is kept here.
        fd = os.open(self.logs / f"{attempt_id}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] worker {self.id} launch {count} of runner for attempt {attempt_id}\n".encode())
            return subprocess.Popen(
                [sys.executable, "-m", "boring_agent.runner", "--home", str(self.store.home),
                 "--attempt", attempt_id, "--worker", self.id], env=env,
                stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True)
        finally:
            os.close(fd)
