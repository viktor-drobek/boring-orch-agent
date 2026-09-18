# Changelog

## Unreleased

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
