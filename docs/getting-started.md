# Getting started

This guide starts a complete offline installation, then shows how to replace the demo worker with an LLM worker. It uses one workspace directory and one SQLite state directory.

## 1. Install and choose a workspace

Linux is required. The manager and worker read process identity from `/proc` and take `flock` locks; on another platform every long-running subcommand exits with "runs on Linux only" before touching the store.

```bash
git clone https://github.com/viktor-drobek/boring-agent.git
cd boring-agent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
mkdir -p ~/agent-workspace
```

The agent can only see files below the workspace path supplied at initialization. Keep job JSON files outside that directory when they contain operational metadata or credentials.

## 2. Initialize the durable store

```bash
boring-agent --home .boa init --workspace ~/agent-workspace --max-active 2
```

`.boa/agent.db` records commands, tasks, attempts, and events. Reuse it across manager and worker restarts.

## 3. Start the manager and an offline worker

Run these in separate terminals from the project directory:

```bash
# terminal 1
boring-agent --home .boa manager

# terminal 2
boring-agent --home .boa worker --id demo-1 --runtime demo --slots 1
```

## 4. Submit, observe, and retrieve a job

Use the included offline job. The stable key represents this one logical submission.

```bash
boring-agent --home .boa submit examples/demo-task.json --key demo-first-job
# copy task_id from the JSON receipt
boring-agent --home .boa status TASK_ID
boring-agent --home .boa wait TASK_ID --timeout 60
boring-agent --home .boa result TASK_ID
```

`submit` returns a receipt immediately after storage. `wait` returns `Succeeded`, `Failed`, `Cancelled`, or a nonterminal task whose observation is `Unknown`. Use `events TASK_ID` when diagnosing what happened. Each runner's output is kept in `.boa/logs/ATTEMPT_ID.log`; an attempt reported as `Runner lost` names that file in its error message, so read it before resolving the attempt.

## 5. Start API v1 for people or other agents

```bash
export BOA_API_TOKEN="choose-a-random-local-secret"
boring-agent --home .boa serve --host 127.0.0.1 --port 8088
```

Submit the same kind of document through `POST /api/v1/tasks`, including `Authorization: Bearer $BOA_API_TOKEN` and `Idempotency-Key`. See [`api-v1.md`](api-v1.md) for routes and [`job-descriptions.md`](job-descriptions.md) for job sources and templates.

## 6. Use an LLM worker

Stop the demo worker, configure a provider, and start an LLM worker. Provider credentials stay in the worker process environment; they never go in the task JSON.

```bash
export BOA_PROVIDER=openai
export BOA_BASE_URL=https://provider.example/v1
export BOA_MODEL=provider-model-id
export BOA_API_KEY=provider-api-key
boring-agent --home .boa worker --id llm-1 --runtime llm --slots 1
```

The worker asks an OpenAI-compatible server for JSON output (`response_format: {"type": "json_object"}`), which keeps a chatty model inside the one-action-per-turn envelope; `export BOA_JSON_MODE=off` disables it for servers that reject the field. A reply cut off at the output limit before any content arrived fails the attempt with a message naming `budget.output_tokens`; that is the signature of a reasoning model spending its whole budget on thinking. A server that reports no `usage` in its replies leaves the task's token accounting unknown, so do not set `budget.max_tokens` for such a provider: the next model call would be blocked as unverifiable.

Use a template from [`examples/jobs/`](../examples/jobs/) as the submission body.

For Coddy, use the dedicated Responses provider rather than the OpenAI
compatibility adapter:

```bash
export BOA_PROVIDER=coddy
export BOA_BASE_URL=http://127.0.0.1:12345/v1
export BOA_MODEL=model-id-from-coddy
export BOA_API_KEY="$CODDY_HTTP_TOKEN"
export BOA_PERMISSION_MODE=ask
boring-agent --home .boa worker --id coddy-1 --runtime llm --slots 1
```

Coddy streaming is enabled by default. The worker keeps a stable session ID for
the task, qualifies `BOA_MODEL` from `GET /v1/models` context metadata, and
performs `/compact` followed by `/rpa-init` unless a resumed session contains
explicit successful evidence for both commands in that order. Direct model
turns receive the task's `budget.output_tokens` as `max_output_tokens`. Coddy's
`agent`, `plan`, and `ask` profiles currently manage generation caps internally,
so per-request output caps do not apply to those profile turns. Follow
[`examples/coddy/README.md`](../examples/coddy/README.md) for session reuse and
subagent mention fields.
