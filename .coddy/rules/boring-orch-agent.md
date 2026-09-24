# Coddy project addendum

Coddy discovers root `AGENTS.md`, the canonical brief shared by the agent rule trees.
Before changing this project, read it, `docs/architecture.md`, and relevant tests.

Before an operational job, read `docs/exec.md`
(relative to repository root). The parent validates READY status, dependencies, slot
and job.model, then calls `spawn_agent(model=job.model)`. Exec reads the whole job
and executes it itself with permitted native Coddy tools. Do not use execution
adapters, manager/worker services, intermediate submissions or direct provider HTTP.
Do not prelaunch exec agents to wait for another job or capacity.

Pass a complete assignment with absolute job path/hash, native run ID, workspace,
selected_model, permissions, budgets, checks and result recipient. Use background
execution and parent notification. A missing or mismatched model blocks work; Luna
is only the definition's bootstrap default. Native results do not settle legacy
SQLite tasks. Preserve historical Unknown records and never replay automatically.

The parent delivers reports and monitors native progress independently. Visible idle
>1800s needs an operator model decision without waiting for a hung child's answer.
Do not claim that a prompt implements a timer. A child has no switch_model tool;
`/model --count=N` in a normal session counts user turns, not the job budget.

## Native session lifecycle

An exact `@session:<id>` is a read-only Coddy digest attachment capped at 24 KiB, not a live child session. The lifecycle layer stores session IDs, lineage, job links, run history, transfers, branches and recovery evidence atomically in SQLite. A new session runs `/compact` then `/rpa-init` with a model having at least 100,000 context tokens; warm-up time counts against the job deadline and successful steps use stable idempotency keys.

A single sequential dependent may reuse a completed session. A 1-to-N fan-out creates independent child sessions with shared lineage and never shares a live session concurrently. Restart recovery preserves unknown outcomes and never auto-replays them. Native lifecycle records remain separate from legacy SQLite task history.

After a verified outcome, reassess and dispatch the next ready independent job.
Keep credentials out of prompts, job files and reports. When agent instructions
change, follow the complete Rules Sync contract in `AGENTS.md`: update paired topic
bodies and frontmatter, verify the `CLAUDE.md` symlink, refresh the Codex index only
when topics change, and include every affected tree in the same commit. The Codex
hook bridge stays unchanged unless its canonical template changes.
