# ACP isolation contract

ACP launches are classified by what the host can enforce, not by what an agent or
callback claims.

## Tier A: bubblewrap

On Linux, when `bwrap` is available and its capability is known, a protected ACP
launch is prepared with:

- a temporary root and private `/tmp`, `/run` and `/home` mounts;
- the requested workspace bound read-only for `sandbox: read-only`, or read-write
  only for `sandbox: workspace-write`;
- a private agent state directory mounted at `/.acp-state`;
- no bind of the SQLite store home or other store-managed paths;
- `--unshare-net` unless the task explicitly allows network access;
- a new PID/session boundary tied to the parent process.

The workspace and state paths are resolved before the command is built. A command
list is evidence of the intended boundary; the process launcher must still record
the resolved route and executable identity.

## Tier B: trusted operator

If bubblewrap is unavailable, the runtime cannot claim OS isolation. A
`read-only` ACP task is refused before launch (unknown capability also fails
closed). A writable task may be represented only as `trusted_operator`; this is
not sandboxing and must be visible in the attempt result and audit record.

Protocol callbacks are an audit trail, not a security boundary. Absolute callback
paths are mapped into workspace-relative paths and run through the same visible,
non-escaping path checks as ordinary tools. The store home is never granted as a
workspace.

## Agent-owned permissions

An ACP agent's own permission system cannot be bypassed. `bypass` is refused.
The prepared environment uses an isolated home and disables project-local hooks,
MCP servers, subagents and automatic skill discovery. These restrictions are
independent of bubblewrap and remain part of the launch record.
