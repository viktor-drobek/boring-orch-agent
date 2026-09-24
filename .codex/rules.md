# Codex rule index

`.cursor/rules/` is the canonical per-topic rule source. `.codex/hooks/attach_rules.py` delivers the rules below to Codex without duplicating their bodies.

| Rule | Attachment | Scope |
| --- | --- | --- |
| `workflow.mdc` | Session start | Always on; native exec, job.model selection, ready-only dispatch, session lifecycle, Coddy transport order and result handoff |
| `architecture.mdc` | Session start | Always on |
| `testing.mdc` | Session start | Always on |
| `code-style.mdc` | Session start | Always on |
| `implementation-order.mdc` | Before edit | `boring_agent/**/*.py` |
| `api-layer.mdc` | Before edit | API, CLI, API tests, API docs |
| `core-modules.mdc` | Before edit | State model, Store, manager, worker, runner |

Trust the hooks through `/hooks` in Codex once per clone and again after changing `.codex/hooks.json` or `.codex/hooks/attach_rules.py`.
