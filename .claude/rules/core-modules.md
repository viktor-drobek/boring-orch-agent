---
paths: ["boring_agent/model.py", "boring_agent/store.py", "boring_agent/manager.py", "boring_agent/worker.py", "boring_agent/runner.py"]
---

# Core modules

Task identity, command identity, idempotency, attempt ownership, reservations, and state transitions are durable invariants. Do not infer a completed remote action after a crash or timeout. Preserve monotonic observations and the distinction between terminal task state and an attempt artifact retained for diagnostics.
