# boring-agent

`boring-agent` is a durable, local manager-worker orchestrator for bounded agent tasks. It accepts an immutable task document, records the command in SQLite, dispatches it to a compatible worker, and retains the task history and validated result. It is deliberately small enough to inspect and run on one machine.

The distribution and primary command are named `boring-agent`. Existing installations may continue to use the `boring-orch-agent` console-script alias.

It applies the ideas in Tim Boring’s [*Build an Orchestrator in Go (From Scratch)*](https://books.google.com/books?vid=ISBN9781617299759) to local agent work. This Python implementation is independent software; it does not include the book or its source code.

Start with the complete [getting-started guide](docs/getting-started.md), then choose a versioned task template from [examples/jobs](examples/jobs/README.md). [Job descriptions and sample scenarios](docs/job-descriptions.md) explain how a person, Coddy, or another trusted integration turns an approved ticket or workflow into a durable task.

## For people

The orchestrator runs on **Linux only**: process identity comes from `/proc` and singleton locks from `flock`, and `manager`, `worker`, `serve` and `demo` refuse to start elsewhere with a clear message. Install Python 3.11+ and create an environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install boring-agent
```

The offline demo performs one safely replayed failure followed by a successful result. It calls no model provider:

```bash
boring-agent --home /tmp/boa-demo demo
```

For a durable service, initialize it once and keep the manager and worker in separate terminals:

```bash
# terminal 1
boring-agent --home .boa init --workspace . --max-active 2
boring-agent --home .boa manager

# terminal 2 — offline worker for the included task example
boring-agent --home .boa worker --id demo --runtime demo --slots 1

# terminal 3
boring-agent --home .boa submit examples/demo-task.json --key demo-001
```

The reply is a durable receipt. Use the returned `task_id` with `status`, `events`, `wait`, and `result`. A stable idempotency key makes a retried submission return the same receipt instead of creating more work.

For LLM work, set the operator-owned provider configuration before starting an `llm` worker:

```bash
export BOA_PROVIDER=openai
export BOA_BASE_URL=https://provider.example/v1
export BOA_MODEL=provider-model-id
export BOA_API_KEY=provider-api-key
boring-agent --home .boa worker --id llm-1 --runtime llm --slots 1
```

The built-in file tools are `list_files` and `read_file`. A task can write only when the installation and worker both explicitly enable `workspace-write`; no shell tool is exposed. Read [the API contract](docs/api-v1.md) before placing the service behind another process or network boundary.

## For agents and integrations

Run the task API beside the manager and workers:

```bash
export BOA_API_TOKEN="replace-with-a-random-secret"
boring-agent --home .boa serve --host 127.0.0.1 --port 8088
```

The API is JSON at `/api/v1`. Every mutable request requires `Authorization: Bearer …` and an `Idempotency-Key`. A receipt means the command is stored; use a task read or result read to learn the execution outcome.

```bash
curl --fail-with-body http://127.0.0.1:8088/api/v1/tasks \
  -H "Authorization: Bearer $BOA_API_TOKEN" \
  -H "Idempotency-Key: review-2026-001" \
  -H 'Content-Type: application/json' \
  --data @examples/api/submit-demo.json
```

The returned `task_id` can be read from `GET /api/v1/tasks/{task_id}`. See [docs/api-v1.md](docs/api-v1.md) for the complete route and error contract, and [examples/coddy/README.md](examples/coddy/README.md) for using Coddy through its session-aware Responses API.

## Coddy as the model provider

Coddy’s `serve` command exposes `POST /v1/responses`. Start Coddy with a bearer token, then select the dedicated `coddy` provider:

```bash
export CODDY_HTTP_TOKEN="replace-with-a-random-secret"
coddy serve --http --host 127.0.0.1 --port 12345 --auth-token "$CODDY_HTTP_TOKEN"

export BOA_PROVIDER=coddy
export BOA_BASE_URL=http://127.0.0.1:12345/v1
export BOA_MODEL="your-coddy-model-id"
export BOA_API_KEY="$CODDY_HTTP_TOKEN"
export BOA_PERMISSION_MODE=ask
export BOA_CODDY_STREAM=on
boring-agent --home .boa worker --id coddy-1 --runtime llm --slots 1
```

The worker assigns each task a stable `sess_…` ID, sends it in `X-Coddy-Session-ID`, and consumes SSE by default. A new session runs `/compact` and `/rpa-init` once before work. A task can resume a prepared session with `coddy.session`, disable streaming with `coddy.stream`, or delegate through `coddy.mention`; child permission mode is inherited and may only be narrowed. The runnable task and full field reference are in [examples/coddy](examples/coddy/README.md) and [job descriptions](docs/job-descriptions.md).

## Development and release

Agents can use the canonical [project instructions](AGENTS.md). Cursor, Claude Code, Codex, and Coddy-specific rule files are versioned with the repository; see [the rule trees](AGENTS.md#rules-sync) for their synchronization contract.

```bash
python -m pip install -e '.[dev]'
python tools/pipeline.py
python tools/version.py check v0.1.0
```

The pipeline runs Gherkin acceptance features first, then unit/regression tests, source and wheel builds, and an installed-wheel demo. GitHub Actions runs the same pipeline on each push and pull request. A pushed, validated `vX.Y.Z` tag creates a GitHub release with the sdist and wheel; [`tools/version.py`](tools/version.py) updates and verifies the package version.

## Design boundaries

The local SQLite database is the command and task source of truth. Manager and worker processes can be restarted; the manager reconciles durable state. A request whose remote execution cannot be proven stopped becomes `Unknown` and is not automatically replayed. This is a local orchestrator, not a distributed consensus system or an OS sandbox.

## License

MIT. See [LICENSE](LICENSE).
