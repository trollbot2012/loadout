# loadout

Universal harness-audit skill: inventories the coding agent you're running in
(skills, plugin-provided skills, plugins, registered hooks, commands, agents, MCP servers,
with on/off state — across every harness installed on the machine), then recommends
the best workflow of skills for the project you're building, wires the accepted
loadout into the project's agent config, and starts the work.

- `SKILL.md` — the skill (portable agent-skills format)
- `scripts/scan.py` — stdlib-only inventory scanner (facts; the model does the judgment)
- `scripts/apply.py` — idempotent writer for the `## Loadout` section (AGENTS.md + native file)
- `scripts/gate.py` — Claude Code hook that makes the accepted loadout binding (deny edits before stage 1, block stopping while a binding stage is missing)

## Why an invoked skill, not a hook or plugin

Hooks and plugins are host-specific; SKILL.md is the only convention that installs
across Claude Code, Codex, Cursor, OpenCode, Gemini CLI, Qwen, Grok, Crush, Copilot, etc.
An audit is on-demand advice, so it's invoked (at project start), not event-driven.

## Install

One-liner (installs into your agents via the skills CLI; add `-g` for user-level):

```bash
npx skills add trollbot2012/loadout
```

Or copy the folder into your harness's skills dir manually:

| Host | Destination |
|------|-------------|
| Claude Code | `~/.claude/skills/loadout/` (or `$CLAUDE_CONFIG_DIR/skills/`) |
| Codex | `~/.agents/skills/loadout/` (`~/.codex/skills/` is legacy but still read) |
| Cursor | `~/.cursor/skills/loadout/` or `~/.agents/skills/loadout/` |
| OpenCode | `~/.config/opencode/skills/loadout/` or `~/.agents/skills/loadout/` |
| Gemini CLI | `~/.gemini/skills/loadout/` or `~/.agents/skills/loadout/` |
| Copilot CLI | `~/.copilot/skills/loadout/` or `~/.agents/skills/loadout/` |
| Grok / Crush | their `skills/` dir or `~/.agents/skills/loadout/` |
| DeepSeek Harness | `~/.dsh/skills/loadout/` (`$DSH_HOME` overrides) |
| Qwen / Continue / Vibe / Hermes / zcode | their respective `skills/` dir |

`~/.agents/skills` is the shared pool read natively by Codex, Gemini, Cursor, OpenCode,
Copilot, Grok and Crush. Claude Code does not read it.

Then invoke: `/loadout` (or "audit my harness and recommend a workflow").
After the report, every recommendation is offered for selection (checkbox picker where
the harness has one, numbered list otherwise). Accepted skills are written to
`LOADOUT.md` and, via `scripts/apply.py`, into the project's `AGENTS.md` and the running
host's native instruction file (`CLAUDE.md` with an `@AGENTS.md` import, `GEMINI.md`,
`QWEN.md`). Re-audits replace the section instead of adding another. The run ends by naming the
first task and starting it, not by saving a file.

## Enforcement (Claude Code)

`apply.py --host claude-code` also registers `scripts/gate.py` as a PreToolUse and Stop hook
in the project's `.claude/settings.local.json` (local: the command embeds this machine's
path). From the next session the agent cannot edit files, or run a write-shaped shell
command, before the stage-1 skill has been invoked, and cannot stop while any binding stage
(every Accepted line not labelled `situational`) was never invoked. The ledger is the
session transcript; there is no state file and no agent-side override.

Operator hatch: `LOADOUT_ENFORCE=0` in the environment, or remove LOADOUT.md. Skip
registration with `--no-enforce`. Writes to LOADOUT.md, AGENTS.md and CLAUDE.md are gated
like any other edit once the gate is active; only an exact `apply.py` invocation is allowed
as a re-bootstrap, so a re-audit on an enforced project needs the stage-1 skill first or the
hatch. Ceilings: Claude Code overrides a Stop hook after 8 consecutive blocks without
progress; the shell write check is a heuristic that can misfire on an innocent command and
does not see writes made by an arbitrary script. Other hosts keep prose wiring in this version.

Self-install, check and update from a source checkout:

```bash
python scripts/scan.py --check                      # exit 1 if any copy is stale or missing
python scripts/scan.py --self-install               # table hosts present on this machine (+ ~/.agents)
python scripts/scan.py --self-install --hosts all   # every discovered <root>/skills dir
```

The scanner also runs standalone:

```bash
python scripts/scan.py [--json] [--brief] [project_dir]
```

Extras: `LOADOUT_HOST=<host>` overrides harness detection (env markers are child-shell
signals and stay `unknown` without one); the value is a table key (`claude-code`, `codex`,
`cursor`, `gemini`, `opencode`, `crush`, `qwen`, `continue`, `copilot`, `grok`, `vibe`,
`deepseek`, `hermes`, `zcode`), with `claude` and `dsh` as aliases; `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GROK_HOME`,
`VIBE_HOME`, `HERMES_HOME`, `DSH_HOME` and `XDG_CONFIG_HOME` are honoured. Output starts with the
project (including prior-loadout signals for re-audits), then the current host's full
listing with `(off)` markers, then cross-host coverage and MCP.

Requires Python 3.9+; stdlib only. MIT licensed.
