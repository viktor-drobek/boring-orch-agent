# Changelog

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
