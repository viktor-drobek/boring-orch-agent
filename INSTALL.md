# Installation Instructions

How to add the **boring-orch-agent** skill / project rules to Codex, Coddy, Claude, and Cursor.

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
   mkdir -p ~/.codex/plugins/boring-orch-agent
   cp .codex-plugin/plugin.json ~/.codex/plugins/boring-orch-agent/
   ```

2. Copy the skill and hook files:

   ```bash
   cp SKILL.md ~/.codex/plugins/boring-orch-agent/
   cp -r .codex/hooks.json .codex/hooks/ ~/.codex/plugins/boring-orch-agent/ 2>/dev/null || true
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
ln -s "$(pwd)" ~/.codex/projects/boring-orch-agent
```

Then trust hooks as shown above.

---

## Coddy

Coddy discovers skills from `skills.dirs` (defaults include `~/.coddy/skills/` and
`${CWD}/.coddy/skills/`).

### Quick install

```bash
# 1. Link or copy the skill into Coddy's skill directory
mkdir -p ~/.coddy/skills
ln -s "$(pwd)" ~/.coddy/skills/boring-orch-agent

# 2. (Optional) Add to skills.dirs if not already covered
coddy config set skills.dirs='["~/.agents/skills", "~/.coddy/skills", "${CWD}/.coddy/skills"]'
```

### Verify

```bash
coddy skills list | grep boring-orch-agent
```

The skill auto-activates when the user asks about orchestrator tasks, workers,
managers, or the `boring-orch-agent` CLI / API.

---

## Claude (Claude Code / Claude Desktop)

Claude Code reads `.claude/rules/*.md` for project-specific instructions.

### Install

1. Copy the plugin descriptor:

   ```bash
   mkdir -p ~/.claude/plugins/boring-orch-agent
   cp .claude-plugin/plugin.json ~/.claude/plugins/boring-orch-agent/
   ```

2. Copy the skill file (Claude Code ignores `SKILL.md` by default, but keeps it
   for reference):

   ```bash
   cp SKILL.md ~/.claude/plugins/boring-orch-agent/
   ```

3. Symlink the rules into the project's `.claude/rules/` (if working inside the
   submodule directly):

   ```bash
   mkdir -p .claude/rules
   for f in .cursor/rules/*.mdc; do
     ln -s "$(realpath "$f")" ".claude/rules/$(basename "$f" .mdc).md"
   done
   ```

> **Important:** Claude Code does not natively read `.mdc` files. The symlink
> step converts Cursor's `.mdc` rules into `.md` files Claude Code understands.
> Keep the content equivalent per the [Rules Sync](AGENTS.md#rules-sync) contract.

---

## Cursor

Cursor natively reads `.cursor/rules/*.mdc` and discovers plugins through
`.cursor-plugin/`.

### Install

1. Copy the plugin descriptor:

   ```bash
   mkdir -p ~/.cursor/plugins/boring-orch-agent
   cp .cursor-plugin/plugin.json ~/.cursor/plugins/boring-orch-agent/
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
| Coddy does not list skill | `skills.dirs` missing path | Check `coddy config get skills.dirs` and add `~/.coddy/skills` |
| Claude Code ignores rules | `.claude/rules/*.md` missing | Symlink or copy `.mdc` content as `.md` |
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
