# Enforcement gate for the accepted loadout

Date: 2026-09-02 | Status: approved design | Target: loadout v1.4.0

## Problem

`apply.py` wires the accepted workflow into AGENTS.md / CLAUDE.md as prose. A model can
read it and still skip stages. The user wants the wired workflow to be binding: once a
project carries a LOADOUT.md, the agent has no in-session way to edit before the planning
stage or to finish with a binding stage never run.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Bite point | Both: PreToolUse denies edits before stage 1; Stop refuses to end with binding stages skipped |
| Binding set | Accepted lines whose stage label does not start with `situational` |
| Escape hatch | User only, from outside the session: `LOADOUT_ENFORCE=0` or removing LOADOUT.md. No agent-side override |
| Ledger | The session transcript (JSONL) that every hook receives as `transcript_path`. No state file, no PostToolUse hook |
| Bash writes | Gated too: PreToolUse on Bash denies write-shaped commands before stage 1 |
| Hosts | Claude Code in this version. Other hosts keep prose wiring; the report says where enforcement is live |

## Components

### `scripts/gate.py` (new, stdlib only)

```
python gate.py pre    # PreToolUse hook: Edit|Write|MultiEdit|NotebookEdit|Bash
python gate.py stop   # Stop hook
```

Reads the hook JSON from stdin (`cwd`, `transcript_path`, `tool_name`, `tool_input`,
`stop_hook_active`). Exit 0 with no output means "allow".

Silent allow (exit 0, no output) when any of:
- `LOADOUT_ENFORCE` is `0`
- no LOADOUT.md is found walking up from `cwd` to the filesystem root
- LOADOUT.md has no binding Accepted lines
- `stop` mode and `stop_hook_active` is true (harness loop guard)
- `stop` mode and the transcript shows no edit in this session (no Edit/Write/MultiEdit/
  NotebookEdit tool_use and no write-shaped Bash command), so question-only sessions
  are never trapped
- `pre` mode for Bash and the command is not write-shaped
- `pre` mode for Bash and the command is exactly a validated `apply.py` bootstrap
  invocation (see "Bootstrap boundary" below)

### Bootstrap boundary (no blanket exemption)

The loadout-apply phase is a one-time bootstrap that runs before enforcement is installed.
After activation, writes to LOADOUT.md, AGENTS.md and CLAUDE.md are gated like every other
mutation: an agent must not be able to alter the enforced workflow before satisfying it.
The single exception is re-running the bootstrap itself, and only in its exact shape:

- the command has no `|`, `;`, `&`, `<`, `>`, backtick, `$(` or newline
- tokens (shlex, POSIX rules) are: an interpreter whose basename is `python`, `python3`,
  `python.exe` or `py`, or equals `sys.executable`; a script whose basename is `apply.py`;
  exactly one positional project-dir token; and any of `--host <x>`, `--loadout <y>`,
  `--no-enforce`; nothing else
- Edit/Write/MultiEdit/NotebookEdit calls targeting those files are never exempt

A re-audit that changes the accepted set therefore needs the stage-1 skill to have run
first, or the operator hatch for that session. That is deliberate.

Invoked set: every `tool_use` block in the transcript with `name == "Skill"` contributes
`input.skill`; every user message containing `<command-name>/<x></command-name>`
contributes `x`. Match is exact against the accepted skill string.

`pre` decision: stage 1 = first binding line. Bootstrap invocations (above) are allowed first. If its skill is not in the invoked set, emit

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
 "permissionDecisionReason": "Loadout gate: invoke `<skill>` (<stage>) before editing. Details in LOADOUT.md. Operator hatch: LOADOUT_ENFORCE=0."}}
```

`stop` decision: missing = binding minus invoked. If non-empty, emit

```json
{"decision": "block", "reason": "Loadout gate: stages not run this session: <stage> (`<skill>`), ... Invoke them, then stop. Operator hatch: LOADOUT_ENFORCE=0."}
```

Write-shaped Bash command (regex, single source of truth in gate.py): a redirect `>` or
`>>` that is not `2>&1`, `>/dev/null`, `> NUL`; a heredoc `<<`; or a leading/`|`/`&&`/`;`
-separated word in {`tee`, `sed -i`, `perl -i`, `mv`, `cp`, `install`, `patch`,
`git apply`, `git checkout --`, `git restore`, `touch`}; or any mention of `apply.py`, `gate.py`,
`settings.local.json`, `LOADOUT.md`, `AGENTS.md` or `CLAUDE.md` (the enforcement surface is a
mutation unless the command is the exact bootstrap). False positives are accepted and
named in the deny reason; the hatch is the recourse. Ceiling: an arbitrary script that writes
files (`python other.py`) is not write-shaped.

Never raises: any parse error (bad JSON, unreadable transcript, malformed LOADOUT.md)
allows with exit 0. Enforcement must not break the harness.

### `scripts/apply.py` (extended)

When `--host claude-code`, upsert two hook registrations into
`<project>/.claude/settings.local.json` (local, not shared: the command embeds this
machine's path to the installed `gate.py`):

```json
{"hooks": {
  "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
                  "hooks": [{"type": "command", "command": "python \"<skill-dir>/scripts/gate.py\" pre"}]}],
  "Stop":       [{"hooks": [{"type": "command", "command": "python \"<skill-dir>/scripts/gate.py\" stop"}]}]}}
```

Idempotent: an entry whose command contains `gate.py` is replaced, never duplicated;
other hooks and keys in the file are preserved. `<skill-dir>` is the directory containing
apply.py. Output adds a line `- .claude/settings.local.json: <action> (hooks take effect
from the next Claude Code session)`. A new flag `--no-enforce` skips this step.

### SKILL.md / README

- Step 5 action 2 gains: "On Claude Code this also registers the enforcement gate; say
  that it takes effect from the next session."
- Report template gains a line under the header:
  `Enforcement: claude-code gate registered | prose only` and the Accepted section's
  convention is stated: stage labels starting with `situational` are not binding.
- README documents gate.py, the hatch, and the two ceilings: Claude Code overrides a Stop
  hook after 8 consecutive blocks without progress; only Claude Code enforces in this
  version.
- `SKILL_FILES` in scan.py gains `scripts/gate.py` so self-install ships it.

## Testing

`tests/test_gate.py` drives gate.py as a subprocess with synthetic transcripts and a
temp project:
- no LOADOUT.md → allow; `LOADOUT_ENFORCE=0` → allow
- pre/Edit before stage 1 → deny naming the stage-1 skill; after a Skill tool_use of it → allow
- pre/Bash: `cat > f <<EOF` denied, `pytest -q` allowed, `cmd 2>&1 >/dev/null` allowed
- slash-command invocation counts as invoked
- stop: no edits in session → allow; edits + missing binding stage → block naming it;
  situational stage missing → allow; `stop_hook_active` → allow
- malformed transcript line → allow
`tests/test_apply.py` gains: settings.local.json created, re-run does not duplicate,
existing unrelated hooks preserved, `--no-enforce` skips.

## Out of scope

Codex / Cursor / Gemini gates (stop-hook blocking semantics unverified), per-line
`(required)` markers, any agent-side override.

## Implementation notes (post-review, 2026-09-02)

Amendments the code review surfaced; the code is the reference for these:

- Slash-command invocations count only from user messages (transcript `type == "user"`), so an
  agent cannot write `<command-name>/x</command-name>` in its own text to pass the gate.
- Naming the enforcement surface (`apply.py`, `gate.py`, `settings.local.json`, LOADOUT.md,
  AGENTS.md, CLAUDE.md) is gated in `pre` mode only; it does not mark the session as edited, so a
  read-only session that merely reads those files is never trapped by Stop.
- The hook command is `"<sys.executable>" "<skill-dir>/scripts/gate.py" <mode>`, not bare
  `python`, so it works where `python` is not on PATH. That is why the file is local.
- `register_gate` returns `created | updated | unchanged`; it removes only its own hook commands and
  keeps sibling hooks inside the same matcher entry. `settings.local.json` is parsed before any
  file is written, so a corrupt file fails the whole apply cleanly.
- Redirects `1>` and `&>` count as write-shaped; `2>&1` and `>/dev/null` do not.
- A gate that hits an internal error still allows (exit 0, no stdout) but prints the traceback to
  stderr, so a broken gate is visible in hook debug output rather than a silent no-op.
- A denied Edit/Write attempt is still a tool_use in the transcript, so it counts as an edit for the
  Stop gate. Kept deliberately: intent to mutate is enough to require the workflow. Only a session
  that never attempted an edit is exempt.
- Additions beyond the original spec, kept: the scanner reports `enforcement gate registered
  (claude-code)` in the project section for re-audits; `.claude/settings.local.json` is gitignored
  because it embeds machine paths.
