# Native exec supervision

Native operational jobs are launched by the parent through Coddy `exec`. The
parent reads the immutable job document, selects its explicit `model`, and
observes the child independently. A receipt, local heartbeat, tool liveness,
or a quiet poll is not visible model progress.

## Parent idle watchdog

Use `boring_agent.parent_watchdog.ParentIdleWatchdog` for every native run. The
parent must create one instance per run and start it with the task, attempt,
session, model, and absolute deadline. Only a parent observation of visible
child progress may reset the timer:

```python
watchdog = ParentIdleWatchdog(time.monotonic)
watchdog.start(
    task_id=job["id"],
    attempt_id=run["attempt_id"],
    session_id=run.get("session_id"),
    model=job["model"],
    deadline_at=run["deadline_monotonic"],
)

while child_is_running:
    output = read_parent_visible_output()
    if output.has_visible_progress:
        watchdog.record_visible_progress()

    decision = watchdog.poll()
    if decision is not None:
        emit(decision.as_dict())
        break
```

The threshold is strict: escalation occurs only after more than 1800 seconds
without visible progress. The watchdog emits exactly one
`NEEDS_MODEL_DECISION` event per idle episode and includes task, attempt,
session, model, idle duration, and remaining deadline. It never switches the
model, cancels the child, starts a replay, or resolves an `Unknown` outcome.
After visible progress resumes, a later idle period is a new episode.

If parent observation is unavailable, report `HANDOFF` rather than claiming
that idle supervision happened. A child must not implement a replacement
watchdog for itself.

## Adding a job

A new operational job needs an immutable source document and an authoritative
native copy. Both must contain an explicit model selector and a runtime of
`acp` or `coddy_native`; the queue entry starts as `Pending`, with
`execution_authorized: false`, `native_attempt_count: 0`, and no timestamps.
Creating a queue entry does not launch it. Launch only after the parent has
verified readiness, dependencies, model selection, and a free native slot;
then call the `exec` sub-agent with `model=job["model"]` and observe it with
the watchdog above.

## Native session lifecycle

A native job may carry an exact `@session:<id>` mention. Coddy resolves that
mention to a read-only digest attachment; it is not a live child connection and
its serialized attachment is limited to 24 KiB. The durable lifecycle stores
session IDs, lineage, job links, run history, transfer records, branch records,
and recovery evidence in `lifecycle_*` tables in the existing SQLite store.

A first run of a new session must execute `/compact` and then `/rpa-init` with a
model whose context window is at least 100,000 tokens. The selected default is
`ndsub/qwen3.8-27b` (262,144 tokens), unless the requested model is already
qualified. Warm-up time consumes the job deadline. Each command has a stable
session-scoped idempotency key; a recorded successful command is never run
again. A crash while a command is externally running becomes `recovering` and
requires an operator-confirmed retry with the same key.

Completed jobs transfer only their recorded, validated result and session
mention to dependents. A single sequential dependent may reuse the completed
session. When one parent has multiple dependents, every dependent gets an
independent child session with the same lineage; no live session is used
concurrently. A restart marks an in-flight run or uncertain transfer as
`unknown`/`recovering`, retains the diagnostic evidence, and never replays it
automatically. Legacy `llm`/`demo` tasks and their SQLite history remain
separate from this native lifecycle.
