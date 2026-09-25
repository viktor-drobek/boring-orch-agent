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

## Operator driver for Coddy serve

`tools/coddy_driver` is the operator side of an unattended native run over the
Coddy Responses API. It starts the parent session and supervises it; the job
itself still runs inside Coddy, where the parent delegates through
`spawn_agent`. It never submits to the manager, starts workers, nests `coddy`
processes or calls a model provider.

```bash
tools/coddy_driver/run_job.sh launch JOB.json PARENT_PROMPT.md
tools/coddy_driver/run_job.sh status JOB.json
tools/coddy_driver/run_job.sh attach JOB.json SESSION_ID
tools/coddy_driver/run_job.sh stop-serve
```

`run_job.sh` starts (or reuses) `coddy serve` on `127.0.0.1` with a generated
bearer token passed through the environment, and runs the driver; both are
detached with `setsid`, so a run survives the terminal or agent session that
started it. A server that dies takes its in-process children with it, which is
why it must not live inside a supervising session.

The driver validates the job with `validate_native_job` and takes model,
workspace, permission mode and interpreter from it. It creates a fresh session
with one bootstrap turn, pins `permissionMode`, `mode` and `selectedModelId`
with `PATCH /coddy/sessions/{id}` before the job turn, and streams that turn.
It then follows woken turns on the composer stream and detached children on
`GET /coddy/events`, answering every permission prompt with
`tools/coddy_driver/policy.py`: reads, read-only `git` and the project
interpreter's `scripts/check_*`, `scripts/validate_*`, `-m behave` and
`-m unittest` are allowed; redirection, background jobs, subshells, command
substitution, inline Python, writes outside the workspace and deletes are
rejected, and a prompt whose arguments cannot be read is rejected.

It feeds new child output to `ParentIdleWatchdog` as visible progress, polls it
only while a child is running, and hands `NEEDS_MODEL_DECISION` to the parent
(queued to its turn, or as a new turn). It records the outcome in
`<job dir>/<job id>/state.json`: `unknown` when a turn ended without
`data: [DONE]`, a child was still running, or the server was lost (repeated
connection failures; an HTTP error means the server is up), otherwise the
status the parent reported. `attach` to a session the server does not know
exits with status 3 and outcome `session-unknown`.

## Adding a job

A new operational job needs an immutable source document and an authoritative
native copy. Both must contain an explicit model selector and a runtime of
`acp` or `coddy_native`, an explicit absolute `workspace` that is not the store
home, and a permission mode that asks (`bypass` is refused). The registered
`lifecycle_jobs` row starts in state `pending` (or `ready` when it has no
dependencies) with `attempt_count: 0` and no active run. Creating the entry
does not launch it. Launch only after the parent has
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
again. A crash while a command is externally running, or an executor error whose
outcome is `unknown` or unclassified (for example a stream without
`data: [DONE]`), becomes `recovering` and requires an operator-confirmed retry
with the same key. Only a confirmed rejection (`permanent`, `transient`,
`validation`, `cancelled`, or an explicit `False`) marks the warm-up `failed`.
A job is never claimed on a session that has not recorded both successful
steps; a failed or recovering warm-up must be retried first.

Completed jobs transfer only their recorded, validated result and session
mention to dependents. A single sequential dependent may reuse the completed
session. When one parent has multiple dependents, every dependent gets an
independent child session with the same lineage; no live session is used
concurrently. A dependent that names its own `@session:<id>` keeps that session
and becomes `ready` once its dependencies succeed; jobs that share it still run
only one at a time. A restart marks an in-flight run or uncertain transfer as
`unknown`/`recovering`, retains the diagnostic evidence, and never replays it
automatically. Legacy `llm`/`demo` tasks and their SQLite history remain
separate from this native lifecycle.
