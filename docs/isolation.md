# ACP isolation contract

ACP launches are classified by what the host can enforce, not by what an agent or
callback claims.

## Tier A: bubblewrap

On Linux, when `bwrap` is found on `PATH`, a protected ACP launch is prepared
with, in this mount order:

- a temporary root first, so the host filesystem is hidden rather than merely
  not bound, then private `/proc`, `/dev`, `/tmp`, `/run` and `/home` mounts;
- the requested workspace bound read-only for `sandbox: read-only`, or read-write
  only for `sandbox: workspace-write`;
- a private agent state directory mounted at `/.acp-state`;
- no bind of the SQLite store home; a workspace or agent state directory inside
  the store home is refused before any command is built;
- `--unshare-net` unless the task explicitly allows network access;
- a new PID/session boundary tied to the parent process.

The workspace and state paths are resolved before the command is built, and a
private state path is required for every launch. Finding `bwrap` proves only
that the binary exists; whether unprivileged user namespaces are permitted is
learned when the sandbox starts, so the launch record must carry the actual
start outcome.

**Status in 0.1.5:** `boring_agent.acp` prepares and validates these launch plans
and is covered by unit tests. No worker runtime spawns the prepared command yet:
`worker --runtime` still offers `demo` and `llm` only. The contract below is what
that runtime must satisfy before it is added.

## Tier B: trusted operator

If bubblewrap is unavailable, the runtime cannot claim OS isolation. A
`read-only` ACP task is refused before launch (unknown capability also fails
closed). A writable task may be represented only as `trusted_operator`; this is
not sandboxing and must be visible in the attempt result and audit record.

Protocol callbacks are an audit trail, not a security boundary. Absolute callback
paths are mapped into workspace-relative paths that reject escapes and hidden
components; the mapped path must then be handed to the ordinary `Workspace` tool
policy, which also refuses symlinked components and the store home.

## Agent-owned permissions

An ACP agent's own permission system cannot be bypassed. Any permission mode that
never asks (`bypass`, `bypassPermissions`, a skip-permissions flag) is refused, for
prepared ACP launches and for native lifecycle sessions alike, whose default mode
is `ask`. The prepared environment uses an isolated home. The environment variables
that disable project-local hooks, MCP servers, subagents and skill discovery are
this project's convention; an adapter honors them only if it reads them, so the
launch record must state which restrictions the adapter confirmed.
