"""Linux process identity and advisory locks for this single-host deployment."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path

from .model import Conflict


def identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{stat[19]}"
    except (OSError, IndexError):
        return None


def alive(pid, start):
    return bool(pid and start and identity(pid) == start)


@contextmanager
def lock(home, name):
    path = Path(home) / "locks" / hashlib.sha256(name.encode()).hexdigest()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Conflict(f"Already running: {name}") from exc
        yield
    finally:
        os.close(fd)
