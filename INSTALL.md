# Installation Instructions

How to add the **boring-agent** project agent, compatibility skill, and rules to Codex, Coddy, Claude, and Cursor.

> **Note:** This repository is also a Git submodule. If you arrived here as a
> submodule of a parent project, the paths below are relative to the submodule
> root (e.g. `boring_agent/SKILL.md`).

---

## Codex (OpenAI Codex CLI)

Codex discovers project rules through `.codex/hooks.json` and
`.codex/hooks/attach_rules.py`. The submodule already contains the required
hook bridge that loads rules from `.cursor/rules/`.

### Option A — Install as a plugin (recommended)

1. Copy the plugin descriptor into Codex's plugin directory:

   ```bash
   mkdir -p ~/.codex/plugins/boring-agent
   cp .codex-plugin/plugin.json ~/.codex/plugins/boring-agent/
   ```

2. Copy the skill and hook files:

   ```bash
   cp SKILL.md ~/.codex/plugins/boring-agent/
   cp -r .codex/hooks.json .codex/hooks/ ~/.codex/plugins/boring-agent/ 2>/dev/null || true
   ```

3. Trust the hooks once per clone (Codex tracks by content hash):

   ```bash
   codex /hooks
   # or inside a Codex session:
   # > /hooks
   ```

After trust, the `attach_rules.py` hook runs automatically at session start
and before edits, injecting rules from `.cursor/rules/` into the context.

### Option B — Manual registration

If your Codex setup does not use plugins, symlink the repository into a path
Codex indexes:

```bash
ln -s "$(pwd)" ~/.codex/projects/boring-agent
```

Then trust hooks as shown above.

---

## Coddy

The canonical project agent is `.coddy/agents/boring-agent.md`. From this
checkout, inspect and approve it once for the workspace:

```bash
coddy agents list --cwd "$(pwd)"
coddy agents trust boring-agent --cwd "$(pwd)"
```

The approval is bound to the canonical workspace path and file digest. Editing
the definition requires a new approval. The definition intentionally omits a
model and permission mode, so both are inherited and can never be widened.

The project agent is a coordinator and must delegate every execution step to
the configured `exec` subagent. If `boring-agent` itself is spawned as a child,
that delegation is nested one level deeper. Set `subagents.max_depth: 2` (YAML:
`subagents: {max_depth: 2}`) or greater in Coddy's configuration, and ensure
that an `exec` definition is visible. The default depth of 1 lets the root
spawn `boring-agent` but withholds `spawn_agent` from it, so the project agent
will correctly stop with `BLOCKED` instead of bypassing `exec`.

`SKILL.md` remains a compatibility entry point for clients that discover
slash-command skills but not project agents. To install that optional wrapper
globally:

```bash
mkdir -p ~/.coddy/skills/boring-agent
cp SKILL.md ~/.coddy/skills/boring-agent/SKILL.md
coddy skills list | grep boring-agent
```

---

## Claude (Claude Code / Claude Desktop)

Claude Code needs no installation step inside this checkout. Opening the
repository root loads `CLAUDE.md` (a symlink to `AGENTS.md`) and every
`.claude/rules/*.md` file. Rules without frontmatter are always on; rules with
`paths:` frontmatter attach when Claude works on matching files. The
`.claude/rules/` files are maintained copies of `.cursor/rules/*.mdc` with
Claude frontmatter, so do not replace them with symlinks to the `.mdc` files:
Cursor `globs:` frontmatter is not read by Claude Code.

`.claude/settings.json` pre-approves the read-only and test commands used by
the development workflow. Put personal overrides in the git-ignored
`.claude/settings.local.json`.

### Optional: load the compatibility skill as a plugin

The repository root is also a single-skill Claude Code plugin
(`.claude-plugin/plugin.json` plus the root `SKILL.md`). Load it for one
session from a checkout:

```bash
claude --plugin-dir /path/to/boring-orch-agent
```

Check the manifest with `claude plugin validate .`. Copying `plugin.json` into
`~/.claude/plugins/` does not install a plugin.

---

## Cursor

Cursor natively reads `.cursor/rules/*.mdc` and discovers plugins through
`.cursor-plugin/`.

### Install

1. Copy the plugin descriptor:

   ```bash
   mkdir -p ~/.cursor/plugins/boring-agent
   cp .cursor-plugin/plugin.json ~/.cursor/plugins/boring-agent/
   ```

2. The rules are already present in `.cursor/rules/*.mdc`. If you are consuming
   this repository as a submodule, ensure the `.cursor/rules/` directory is
   included in the editor workspace so Cursor indexes it.

3. No additional action is required — Cursor picks up `.mdc` rules automatically
   when the workspace root contains `.cursor/rules/`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Codex does not load rules | Hooks not trusted | Run `codex /hooks` and approve |
| Coddy refuses the project agent | Definition needs workspace approval | Run `coddy agents trust boring-agent --cwd "$(pwd)"` |
| `boring-agent` reports `BLOCKED` before execution | `exec` is missing or nested spawning is limited to depth 1 | Configure `exec` and set `subagents.max_depth: 2` or greater |
| Coddy does not list compatibility skill | `skills.dirs` missing path | Check the configured skill directories and the copied `SKILL.md` |
| Claude Code ignores rules | Session started outside the repository root, or `.claude/rules/*.md` missing | Start Claude Code in the repository root; restore the rule files from Git |
| Cursor rules not active | `.cursor/rules/` not in workspace | Add submodule folder to workspace root |

---

## Keeping in sync

When agent instructions change, mirror the updates across all rule trees in the
same commit per the [Rules Sync](AGENTS.md#rules-sync) contract:

- `AGENTS.md` / `CLAUDE.md` (root, symlink)
- `.cursor/rules/*.mdc`
- `.claude/rules/*.md`
- `.codex/rules.md`
- `.coddy/rules/*.md`
- `.coddy/agents/*.md`
