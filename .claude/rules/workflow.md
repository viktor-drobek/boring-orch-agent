# Workflow

For a feature or bug, first add or revise an observable Gherkin scenario in `features/`. Run it to establish the intended red state, implement the smallest owning-layer change, then run the focused feature or unittest.

Before reporting work, run `python tools/pipeline.py`. It is the release gate: acceptance features must pass before unit tests, package build, and the installed-wheel demo. Do not weaken an expected behavior simply to make a failing test pass.

Update examples and API documentation when a public task schema, API route, CLI response, or task state meaning changes.

## Operational jobs through exec

Follow `AGENTS.md` and `docs/exec.md` (paths relative to repository root). The parent verifies readiness and the job selector, then calls `spawn_agent(model=job.model)`. Exec reads the whole approved job itself and performs it with permitted native Coddy tools. No execution adapters, manager/worker services, intermediate submissions, nested Coddy processes or direct provider HTTP. Legacy runtime tests remain allowed offline.

Launch only READY jobs with satisfied dependencies and an available execution slot, never waiting supervisors. Reassess remaining independent jobs after each verified outcome. Supply absolute job/workspace paths, hash, native run ID, selected_model, permissions, budgets, acceptance criteria and result recipient. Use background execution with parent notification/supervision. Unknown needs an operator decision without automatic replay. The parent owns independent idle observation and escalation after >1800s; a hung child cannot monitor itself. `/model --count` is not a job-step budget and is not an executable child tool. Keep native run results separate from immutable legacy Store history.

## Native session lifecycle

Native jobs may use an exact `@session:<id>` mention. Coddy resolves it as a read-only digest attachment capped at 24 KiB, never as a live child connection. The lifecycle store records sessions, lineage, job links, run history, transfers, branches and recovery evidence atomically in `lifecycle_*` SQLite tables.

A new session runs `/compact` followed by `/rpa-init` before its first job, using a model with at least 100,000 context tokens (`ndsub/qwen3.8-27b`, 262,144 by default). Warm-up time counts against the job deadline. Stable idempotency keys prevent duplicate confirmed steps; an uncertain external outcome becomes `recovering` and needs operator-confirmed retry with the same key.

A linear dependent may reuse a completed session only sequentially. A 1-to-N fan-out creates independent child sessions with shared lineage, so concurrent jobs never share a live session. Transfers contain only the verified result and read-only session mention. Restart recovery preserves evidence and never automatically replays an unknown run or transfer. Native lifecycle history remains separate from legacy `llm`/`demo` task history.

## Rules Sync

**MANDATORY** - if any rule or agent-instruction file is added or changed in this task, mirror the change to every rule tree in the same commit:

1. Identify every rule tree in the repository: `.cursor/rules/`, `.claude/rules/`, root `AGENTS.md` / `CLAUDE.md`, the Codex bridge (`.codex/`), `.coddy/rules/`, and any other agent roots such as `.kimi/` or `.github/copilot-instructions.md`.
2. For each edited file, locate or create its counterpart in every other tree under the same topic name.
3. Copy the body verbatim, then adapt frontmatter and inline links: Cursor `globs:` plus `alwaysApply:` <-> Claude `paths:` or no `paths:` for an always-on rule, and Cursor `@file.mdc` <-> Claude `.claude/rules/file.md`.
4. Keep the same language across all trees. Rule files and `AGENTS.md` are written in English unless the project deliberately uses another language.
5. If `AGENTS.md` changed, verify that `CLAUDE.md` still resolves to the same content through its symlink.
6. If a rule was added, renamed, or removed, refresh `.codex/rules.md`; `.codex/hooks.json` and `.codex/hooks/attach_rules.py` read `.cursor/rules/` directly and must remain the canonical template unless the template itself changes.
7. Include every synced file in the same commit and list it in the task report. Do not leave one tree ahead of another.

Skip synchronization only when a rule is genuinely tool-specific. Document the exception in the divergent file so it remains intentional and visible.
