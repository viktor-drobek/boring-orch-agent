# Job descriptions and sample scenarios

A job description is a JSON task document accepted by `POST /api/v1/tasks` or `boring-orch-agent submit`. The document is immutable after acceptance. Its `objective` is the agent’s work request; it is not inferred from a branch name, an issue title, or a file found in the workspace.

## Where jobs come from

The orchestrator does not poll external systems. A person or a trusted integration creates a task document from a source such as a ticket, a release checklist, a scheduled internal workflow, or a Coddy session, then submits it through the CLI or API. Keep that source-to-task conversion in the caller or integration, where its authentication and approval policy can be reviewed.

For local work, copy one of the versioned templates in [`examples/jobs/`](../examples/jobs/), edit it, and submit it. For an external system, translate its approved fields into the same schema and keep the source identifier in the caller’s idempotency key or audit record. Do not put API keys, passwords, or access tokens in `objective`, task JSON, or result schemas.

## Required decisions in every job

| Field | Decision it records |
| --- | --- |
| `objective` | Observable result, target files, and scope. |
| `runtime` | `demo` for offline verification or `llm` for a configured provider. |
| `workspace` | Existing directory under the installation’s configured workspace root. |
| `sandbox` and `tools` | Read-only by default; explicit write access when required. |
| `output_schema` | Machine-checkable final result. |
| `budget` | Maximum agent steps, provider request time, and output size. |
| `retry` | Whether the logical job is safe to repeat after a confirmed transient failure. |
| `expect_files` | Relative paths that must exist in the workspace when the job returns. The runner checks them before publishing the result, so a job cannot report success for a file it never wrote. The check is **existence at publication time**: it proves the file is there, not that this attempt created it or that its contents are correct. A file that already existed satisfies the check, so pair it with an `output_schema` that carries what the job produced, or with a following read-only review job. Paths are validated like every other task path: absolute, parent (`..`) and hidden components are rejected at submission. |

## Scenario: read-only repository review

Use [`read-only-review.json`](../examples/jobs/read-only-review.json) when a reviewer should inspect files and return findings without changing them.

```bash
boring-orch-agent --home .boa submit examples/jobs/read-only-review.json \
  --key review-main-2026-001
```

It grants only `list_files` and `read_file`, requires a summary and structured findings, and has no retry because a human may change the workspace before a repeat.

## Scenario: produce an inventory for another system

Use [`inventory.json`](../examples/jobs/inventory.json) when another agent or service needs a small JSON inventory of a directory. The output schema makes the result suitable for an API client to consume.

```bash
boring-orch-agent --home .boa submit examples/jobs/inventory.json \
  --key inventory-docs-2026-001
```

## Scenario: write a reviewable workspace artifact

Use [`write-summary.json`](../examples/jobs/write-summary.json) only after enabling write access at both boundaries:

```bash
boring-orch-agent --home .boa init --workspace ~/agent-workspace --allow-workspace-write
boring-orch-agent --home .boa worker --id writer-1 --runtime llm --slots 1 --allow-workspace-write
boring-orch-agent --home .boa submit examples/jobs/write-summary.json --key write-summary-2026-001
```

The job asks for one named file. The result reports its path and summary, but a person or separate validation step should still review the changed file.

## API submission scenario

```bash
curl --fail-with-body http://127.0.0.1:8088/api/v1/tasks \
  -H "Authorization: Bearer $BOA_API_TOKEN" \
  -H "Idempotency-Key: ticket-482-review-v1" \
  -H 'Content-Type: application/json' \
  --data @examples/jobs/read-only-review.json
```

Save the returned `task_id` alongside the ticket or workflow run. Reuse the same idempotency key only when retrying the exact same submission after a lost response. A new version of a job needs a new key.

## Native exec jobs and sessions

Native jobs are registered through the lifecycle layer, not submitted to the
legacy `llm`/`demo` manager-worker route. They require `id`, `objective`,
`runtime` (`acp` or `coddy_native`) and an explicit `model`; `dependencies` is an
optional list of job IDs, and `session` is either absent for a new root session
or an exact `@session:<id>` mention. The mention is a read-only Coddy digest
attachment capped at 24 KiB, not a live connection.

The lifecycle stores `lifecycle_sessions`, `lifecycle_jobs`,
`lifecycle_runs`, `lifecycle_branches`, `lifecycle_transfers` and ordered
`lifecycle_events` in the existing SQLite database. Registration and dependency
settlement are atomic. A first run warms a new session with `/compact` followed
by `/rpa-init` using a model with at least 100,000 context tokens; the default
is `ndsub/qwen3.8-27b` (262,144). Warm-up time consumes the job deadline.
Sequential dependents may reuse a completed session, while a fan-out creates
one independent child session per dependent with shared lineage. No live session
is used concurrently.

The API exposes `GET /api/v1/sessions`, session and branch reads,
`POST /api/v1/native/jobs`, `POST /api/v1/native/workflows`, and native run
history. These routes register or observe state only; they do not launch a
native job. Unknown run/transfer outcomes after restart are retained as
`unknown`/`recovering` and never replay automatically. Confirmed warm-up steps
and external operations use stable idempotency keys for safe retry.
