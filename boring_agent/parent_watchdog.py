"""Independent parent-side supervision for native Coddy executions.

The parent owns this timer because a child cannot reliably observe its own
silence. Heartbeats and process liveness are deliberately not progress:
only an explicitly reported visible-progress event resets the idle clock.
"""
from dataclasses import dataclass
from typing import Callable, Optional


IDLE_SECONDS = 1800.0


@dataclass(frozen=True)
class ModelDecision:
    """A request for operator/model guidance after visible progress stopped."""

    event: str
    task_id: str
    attempt_id: str
    session_id: Optional[str]
    model: str
    idle_seconds: float
    remaining_deadline_seconds: Optional[float]
    automatic_switch: bool = False
    replay_started: bool = False
    child_cancelled: bool = False

    def as_dict(self):
        return {
            "event": self.event,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "session_id": self.session_id,
            "model": self.model,
            "idle_seconds": self.idle_seconds,
            "remaining_deadline_seconds": self.remaining_deadline_seconds,
            "automatic_switch": self.automatic_switch,
            "replay_started": self.replay_started,
            "child_cancelled": self.child_cancelled,
        }


class ParentIdleWatchdog:
    """Observe one native run without mutating or replaying that run.

    ``record_visible_progress`` is the only method that resets the timer.
    Callers should invoke it from the parent when a child emits user-visible
    progress or a validated execution artifact. Local heartbeats, polling,
    tool liveness, and receipt events must not be passed to it.
    """

    def __init__(self, clock: Callable[[], float], idle_seconds: float = IDLE_SECONDS):
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        self._clock = clock
        self.idle_seconds = float(idle_seconds)
        self._run = None
        self._last_visible_progress = None
        self._reported = False

    def start(self, *, task_id, attempt_id, model, session_id=None, deadline_at=None):
        now = self._clock()
        self._run = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "session_id": session_id,
            "model": model,
            "deadline_at": deadline_at,
        }
        self._last_visible_progress = now
        self._reported = False

    def record_visible_progress(self, *, at=None):
        if self._run is None:
            raise RuntimeError("watchdog has not been started")
        self._last_visible_progress = self._clock() if at is None else float(at)
        self._reported = False

    def poll(self, *, now=None):
        """Return one decision after strict idle threshold, otherwise ``None``."""
        if self._run is None:
            raise RuntimeError("watchdog has not been started")
        now = self._clock() if now is None else float(now)
        idle = now - self._last_visible_progress
        if self._reported or idle <= self.idle_seconds:
            return None
        self._reported = True
        deadline = self._run["deadline_at"]
        remaining = None if deadline is None else max(0.0, float(deadline) - now)
        return ModelDecision(
            event="NEEDS_MODEL_DECISION",
            task_id=self._run["task_id"],
            attempt_id=self._run["attempt_id"],
            session_id=self._run["session_id"],
            model=self._run["model"],
            idle_seconds=idle,
            remaining_deadline_seconds=remaining,
        )
