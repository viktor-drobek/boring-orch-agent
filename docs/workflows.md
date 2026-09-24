# Durable workflows

Workflows are opt-in planning around the ordinary task lifecycle. The workflow root,
plan revisions, child identity and dependency deliveries are durable metadata; the
planner and every execution child are ordinary `tasks` with one terminal lifecycle.
A workflow does not turn a task into a second task or execute model work by itself.
The manager and workers remain the execution route.

## Creating a workflow

Use the Store method `create_workflow(spec, idempotency_key)` or `POST /api/v1/workflows`.
The root document is a normal task document with an opt-in block:

```json
{
  "objective": "Prepare the release inventory",
  "runtime": "demo",
  "demo": {"result": {"children": []}},
  "workflow": {
    "enabled": true,
    "max_children": 20,
    "max_tokens": 50000,
    "max_attempts": 8,
    "planner_context_threshold": 12000
  }
}
```

Creation stores a workflow root and one ordinary, read-only planner task in one
SQLite transaction. The caller's idempotency key belongs to that planner command;
workflow children use an internal identity of `workflow`, `revision` and `child_index`,
so a child label can never collide with a caller idempotency key.

Planner output is a JSON plan. The manager validates the planner task as usual and,
when it succeeds, settles that plan atomically with child insertion. Invalid graph
plans are recorded as rejected and create no children. A storage failure rolls back
both plan settlement and child expansion.

## Plan format and validation

A plan has a non-empty `children` array. Each child has a unique `id`; `order` is
optional (the array index is used) and must be unique and contiguous when supplied.
`dependencies` names child IDs in the same plan. The validator rejects unknown
references, cycles, duplicate order, duplicate child IDs, oversized plans and more
children than the workflow limit.

A child task may be supplied under `task` or with task fields directly:

```json
{
  "children": [
    {
      "id": "extract",
      "order": 0,
      "task": {"objective": "Extract the facts", "runtime": "demo"},
      "deliver": {"result": true, "files": ["facts.json"]}
    },
    {
      "id": "review",
      "order": 1,
      "dependencies": ["extract"],
      "task": {"objective": "Review the extracted facts", "runtime": "demo"}
    }
  ]
}
```

The plan is checked with Draft 2020-12 JSON Schema and then checked as a dependency
graph. Child task documents are passed through the normal task validator as a second
trust boundary.

## Authority and delivery

A child can only narrow the root authority:

- its workspace is the root workspace;
- a read-only root cannot produce a writable child;
- child tools must be a subset of root tools;
- a pinned root model cannot be replaced by another model;
- retry safety and maximum attempts cannot be broadened;
- every budget field a child omits is inherited from the root, a child may only
  lower a root value, and a child cannot write `null` where the root has a
  ceiling; and
- child token budgets cannot exceed the remaining workflow ceiling.

Dependencies express delivery, not merely ordering. The **producing** child declares
`deliver`; a consumer only names its dependencies. A successful source hands over
only the declared validated result and named visible workspace files. Files are
bounded to 1 MiB each and remain workspace-relative. The resulting canonical payload
and byte count are durable in `workflow_deliveries` and `context_bytes` on the child.
An absent result or named file records a failed dependency transfer and the consumer
is not started. A dependency with no `deliver` declaration transfers no data, even
though its completion may still be required for ordering.

## Budgets and replanning

`max_attempts` and `max_tokens` are cumulative workflow ceilings. A new plan revision
never resets either counter. Attempts are consumed when a child is admitted; reported
child usage is added to the workflow token counter. The planner-context threshold is
an admission/scheduling value only; it is not a claim about output quality.

`POST /api/v1/workflows/{id}/replan` or `Store.replan_workflow` validates and accepts
a new revision. Obsolete queued children are cancelled before replacement insertion;
running children are not silently killed. Only `Succeeded` children with the same
child ID are carried into the new revision with their verified output and measured
context, so completed work is not regenerated; a child that was still pending gets a
fresh task under the new revision even when the new plan keeps its ID. New children use a fresh internal revision
identity while retaining the workflow's remaining authority and budget.

Useful read routes are:

- `GET /api/v1/workflows`
- `GET /api/v1/workflows/{workflow_id}`
- `GET /api/v1/workflows/{workflow_id}/children`

These routes observe durable state only. They do not launch a planner, manager,
worker, provider or native session.
