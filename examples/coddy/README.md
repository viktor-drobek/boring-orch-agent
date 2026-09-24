# Coddy `serve` integration

This example has two independent HTTP boundaries:

1. Coddy `serve` exposes the session-aware `POST /v1/responses` model API. A `boring-agent` LLM worker calls it.
2. `boring-agent serve` exposes the durable task API at `/api/v1`. Coddy, a script, or a person can submit and observe tasks there.

## Coddy transport order

When work uses Coddy from outside a Coddy session, prefer the HTTP Responses API (`coddy serve`, `POST /v1/responses`), then the Agent Client Protocol (`coddy acp`), then plain CLI prompts (`coddy -p`), in that order. Use a later transport only when every earlier one is unavailable or not configured for the project, and never switch transports to retry work whose outcome is `Unknown`. On the first run in a new project, before any other work, tell the operator which transport was selected and why each earlier one was not used, and ask the operator which permission mode and which model to use for the project. The permission mode may only narrow the current session's authority, and `bypass` is never offered. The chosen model is the project default only: a native job still runs with its own explicit `job.model`, and a missing or mismatched selector still blocks work. Do not start work until the operator has answered. A project is new when this agent has not run in it before, for example when its store home (`.boa` by default) does not exist yet. The legacy `coddy` provider implements only the API transport; ACP and plain CLI are agent-side choices, not runtimes of this package.

## Use Coddy as the worker provider

Start Coddy after configuring at least one model in Coddy itself:

```bash
export CODDY_HTTP_TOKEN="replace-with-a-random-secret"
coddy serve --http --host 127.0.0.1 --port 12345 --auth-token "$CODDY_HTTP_TOKEN"

curl --fail-with-body http://127.0.0.1:12345/v1/models \
  -H "Authorization: Bearer $CODDY_HTTP_TOKEN"
```

Initialize the orchestrator and start the manager:

```bash
boring-agent --home .boa init --workspace . --max-active 1
boring-agent --home .boa manager
```

Start a worker with the dedicated provider kind. `BOA_BASE_URL` may include
`/v1`; the adapter adds it when omitted.

```bash
export BOA_PROVIDER=coddy
export BOA_BASE_URL=http://127.0.0.1:12345/v1
export BOA_MODEL="model-id-from-coddy"
export BOA_API_KEY="$CODDY_HTTP_TOKEN"
export BOA_PERMISSION_MODE=ask
export BOA_CODDY_STREAM=on
boring-agent --home .boa worker --id coddy-1 --runtime llm --slots 1
```

In another terminal, submit the sample:

```bash
boring-agent --home .boa submit examples/coddy/coddy-task.json --key coddy-review-001
```

The worker first reads `GET /v1/models`; `BOA_MODEL` is used for warm-up only
when its advertised `max_context_tokens` is at least 100,000. It never invents
an unavailable model ID. The task then receives a deterministic `sess_…` ID. Every `POST /v1/responses`
request carries it in `X-Coddy-Session-ID`. Before the first work turn, the
worker runs `/compact` and `/rpa-init` once. A resumed session is adopted as
prepared only when its snapshot explicitly proves both commands succeeded in
that order through command records or adjacent user-command/assistant-success
pairs; unrelated existing messages do not skip warm-up. A missing snapshot, or
one that does not report `permissionMode`, starts at `ask` and cannot carry
inherited bypass permission. An unconfirmed warm-up command leaves the session
recovering until an operator confirms a retry. SSE is complete only after
`data: [DONE]` plus a nonblank string `finish_reason` or
`coddy_meta.stop_reason`; a broken or malformed stream is retained as an
unconfirmed outcome.

Direct model turns receive `budget.output_tokens` as `max_output_tokens`.
Coddy's `agent`, `plan`, and `ask` profiles currently control generation limits
internally, so commands and subagent turns do not honor that per-request cap.

To resume a prepared Coddy session, add an exact mention:

```json
"coddy": {
  "session": "@session:sess_0123456789abcdef01234567",
  "permission_mode": "accept_edits",
  "stream": true
}
```

To delegate the work to a Coddy subagent, add `coddy.mention`. The worker sends
`@agent:<name>` and the complete `spawn_agent` arguments through the same
session connection:

```json
"coddy": {
  "permission_mode": "accept_edits",
  "mention": {
    "agent": "exec",
    "prompt": "Review the workspace and return the requested JSON result.",
    "description": "Review workspace",
    "background": true,
    "expected_seconds": 120,
    "timeout_seconds": 600,
    "model": "model-id-from-coddy",
    "reasoning": "high",
    "notify_on_finish": true,
    "permission_mode": "ask"
  }
}
```

The explicit child permission mode may narrow the parent mode but cannot widen
it. Detached permission prompts remain attached to the parent Coddy session.
Never put the Coddy URL or bearer token in task JSON.

## Let Coddy or another client operate the task API

Start the task API with a different secret:

```bash
export BOA_API_TOKEN="different-random-secret"
boring-agent --home .boa serve --host 127.0.0.1 --port 8088
```

Submit the exact task document and keep its idempotency key stable when retrying
a lost client response:

```bash
curl --fail-with-body http://127.0.0.1:8088/api/v1/tasks \
  -H "Authorization: Bearer $BOA_API_TOKEN" \
  -H "Idempotency-Key: coddy-operated-001" \
  -H 'Content-Type: application/json' \
  --data @examples/coddy/coddy-task.json
```

Use the returned task ID with `GET /api/v1/tasks/{task_id}` and
`GET /api/v1/tasks/{task_id}/result`. Coddy's own API documentation is at
`http://127.0.0.1:12345/docs/` while it is serving.
