# boring-orch-agent

`boring-orch-agent` is a durable, local manager-worker orchestrator for bounded agent tasks. It accepts an immutable task document, records the command in SQLite, dispatches it to a compatible worker, and retains the task history and validated result. It is deliberately small enough to inspect and run on one machine.

It applies the ideas in Tim Boring’s [*Build an Orchestrator in Go (From Scratch)*](https://books.google.com/books?vid=ISBN9781617299759) to local agent work. This Python implementation is independent software; it does not include the book or its source code.

## For people

Install Python 3.11+ and create an environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install boring-orch-agent
```

The offline demo performs one safely replayed failure followed by a successful result. It calls no model provider:

```bash
boring-orch-agent --home /tmp/boa-demo demo
```

For a durable service, initialize it once and keep the manager and worker in separate terminals:

```bash
# terminal 1
boring-orch-agent --home .boa init --workspace . --max-active 2
boring-orch-agent --home .boa manager

# terminal 2 — offline worker for the included task example
boring-orch-agent --home .boa worker --id demo --runtime demo --slots 1

# terminal 3
boring-orch-agent --home .boa submit examples/demo-task.json --key demo-001
```

The reply is a durable receipt. Use the returned `task_id` with `status`, `events`, `wait`, and `result`. A stable idempotency key makes a retried submission return the same receipt instead of creating more work.

For LLM work, set the operator-owned provider configuration before starting an `llm` worker:

```bash
export BOA_PROVIDER=openai
export BOA_BASE_URL=https://provider.example/v1
export BOA_MODEL=provider-model-id
export BOA_API_KEY=provider-api-key
boring-orch-agent --home .boa worker --id llm-1 --runtime llm --slots 1
```

The built-in file tools are `list_files` and `read_file`. A task can write only when the installation and worker both explicitly enable `workspace-write`; no shell tool is exposed. Read [the API contract](docs/api-v1.md) before placing the service behind another process or network boundary.

## For agents and integrations

Run the task API beside the manager and workers:

```bash
export BOA_API_TOKEN="replace-with-a-random-secret"
boring-orch-agent --home .boa serve --host 127.0.0.1 --port 8088
```

The API is JSON at `/api/v1`. Every mutable request requires `Authorization: Bearer …` and an `Idempotency-Key`. A receipt means the command is stored; use a task read or result read to learn the execution outcome.

```bash
curl --fail-with-body http://127.0.0.1:8088/api/v1/tasks \
  -H "Authorization: Bearer $BOA_API_TOKEN" \
  -H "Idempotency-Key: review-2026-001" \
  -H 'Content-Type: application/json' \
  --data @examples/api/submit-demo.json
```

The returned `task_id` can be read from `GET /api/v1/tasks/{task_id}`. See [docs/api-v1.md](docs/api-v1.md) for the complete route and error contract, and [examples/coddy/README.md](examples/coddy/README.md) for using Coddy in `serve` mode through its OpenAI-compatible `/v1` API.

## Coddy as the model provider

Coddy’s `serve` command exposes an OpenAI-compatible `/v1` API. Start Coddy with a bearer token, then point an `llm` worker at it:

```bash
export CODDY_HTTP_TOKEN="replace-with-a-random-secret"
coddy serve --http --host 127.0.0.1 --port 12345 --auth-token "$CODDY_HTTP_TOKEN"

export BOA_PROVIDER=openai
export BOA_BASE_URL=http://127.0.0.1:12345/v1
export BOA_MODEL="your-coddy-model-id"
export BOA_API_KEY="$CODDY_HTTP_TOKEN"
boring-orch-agent --home .boa worker --id coddy-1 --runtime llm --slots 1
```

Confirm the model identifier from Coddy with `GET /v1/models` or its `/docs/` page. `boring-orch-agent` sends OpenAI-compatible chat-completion requests; Coddy owns its own model, tool, and permission configuration. The runnable files and two-direction integration steps are in [examples/coddy](examples/coddy/README.md).

## Development and release

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
