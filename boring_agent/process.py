"""Linux process identity and advisory locks for this single-host deployment."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path

from .model import Conflict

try:  # fcntl does not exist on Windows; importing this module must still succeed there.
    import fcntl
except ImportError:  # pragma: no cover - exercised through supported() on other platforms
    fcntl = None


def supported() -> str | None:
    """Return why this host cannot run managers or workers, or None when it can.

    Process identity comes from /proc and the boot ID, and singleton locks from
    flock. Without them a runner would fail its first claim with a confusing
    "identity unavailable", so the loops refuse to start instead.
    """
    if fcntl is None:
        return "flock advisory locking is unavailable"
    if not Path("/proc/self/stat").is_file() or not Path("/proc/sys/kernel/random/boot_id").is_file():
        return "/proc process identity is unavailable"
    return None


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
