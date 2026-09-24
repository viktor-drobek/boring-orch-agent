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
| `GET` | `/api/v1/workflows` | Workflow roots with plans, children and remaining budgets. |
| `POST` | `/api/v1/workflows` | Create a workflow root and its read-only planner task. Requires `Idempotency-Key`; returns `202`. |
| `GET` | `/api/v1/workflows/{workflow_id}` | One workflow root. |
| `GET` | `/api/v1/workflows/{workflow_id}/children` | Child records across plan revisions. |
| `POST` | `/api/v1/workflows/{workflow_id}/plan` | Validate and settle a plan; an invalid plan is recorded as rejected and creates no children. |
| `POST` | `/api/v1/workflows/{workflow_id}/replan` | Accept a new plan revision; obsolete pending children are cancelled first. |
| `GET` | `/api/v1/discovery/inventory` | Passive inventory recorded at `init`. |
| `GET` | `/api/v1/discovery/approvals` | Approval records with route fingerprints. |
| `GET` | `/api/v1/discovery/evidence` | Sanitized probe evidence. |
| `GET` | `/api/v1/discovery/audit` | Approval and probe audit history. |
| `POST` | `/api/v1/discovery/approve` | Approve a route for `handshake` or `generative` probing (`generative` needs `cost_policy`). |
| `POST` | `/api/v1/discovery/handshake` | Run an approved handshake probe. `allow_unlisted` in the body is refused with `400`. |
| `POST` | `/api/v1/discovery/generative` | Run one approved completion probe. `allow_unlisted` in the body is refused with `400`. |

A native job document must name an existing absolute `workspace` outside the store home; the session it creates defaults to permission mode `ask` and never `bypass`.

Use one durable key for each logical command. Resending the same request and key returns the original receipt with `duplicate: true`; reusing a key for a different command or payload returns `409 conflict`. A submit receipt only proves durable acceptance, never successful execution.

```bash
task_id="..."
curl --fail-with-body "http://127.0.0.1:8088/api/v1/tasks/$task_id" \
  -H "Authorization: Bearer $BOA_API_TOKEN"
```
