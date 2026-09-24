# Changelog

## 0.1.5

Correction release for 0.1.4, following the review in `release-0.1.4-review.md`.

- **Acceptance suite is honest again.** The import-time generator that bound roughly 270 step lines to a no-op is removed, along with the three feature files it served (`acp_runtime`, `configuration`, `context_admission`) whose behavior does not exist. Three shared steps that had been weakened to return early are restored. `discovery`, `workflows` and `hardening` are rewritten with steps that call production code. A regression test now fails the build if a step is generated at import or bound to a catch-all.
- **CI passes.** The two guidance tests read `docs/exec.md`, which is committed, instead of an untracked `memory/` path; every rule tree references that path.
- **Security.** Native lifecycle sessions default to permission mode `ask`, refuse any bypass mode, and require an existing absolute workspace outside the store home. The `allow_unlisted` discovery escape is refused over HTTP; it remains an audited, operator-only Python parameter.
- **Correctness.** Retention no longer wedges on a workflow planner task (foreign key). Replanning carries only `Succeeded` children and gives a kept pending child a fresh task instead of a cancelled one. A child inherits and may only lower root budgets; `null` cannot remove a token ceiling and deadlines cannot grow. `deliver` is read from the producing child, as documented. `create_workflow` on a key used by a plain submit returns `409` instead of crashing. Discovery understands `env:NAME` credential references, honors `credential_ref` for generative probes, kills a group that ignores `SIGTERM` before removing its state, and validates its timeout. ACP launch plans use a correct bubblewrap argument order (tmpfs root first, no orphaned `--ro-bind`), require a private state path, refuse the store home, take the budget matrix from an adapter registry instead of the task, understand OpenAI-shaped usage, keep missing usage unknown, and escalate through both signals in one late poll. Lifecycle read routes no longer take the write lock.
- **Hardening items delivered:** import-safe Linux-only guard for `manager`, `worker`, `serve` and `demo`; `merged_sequence` is no longer written; the Linux classifier and a platform note in the README and getting-started guide.
- **Still not implemented, and no longer claimed:** an ACP worker runtime, context admission, the configuration file, durable relaunch backoff, and listing paging. `PLAN.md` records their status.

## 0.1.4

- Add durable retention with idempotency tombstones, deadline settlement precedence, artifact validation outside the write transaction, and resumable schema migrations (`user_version` 2).
- Add workflow planning metadata (`workflow_roots`, plans, children, deliveries) settled by the manager when a planner task succeeds, with an HTTP API.
- Add consent-gated discovery (passive inventory at `init`, approved handshake and generative probes) with an HTTP API.
- Add the native session lifecycle store and its HTTP API, ACP launch-policy objects, a parent idle watchdog, and the contract documents for isolation, cancellation, budgets, discovery, exec and workflows.
- Note: this entry was rewritten in 0.1.5. The original claimed acceptance coverage for ACP execution, context admission and configuration that did not exist; see `release-0.1.4-review.md`.

## 0.1.3

- Replace `TODO.md` with `PLAN.md`: the implementation plan for the context budget, workflow planning, environment discovery and the ACP runtime, with the hardening work from three independent reviews scheduled ahead of them.
- Document `expect_files` as existence at publication time, not proof that the attempt wrote the file, and reject absolute, parent and hidden expected paths at submission.

### Known provider limitations (Coddy)

Tested against `coddy serve` 1.1.53 on 2026-09-18 and reproduced on 1.1.59 on 2026-09-19; the source of 1.1.62 shows the same code paths. Both are Coddy-side defects with issues filed; the orchestrator's behavior is correct given what it receives, and these workarounds apply until the issues are fixed.

- Coddy's non-streaming `/v1/chat/completions` reply carries no `usage` object ([coddy-agent#321](https://github.com/coddy-project/coddy-agent/issues/321)). The worker records the task's token accounting as unknown and, with `budget.max_tokens` set, refuses the next model call as unverifiable. Workaround: leave `budget.max_tokens` unset for Coddy-backed tasks and bound work with `max_steps`, `request_seconds` and `attempt_seconds`.
- Coddy returns HTTP 500 for every upstream provider error, including a deterministic upstream 400 such as "request over the plan's input-token limit" ([coddy-agent#322](https://github.com/coddy-project/coddy-agent/issues/322)). The orchestrator treats a 5xx as an ambiguous outcome by design, so the attempt is held as `Unknown` with its reservation until an operator runs `resolve --confirm-stopped`; the real cause is only in `~/.coddy/logs/serve.log`. Workaround: keep each task's conversation well under the upstream plan's input limit (small source files, indexes without long excerpts) and check Coddy's log before resolving an `Unknown` attempt.

- Add the `expect_files` task field: relative paths the runner verifies in the workspace before a result can be published, so a model cannot report success for a file it never wrote.
- `write_file` creates missing parent directories inside the workspace instead of failing; path validation still rejects escapes, hidden names and symlinked components first.
- A completion cut off at the output limit with no content now fails with a message that names `budget.output_tokens` and the reasoning cause, instead of "Invalid JSON at char 0"; a cut-off partial answer says so in its validation error.

- **Agent tip**: If Coddy reports an upstream API context-limit error, the agent can send `/compact` to trigger context compaction and continue the session.

- Ask OpenAI-compatible servers for JSON output with `response_format`, matching the JSON request already sent to Ollama, so a chatty or reasoning model stays inside the one-action-per-turn envelope. `BOA_JSON_MODE=off` restores the previous request shape.

- Add acceptance scenarios for a queued assignment that stays `Fresh` while waiting, a runner that cannot record its process identity and therefore never starts, the log file named by a `Runner lost` attempt, and the launch log a worker keeps for each attempt.
- Document confirmed non-start requeueing after worker loss, per-attempt runner logs under `logs/`, relaunch backoff and read snapshots in the architecture and getting-started guides.

## 0.1.1

- Add a start-to-finish guide, job-description contract, and read-only, inventory, and workspace-write task templates.
- Add synchronized rules for Codex, Claude Code, Cursor, and Coddy, with a Codex Cursor-rule hook bridge.

## 0.1.0

- Initial public release of the durable local agent orchestrator.
- Gherkin-first build pipeline, CLI, SQLite manager-worker lifecycle, and validated result artifacts.
- Authenticated API v1 and examples for a Coddy `serve` OpenAI-compatible provider.
