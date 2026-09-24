# Implementation plan

This is the working plan for `boring-orch-agent`. It replaces the earlier `TODO.md`.
Work follows [AGENTS.md](AGENTS.md): an observable Gherkin scenario first, then the
smallest change in the lowest owning layer ([docs/architecture.md](docs/architecture.md)),
then `python tools/pipeline.py` as the release gate.

Four features are planned: a per-job context budget, planning as a first-class job,
environment discovery with explicit approval, and an Agent Client Protocol (ACP) runtime.
Three independent reviews (`kimi_review.md`, `qwen_review.md` and a review of an earlier
revision of this plan) added correctness and hardening work that lands first.

## Baseline

Version 0.1.3 is the pinned baseline: expected-file verification (`expect_files`),
truncated-completion reporting, JSON-mode requests for OpenAI-compatible servers, per-attempt
runner logs, and free requeue after a worker is lost before launch. A plan review dated
2026-09-19 read commit `d32fac7` and reported `expect_files` and truncation handling as
missing; they were uncommitted at that moment and are part of 0.1.3.

`expect_files` means **existence at publication time**. It proves a file is present, not that
this attempt wrote it or that its contents are right. Pair it with an `output_schema` that
carries what the job produced, or with a following read-only review job.

## Contracts to write before code

Each is a short document in `docs/`, merged before the milestone that depends on it. They
exist because the guarantees below cannot be inferred from the code as it stands.

| Document | Blocks | Settles |
|---|---|---|
| `docs/isolation.md` | Milestone 4 | What an agent runtime may claim. Tier A: the agent runs under `bwrap` with the workspace bound read-only (read-write only under `workspace-write`), private `/tmp`, an isolated agent state directory, no store-home access, network off unless the task allows it. Tier B: no OS isolation, the claim drops to "the agent is trusted as the operator", and `sandbox: read-only` tasks are refused. Unknown capability fails closed. Protocol callbacks are an audit trail, never a boundary. |
| `docs/cancellation.md` | Milestone 4 | Cooperative cancel, bounded grace, `SIGTERM` then `SIGKILL` to the process group. Confirmed cessation requires both a gone process group and a terminal adapter stop reason; anything else stays `Unknown` with its reservation. A timer-driven supervisor bounds a silent agent. |
| `docs/workflows.md` | Milestone 2 | Workflow root and states, child identity, authority inheritance, dependency data transfer, workflow-wide budgets. |
| `docs/budgets.md` | Milestones 1, 4 | What each budget means, how the effective limit is computed, and a per-runtime matrix of enforced, approximate and unenforceable budgets. |
| `docs/discovery.md` | Milestone 3 | The three probe tiers and the consent rule. |

## Milestone 0 — Configuration

Thresholds and paths become operator-visible without weakening invariants.

- **Tunables versus invariants.** `limits` holds file and listing sizes, output caps, polls,
  TTLs, retention, backoff and probe bounds. Invariants are not configurable: `synchronous=FULL`,
  `BEGIN IMMEDIATE` for mutations, path confinement, HTTPS off loopback, the 2 MiB provider cap.
  Weaker durability is never a side effect of this milestone.
- **One authority.** `workspace_root`, `max_active` and `allow_write` stay in the SQLite
  `settings` table; the config file holds only tunables. `init --config-only` refuses to
  overwrite an existing file without `--force`.
- **Propagation to detached runners.** `Worker.launch()` passes the resolved `--config` path and
  a revision hash; a runner whose effective configuration differs fails the attempt `permanent`,
  so manager, worker and runner can never diverge silently. The effective mapping is written to
  `config.effective.json` for inspection.
- **Path security.** Configured artifact, log and lock paths join the store home in the
  workspace exclusion set, and result reads resolve through the configured path.
- **Tick errors.** `loop()` catches `StorageError` and `OSError`, logs with bounded backoff and
  continues; other exceptions still exit non-zero, and `manager --once` keeps a failing status.
- **Logging.** Structured stderr JSON lines for scheduled, settled, retried, requeued, lost and
  pruned. `anthropic-version` moves to configuration.

The complete key inventory ships here; no later milestone introduces an unnamed key.

## Milestone H — Hardening from the code reviews

1. **Linux only.** `fcntl` is imported at module scope, so the guard is import-safe:
   `process.supported()` returns a reason, and every loop subcommand refuses early with
   "runs on Linux only (needs /proc identity and flock)". Stated in the README, the
   getting-started guide and the packaging classifiers.
2. **Retention.** Lock files are never pruned by age: unlinking a locked pathname lets a second
   process create a new inode and take a second exclusive lock. Only a lock-holding prune is
   allowed, and it is off by default. Command receipts become tombstones so a delayed duplicate
   submission cannot re-execute after its task is gone; the idempotency horizon is its own
   setting. Deletion uses a durable intent, resumable after a crash, deleting in foreign-key
   order before artifacts. Workers are reaped by liveness and zero references, never by name
   prefix. Artifacts a live workflow still needs are retained, and a result read after expiry
   returns a distinct `gone` error. Task, event and attempt listings gain paging with documented
   ordering and opaque cursors.
3. **Deadline versus a committed success.** A `Succeeded` attempt that finished before the
   deadline is accepted even when the deadline has since requested cancellation; a user cancel
   still wins.
4. **Artifact validation outside the write transaction.** The guarantee is writer concurrency:
   another connection can submit and heartbeat while an artifact is validated. A pre-validated
   verdict is bound to attempt id, result path, checksum and spec hash, and re-checked against
   settlement eligibility and current intent before it is applied.
5. **Durable relaunch backoff.** The launch counter and next-launch time move into the outbox,
   so a worker restart no longer relaunches a crash-looping runner immediately.
6. **`merged_sequence`** stops being written and is documented as reserved; removing the column
   waits for a later schema change so it is never a prerequisite.
7. **Idempotency documentation.** The hash covers canonical JSON of the submitted document
   before defaults are applied: key order and whitespace do not matter, writing a default
   explicitly does.
8. **Test gaps.** Publishing over `max_output_bytes`; Anthropic and Ollama malformed-completion
   branches; backoff timing; the deadline case; `expect_files` against a pre-existing file, a
   missing file and an escaping path.
9. **Migrations.** One versioned migration per release, honored by both the read and the write
   transaction helpers, tested for fresh init, upgrade, concurrent openers, interruption,
   unsupported versions and an old process against a new schema.

## Milestone 1 — Effective model identity and context admission

Admission is bound to the model that actually runs.

- **Per-attempt profile.** An attempt records its runtime, provider, model, context limit,
  output cap and the source of that limit, written in the claim transaction and re-checked at
  launch. Scheduling compares a task's estimate against the profile of the model the task will
  use, including a task-pinned model, not one number per worker.
- **Arithmetic.** The effective limit is the minimum of the values that are known among the
  task budget, the worker override and the server-advertised window. Unknown is never infinity:
  with nothing known the byte bound applies, the source is recorded as unknown, and admission
  follows an explicit policy. `context_tokens` counts input; the reserve is the smaller of the
  requested output budget and the model's maximum output, subtracted at admission.
- **Accounting.** Completions expose input and output counts separately. The estimator uses the
  provider's reported input count when available and a conservative characters-per-token figure
  otherwise, counting the whole serialized request: system prompt, output schema, expected
  files, history, tool results and delivered dependency inputs. It is an estimate, so upstream
  overflow keeps its conservative handling.
- **Preflight versus mid-run.** A task that cannot fit before any model call ends as a
  confirmed non-start: no effects, free requeue, no retry consumed. A conversation that becomes
  oversized after a call or a write is materially different, since effects may exist; it returns
  to planning only under replay safety and otherwise waits for an operator.
- **Unschedulable work** is marked for planning with its measured requirement instead of sitting
  pending forever.
- **Range reads.** `read_file` gains bounded offset and length so a plan can consume part of a
  large file. An input that cannot be split at all is terminal, so replanning cannot loop.

## Milestone 2 — Workflows

Planning becomes a first-class job whose children are safe by construction.

- **The workflow root is its own record**, with its own states, iteration and plan revision.
  Plan and execution tasks stay ordinary tasks with ordinary terminal results; the workflow
  aggregates them, and no task is given a second lifecycle.
- **Child identity** is internal (workflow, plan revision, child index) in a namespace separate
  from caller keys, and expansion happens through a transaction-aware insert from inside
  settlement, never through the public submit path.
- **The plan document is a real JSON Schema** with per-job ids and dependencies, rejecting
  cycles, unknown dependencies, duplicate order, empty plans and oversized plans. A rejected
  plan creates no children.
- **Authority is inherited and may only narrow.** Workspace, sandbox, tools, model policy,
  retry policy and budget ceilings come from the root. A read-only workflow cannot produce a
  writable child even where the installation permits writing. Planner output is untrusted data
  and is validated twice.
- **Dependencies declare delivery.** A dependency hands over a bounded, validated result or
  named files, counted in the child's estimate. Ordering alone never implies data flow.
- **Budgets live on the workflow** and are decremented by every child and every replan; a new
  child or plan never resets an allowance.
- **Replanning** receives the completed children's verified outputs and the measurements,
  carries over work already succeeded instead of regenerating it, and cancels obsolete children
  from an earlier revision first.
- **Planning is opt-in per task**, not forced on every model task; single-shot tasks keep the
  Milestone 1 preflight. The planner-context threshold is a scheduling policy compared against
  the effective limit, not a quality claim, and the planner runs read-only by default.

## Milestone 3 — Inventory and approval

The orchestrator records what it could use without doing anything active by default.

- **Three tiers.** Passive inventory is the default at `init`: no subprocess, no network.
  A handshake tier starts an agent with isolated state to read its capabilities, then tears the
  process group down. A generative tier sends one bounded completion per model to learn usage
  reporting and JSON mode, and requires explicit consent with a stated cost policy.
- **Cleanup is real.** Each probe runs in its own process group under a hard timeout enforced by
  a kill, never a future's timeout, and leaves no child behind. Probe output, URLs, versions and
  metadata are bounded and sanitized before they are recorded.
- **Approval binds the resolved route** after environment overrides, including the executable's
  identity; a change requires re-approval rather than silently inheriting it. The unlisted
  escape hatch is per invocation and recorded.
- **Credentials are recorded by reference**, never by value, and no YAML is parsed.

## Milestone 4 — ACP runtime

Run an ACP agent under a contract that is enforceable and honestly described.

- **Isolation** per `docs/isolation.md`, Tier A by default. For an agent with its own permission
  system, the orchestrator also pins an isolated home, refuses a bypass permission mode, denies
  project-local hooks, MCP servers and subagents, and disables skill auto-discovery.
- **Cancellation** per `docs/cancellation.md`. A sent notification is never reported as
  `Cancelled`; the request latency target is stated separately from confirmed cessation.
- **Budgets by capability matrix.** Step counts and per-call limits are unenforceable inside an
  opaque agent turn; wall-clock limits and the process-tree kill are enforced; token totals are
  approximate and only where the adapter reports them, with cumulative-per-turn reporting
  recorded as a maximum rather than summed. A task whose budgets the adapter cannot enforce is
  refused unless it opts into the weaker contract.
- **Protocol handling.** Absolute paths in filesystem callbacks map back to workspace-relative
  paths and are validated like any tool path; plan notifications are progress only and never
  become child task specifications; modes and models are negotiated, not assumed.
- **Layering.** The stdio JSON-RPC transport and provider metadata sit below discovery;
  discovery imports them and they never import discovery.

## Order

Baseline 0.1.3 → contracts → Milestone 0 → H → 1 → 2 → 3 → 4.

Each release states its schema version and contents. Milestone H carries the outbox backoff
columns, command tombstones and the pruning mark; Milestone 1 the per-attempt profile and the
worker default profile; Milestone 2 the workflow tables. No milestone depends on a schema
shipped by a later one.

## Notes for agents working with Coddy

- Read the built-in documentation first: `coddy docs list`, `coddy docs show reference/http-api`,
  or the `/docs` page of a running server.
- `/compact` recovers an interactive Coddy session that hit the upstream input limit. It does
  nothing for this orchestrator's own model calls, which are stateless requests rebuilt each
  turn; Milestone 1 is the answer there.

## Known upstream defects, no local action

- Coddy's non-streaming `/v1/chat/completions` returns no `usage` object
  ([coddy-agent#321](https://github.com/coddy-project/coddy-agent/issues/321)). Leave
  `budget.max_tokens` unset for Coddy-backed tasks.
- Coddy returns HTTP 500 for every upstream provider error, including a deterministic 400
  ([coddy-agent#322](https://github.com/coddy-project/coddy-agent/issues/322)). Keep task
  conversations under the provider plan's input limit, and read Coddy's log before resolving an
  `Unknown` attempt.
