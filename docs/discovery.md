# Environment discovery and approval

Discovery records what the installation can use without turning discovery into an
execution side channel. The default inventory is passive: it resolves executable
names with the local `PATH`, reads executable metadata, and records configured
provider metadata without starting a process or making a network request.

The implementation is in `boring_agent.discovery.Discovery`. The Store seeds the
passive inventory during `init`; callers can read it with
`Store.discovery_inventory()` or `GET /api/v1/discovery/inventory`.

## Probe tiers

### Passive inventory

Passive inventory is the default and has no consent prompt because it has no active
effect. It may record:

- route name and whether its executable is locally available;
- resolved executable path and a local identity (device, inode, size, mtime and
  SHA-256); and
- provider URL, model, version and credential references, when configured.

It never calls `--version`, opens a provider URL, reads a credential value, or parses
YAML. A credential is represented by a reference such as `env:BOA_API_KEY`, never
by the value in the environment.

### Handshake

A handshake is an explicit, operator-approved invocation of one executable. Approval
is made with a route and tier:

```python
from boring_agent.discovery import Discovery

route = {
    "id": "fixture",
    "executable": "/path/to/agent",
    "args": ["--version"],
    "credential_ref": "env:BOA_API_KEY",
}
approval = Discovery(store).approve(route, "handshake")
result = Discovery(store).handshake(route, approval["approval_id"], timeout=10)
```

The executable starts in a new process group with an isolated `HOME` and XDG state
directories. The supervisor enforces the hard timeout itself, terminates the whole
process group (`SIGTERM`, then `SIGKILL` when necessary), drains output with a
bounded collector, and records a bounded, sanitized result. A timeout is an
observable timeout outcome, not a successful handshake.

### Generative probe

A generative probe is refused until the operator approves the route as
`generative` and states a cost policy. It makes exactly one bounded completion
request; discovery does not retry it:

```python
approval = Discovery(store).approve(
    route | {"model": "fixture-model"},
    "generative",
    cost_policy={"max_requests": 1, "operator": "release-engineering"},
)
result = Discovery(store).generative(
    route | {"model": "fixture-model"},
    approval_id=approval["approval_id"],
)
```

Tests and offline callers can provide a `requester(messages, model,
output_tokens, timeout)` callback. Without one, the configured provider adapter is
used only after approval. Provider errors are stored as a failed single-probe
outcome and are never silently retried.

## Approval binding

An approval is bound to:

- the resolved route and arguments;
- the resolved executable path and identity, including its content hash; and
- fingerprints of environment overrides used to resolve the route.

Changing an override, changing the executable contents, or resolving a different
executable rejects the old approval and requires explicit re-approval. Approval
records contain only route metadata and fingerprints. They do not contain process
environment values.

An unlisted route can be used with `allow_unlisted=True` for one explicit invocation.
That invocation is written to the audit history and does not create a reusable
approval; a later invocation must receive a new explicit approval or another
one-time exception.

## Evidence and credential handling

Probe output is capped before storage. Credential-shaped assignments, bearer
values, common provider key prefixes and credential references supplied as values
are rejected or redacted. URLs and version metadata remain attributable when output
is truncated. The Store exposes sanitized records through:

- `Store.discovery_evidence()` / `GET /api/v1/discovery/evidence`;
- `Store.discovery_audit()` / `GET /api/v1/discovery/audit`; and
- `Store.discovery_approvals()` / `GET /api/v1/discovery/approvals`.

Routes that contain `api_key`, `password`, `token`, `secret` or another credential
value are invalid. Use `credential_ref` or `credential_refs` instead. The discovery
implementation imports no YAML parser, and raw credential values are not persisted
in inventory, approvals, evidence or audit records.

## HTTP endpoints

All endpoints use the normal authenticated API boundary:

| Method | Path | Effect |
| --- | --- | --- |
| `GET` | `/api/v1/discovery/inventory` | Read passive inventory |
| `GET` | `/api/v1/discovery/approvals` | Read approval records |
| `GET` | `/api/v1/discovery/evidence` | Read sanitized probe evidence |
| `GET` | `/api/v1/discovery/audit` | Read approval/probe audit history |
| `POST` | `/api/v1/discovery/approve` | Create handshake or generative approval |
| `POST` | `/api/v1/discovery/handshake` | Run an approved handshake |
| `POST` | `/api/v1/discovery/generative` | Run one approved completion probe |

Active POST routes must carry the route and the approval identifier returned by the
approval call. The API does not start discovery from a read request or from Store
initialization.
