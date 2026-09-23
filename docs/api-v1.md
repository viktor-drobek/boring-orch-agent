# API v1

The service is started with `boring-orch-agent --home .boa serve`. It has no execution loop of its own: run one manager and at least one worker against the same `--home` directory.

By default it listens on `127.0.0.1:8088`. A non-loopback listener is rejected unless a bearer token is configured with `--auth-token` or `BOA_API_TOKEN`. When a token is configured, every route requires `Authorization: Bearer <token>`.

All bodies and replies are JSON. Bodies may be at most 256,000 bytes. Unknown paths return `404`; unsupported methods return `405`. Errors have this stable shape:

```json
{"error":"invalid_request","message":"explanation for the caller"}
```

| Method | Route | Meaning |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Service version and liveness. |
| `GET` | `/api/v1/capacity` | Manager capacity and known workers. |
| `GET` | `/api/v1/tasks` | Durable tasks in submission order. |
| `POST` | `/api/v1/tasks` | Submit a task document. Requires `Idempotency-Key`; returns `202` and a receipt. |
| `GET` | `/api/v1/tasks/{task_id}` | Task, attempts, and current observation condition. |
| `GET` | `/api/v1/tasks/{task_id}/events` | Ordered task event history. |
| `GET` | `/api/v1/tasks/{task_id}/result` | Validated result for a succeeded task only. |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | Request cancellation. Requires a distinct `Idempotency-Key`; returns `202`. |
| `POST` | `/api/v1/attempts/{attempt_id}/resolve` | Operator resolution of an `Unknown` attempt. Requires `{"note":"evidence","confirm_stopped":true}`. |
| `GET` | `/api/v1/sessions` | Durable native sessions, lineage and warm-up/recovery state. |
| `GET` | `/api/v1/sessions/{session_id}` | One native session, including its read-only digest metadata. |
| `GET` | `/api/v1/sessions/{session_id}/branches` | Deterministic child branches for one parent session. |
| `GET` | `/api/v1/native/jobs` | Registered native jobs and their readiness state. This route does not launch jobs. |
| `POST` | `/api/v1/native/jobs` | Atomically register one `acp` or `coddy_native` job with a required model. |
| `POST` | `/api/v1/native/workflows` | Atomically register a dependency graph from `{"jobs":[...]}`. |
| `GET` | `/api/v1/native/runs` | Native run history with session IDs and recovery evidence. |

Use one durable key for each logical command. Resending the same request and key returns the original receipt with `duplicate: true`; reusing a key for a different command or payload returns `409 conflict`. A submit receipt only proves durable acceptance, never successful execution.

```bash
task_id="..."
curl --fail-with-body "http://127.0.0.1:8088/api/v1/tasks/$task_id" \
  -H "Authorization: Bearer $BOA_API_TOKEN"
```
