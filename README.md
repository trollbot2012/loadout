# loadout

Universal harness-audit skill: inventories the coding agent you're running in
(skills, plugins, hooks, commands, agents, MCP servers — across every harness
installed on the machine), then recommends the best workflow of skills for the
project you're building.

- `SKILL.md` — the skill (portable agent-skills format)
- `scripts/scan.py` — stdlib-only inventory scanner (facts; the model does the judgment)

## Why an invoked skill, not a hook or plugin

Hooks and plugins are host-specific; SKILL.md is the only convention that installs
across Claude Code, Codex, Cursor, OpenCode, Gemini CLI, Qwen, Grok, Crush, etc.
An audit is on-demand advice, so it's invoked (at project start), not event-driven.

## Install

Copy the folder into your harness's skills dir:

| Host | Destination |
|------|-------------|
| Claude Code | `~/.claude/skills/loadout/` |
| Codex | `~/.codex/skills/loadout/` |
| Cursor | `~/.cursor/skills/loadout/` |
| OpenCode | `~/.config/opencode/skills/loadout/` |
| Gemini CLI | `~/.gemini/skills/loadout/` |
| Qwen / Grok / Crush / Continue | their respective `skills/` dir |
| DeepSeek Harness (and any `.agents`-standard host) | `~/.agents/skills/loadout/` |

Then invoke: `/loadout` (or "audit my harness and recommend a workflow").

Or let it install itself everywhere at once (copies into every detected harness
with a `skills/` dir):

```bash
python scripts/scan.py --self-install
```

The scanner also runs standalone:

```bash
python scripts/scan.py [--json] [project_dir]
```

Extras: `LOADOUT_HOST=<name>` overrides harness detection; output includes a
cross-host coverage section showing which skills are missing from the current
harness but installed in others.
