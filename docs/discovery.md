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
YAML. A credential is represented by a reference, never by its value: `env:NAME` and
`ref:NAME` both name the environment variable `NAME`, which is read only when an
approved probe actually starts and is never stored. A generative probe uses the
route's `credential_ref` when present and `env:BOA_API_KEY` otherwise.

### Handshake

A handshake is an explicit, operator-approved invocation of one executable. Approval
is operator consent and is made locally, in the operator's Python process, with a
route and tier. The HTTP API cannot create an approval:

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
- the resolved executable path and identity, including its content hash;
- fingerprints of environment overrides used to resolve the route; and
- the effective generative endpoint: provider, base URL, model and credential
  reference, including fallbacks from `BOA_PROVIDER`, `BOA_BASE_URL`, `BOA_MODEL`
  and `env:BOA_API_KEY`.

Changing an override, changing the executable contents, resolving a different
executable, or pointing the credential at a different provider or base URL rejects
the old approval and requires explicit re-approval. A credential reference is
therefore only ever sent to the endpoint the operator approved. Approvals created
before the endpoint became part of the fingerprint no longer match and must be
re-approved. Approval
records contain only route metadata and fingerprints. They do not contain process
environment values.

An operator working locally in Python may pass `allow_unlisted=True` to run a
route without an approval record. Every such invocation is written to the audit
history as `unlisted_invocation` and never creates an approval, so the next call
without the flag is refused. The flag is **not accepted over HTTP**: a request
body cannot carry operator consent, and `POST /api/v1/discovery/handshake` or
`/generative` with `allow_unlisted` returns `400` without running anything.

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
| `POST` | `/api/v1/discovery/approve` | Always refused with `403 operator_only` |
| `POST` | `/api/v1/discovery/handshake` | Run a handshake for an operator-approved route |
| `POST` | `/api/v1/discovery/generative` | Run one completion probe for an operator-approved route |

Approval is operator consent to start an executable or to send a credential to a
provider, so, like `allow_unlisted`, it cannot be carried by an HTTP request body:
`POST /api/v1/discovery/approve` returns `403` and creates nothing. The operator
approves locally with `Discovery.approve()`. Active POST routes must carry the
route and the operator's approval identifier; a route that differs from the
approval fingerprint (another executable, arguments, override or base URL) returns
`409` without running anything. The API does not start discovery from a read
request or from Store initialization, and it rejects `allow_unlisted`. Discovery has no CLI subcommand
yet; `boa discover` is planned in `PLAN.md`.
