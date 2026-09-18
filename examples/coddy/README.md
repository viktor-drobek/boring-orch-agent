# Coddy `serve` integration

This example has two independent HTTP boundaries:

1. Coddy `serve` exposes its OpenAI-compatible `/v1` model API. A `boring-orch-agent` LLM worker calls it.
2. `boring-orch-agent serve` exposes its task API at `/api/v1`. Coddy, a script, or a person can submit and observe durable tasks there.

## Use Coddy as the worker’s model endpoint

Start Coddy after configuring at least one model in Coddy’s own configuration:

```bash
export CODDY_HTTP_TOKEN="replace-with-a-random-secret"
coddy serve --http --host 127.0.0.1 --port 12345 --auth-token "$CODDY_HTTP_TOKEN"

curl --fail-with-body http://127.0.0.1:12345/v1/models \
  -H "Authorization: Bearer $CODDY_HTTP_TOKEN"
```

Choose a model ID from that response, then initialize and start the orchestrator:

```bash
boring-orch-agent --home .boa init --workspace . --max-active 1
boring-orch-agent --home .boa manager

export BOA_PROVIDER=openai
export BOA_BASE_URL=http://127.0.0.1:12345/v1
export BOA_MODEL="model-id-from-coddy"
export BOA_API_KEY="$CODDY_HTTP_TOKEN"
boring-orch-agent --home .boa worker --id coddy-1 --runtime llm --slots 1
```

In another terminal, submit the sample:

```bash
boring-orch-agent --home .boa submit examples/coddy/coddy-task.json --key coddy-review-001
```

## Let Coddy or another HTTP client operate the orchestrator

Start the task API and pass its base URL and bearer token to the client that will call it:

```bash
export BOA_API_TOKEN="different-random-secret"
boring-orch-agent --home .boa serve --host 127.0.0.1 --port 8088
```

The client submits the exact task JSON and keeps the idempotency key stable when retrying:

```bash
curl --fail-with-body http://127.0.0.1:8088/api/v1/tasks \
  -H "Authorization: Bearer $BOA_API_TOKEN" \
  -H "Idempotency-Key: coddy-operated-001" \
  -H 'Content-Type: application/json' \
  --data @examples/coddy/coddy-task.json
```

Use the returned task ID to poll `GET /api/v1/tasks/{task_id}`. Coddy’s HTTP API and task-agent workflow vary by Coddy release, so configure its request tool to use this request shape rather than embedding either secret in a prompt. Coddy’s own Swagger page is available at `http://127.0.0.1:12345/docs/` while it is serving.
