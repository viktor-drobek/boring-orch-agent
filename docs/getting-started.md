# Getting started

This guide starts a complete offline installation, then shows how to replace the demo worker with an LLM worker. It uses one workspace directory and one SQLite state directory.

## 1. Install and choose a workspace

```bash
git clone https://github.com/viktor-drobek/boring-orch-agent.git
cd boring-orch-agent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
mkdir -p ~/agent-workspace
```

The agent can only see files below the workspace path supplied at initialization. Keep job JSON files outside that directory when they contain operational metadata or credentials.

## 2. Initialize the durable store

```bash
boring-orch-agent --home .boa init --workspace ~/agent-workspace --max-active 2
```

`.boa/agent.db` records commands, tasks, attempts, and events. Reuse it across manager and worker restarts.

## 3. Start the manager and an offline worker

Run these in separate terminals from the project directory:

```bash
# terminal 1
boring-orch-agent --home .boa manager

# terminal 2
boring-orch-agent --home .boa worker --id demo-1 --runtime demo --slots 1
```

## 4. Submit, observe, and retrieve a job

Use the included offline job. The stable key represents this one logical submission.

```bash
boring-orch-agent --home .boa submit examples/demo-task.json --key demo-first-job
# copy task_id from the JSON receipt
boring-orch-agent --home .boa status TASK_ID
boring-orch-agent --home .boa wait TASK_ID --timeout 60
boring-orch-agent --home .boa result TASK_ID
```

`submit` returns a receipt immediately after storage. `wait` returns `Succeeded`, `Failed`, `Cancelled`, or a nonterminal task whose observation is `Unknown`. Use `events TASK_ID` when diagnosing what happened. Each runner's output is kept in `.boa/logs/ATTEMPT_ID.log`; an attempt reported as `Runner lost` names that file in its error message, so read it before resolving the attempt.

## 5. Start API v1 for people or other agents

```bash
export BOA_API_TOKEN="choose-a-random-local-secret"
boring-orch-agent --home .boa serve --host 127.0.0.1 --port 8088
```

Submit the same kind of document through `POST /api/v1/tasks`, including `Authorization: Bearer $BOA_API_TOKEN` and `Idempotency-Key`. See [`api-v1.md`](api-v1.md) for routes and [`job-descriptions.md`](job-descriptions.md) for job sources and templates.

## 6. Use an LLM worker

Stop the demo worker, configure a provider, and start an LLM worker. Provider credentials stay in the worker process environment; they never go in the task JSON.

```bash
export BOA_PROVIDER=openai
export BOA_BASE_URL=https://provider.example/v1
export BOA_MODEL=provider-model-id
export BOA_API_KEY=provider-api-key
boring-orch-agent --home .boa worker --id llm-1 --runtime llm --slots 1
```

The worker asks an OpenAI-compatible server for JSON output (`response_format: {"type": "json_object"}`), which keeps a chatty model inside the one-action-per-turn envelope; `export BOA_JSON_MODE=off` disables it for servers that reject the field. A server that reports no `usage` in its replies leaves the task's token accounting unknown, so do not set `budget.max_tokens` for such a provider: the next model call would be blocked as unverifiable.

Use a template from [`examples/jobs/`](../examples/jobs/) as the submission body. For Coddy’s local OpenAI-compatible provider, follow [`examples/coddy/README.md`](../examples/coddy/README.md).
