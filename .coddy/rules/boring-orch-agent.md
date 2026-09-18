# Coddy project addendum

Coddy discovers the root `AGENTS.md`; it is the shared canonical instruction source for this project. This addendum is intentionally Coddy-specific and does not duplicate the Cursor or Claude topic-rule bodies.

Before changing this project, read `AGENTS.md`, `docs/architecture.md`, and the relevant feature or test. For a task run through Coddy `serve`, keep Coddy’s bearer token and the orchestrator API token in the process environment, never in prompts, task JSON, or workspace files. Use the documented `/v1` provider boundary and `/api/v1` task boundary from `examples/coddy/README.md`.

When agent rules change, update the shared rule trees in the same commit as required by `AGENTS.md`.
