# Codex CLI adapter for the enforcement gate

Date: 2026-09-02 | Status: proven live 2026-09-02 | Target: loadout v1.5.0 | Matrix row: `docs/host-capability-matrix.md`

## Goal

The same enforcement Claude Code has, on Codex CLI: no file mutation or write-shaped shell
command before the stage-1 skill has been invoked, no stopping while a binding stage was never
invoked, operator-only hatch, exact-bootstrap boundary. Proven live in a headless `codex exec`
run before the matrix row says `proven`.

## Verified facts this design rests on (2026-09-02, live probes + source)

- Codex hook config is Claude-shaped (`{"hooks":{"PreToolUse":[{"hooks":[{"type":"command",...}]}]}}`)
  and the deny and block JSON contracts are the Claude ones: `hookSpecificOutput.permissionDecision`
  and `{"decision":"block","reason"}`. `stop_hook_active` is on Stop stdin. The gate's output
  dialect is therefore identity; only input normalisation and the ledger differ.
- Hooks run through the session's detected shell. On this machine that is PowerShell
  (`-NoProfile -Command`), so a quoted executable at command position must use the call
  operator. Codex supports a `command_windows` override; registration writes both forms.
- The hook child gets the full environment snapshot of the Codex process (`env_clear` then
  replay), so `LOADOUT_ENFORCE=0` set in the launching shell reaches the gate.
- Untrusted hooks are skipped silently, never failed. Trust = `[hooks.state.'<file>:<event>:<i>:<j>']
  trusted_hash` in config.toml (SHA-256 of the normalised handler). `codex exec` has
  `--dangerously-bypass-hook-trust` for automation. The hash is reproduced in apply.py
  (`codex_hook_hash`, verified against every recorded entry on a real machine), so apply grants trust
  itself; `/hooks` is never needed.
- Project-level `.codex/hooks.json` loads only for trusted projects. Registration is therefore
  user-level (`~/.codex/hooks.json`, `CODEX_HOME` honoured): the gate is a no-op wherever no
  LOADOUT.md is found, so one registration serves every project.
- Ledger = the rollout JSONL (`transcript_path` on stdin, nullable). Verified shapes:
  `session_meta.payload.cwd`; `event_msg/item_completed` items `FileChange` (changes map),
  `CommandExecution` (command list + cwd), `UserMessage` (prompt text); `response_item/custom_tool_call`
  named `exec` whose input is JS calling `tools.apply_patch` / `tools.exec_command`.
  Skill use: only a `CommandExecution` whose `parsed_cmd` reads `<skills root>/<name>/SKILL.md`
  counts (verified live; there is no skill event). A `$name` mention in the prompt is the user's
  intent, not the agent loading the skill, and does not count.
- Stop block feedback is injected as the next prompt and recorded as an `item_completed` of type
  `HookPrompt` (fragments[].text); consecutive blocks are counted from those, reset by a skill invocation.

## Facts added by the recorder probes (2026-09-02, live)

- PreToolUse stdin is Claude-shaped: `tool_name` is `Bash` with `tool_input.command` for shell, and
  `apply_patch` with the patch text as `tool_input.command` for edits; `transcript_path`, `cwd`,
  `session_id`, `turn_id`, `tool_use_id`, `permission_mode` are present.
- Stop stdin carries `session_id`, `stop_hook_active`, `last_assistant_message`, `cwd`, but NO
  `transcript_path`; the gate locates `<CODEX_HOME>/sessions/*/*/*/rollout-*-<session_id>.jsonl`.
- The `& "<python>" "<gate.py>" <mode> --host codex` form runs under Codex's PowerShell hook shell
  (probe: both PreToolUse and Stop completed); an unquoted-call form with a quoted path fails.
- Skill use has no rollout event. It appears as a `CommandExecution` whose `parsed_cmd` reads
  `<skills root>/<name>/SKILL.md`; that read is the only invocation signal and resets the block run.
- Headless `codex exec` must run with stdin closed (`< /dev/null`) or it waits for more prompt input.
- `--approve-for-me` cannot be combined with `-s`; use `-s workspace-write -c approval_policy="never"`.

## Components

- `scripts/gate_codex.py`: `transcript_facts(path) -> gate.Facts`, `is_codex_transcript(path)`.
  Same tolerance contract as gate.py (never raises, bad lines skipped).
- `scripts/gate.py`: `--host codex` flag (or auto-detect from `is_codex_transcript`) selects the
  ledger parser and the tool-name normalisation. Codex's PreToolUse `tool_name`/`tool_input`
  shape is taken from the recorder probe; the gate maps it onto its existing edit / Bash paths.
  Decision logic, bootstrap boundary, surface rules and caps are shared, not duplicated.
- `scripts/apply.py`: `register_codex_gate()` upserts both hook entries into `~/.codex/hooks.json`
  idempotently (own entries replaced, sibling hooks preserved); `apply(..., host="codex")` calls it.
- `docs/host-capability-matrix.md`: Codex row flips to `proven` only after the live run.

## Live proof (required)

Headless `codex exec -s workspace-write -c approval_policy="never" -c 'sandbox_permissions=["disk-full-read-access"]'
--dangerously-bypass-hook-trust -c 'projects."<dir>".trust_level="trusted"' < /dev/null` in a long-form-path
scratch project carrying a LOADOUT.md (ran 2026-09-02, both runs passed): (1) an edit requested with no skill mentioned is denied with the
gate reason; (2) after a `$<stage-1 skill>` mention and an edit, stopping is blocked naming the
missing stages, repeatedly, with the file state checked afterwards.

## Out of scope

Every other host. Cursor and Grok stay `unverified` even though they read Claude-format hooks.

## Second-round review (2026-09-02)

- `*** Move to:` targets in an apply_patch are checked against the surface too.
- The Codex hook config is enforcement surface: `hooks.json` and `config.toml` under any `.codex`
  directory are operator-owned at every stage; a project's own `config.toml` is not.
- `trust_codex_gate` matches `[hooks.state.'<key>']` sections line-anchored and replaces the whole
  section up to the next table header, so a stray comment can never leave two `trusted_hash` keys.
- `find_rollout` escapes the session id before globbing.

## Contract review (2026-09-02): two redesigns before merge

- A bare `$skill` mention no longer counts as invoking a skill. Only a real SKILL.md read does
  (Codex has no native invocation event). The mention path and its regex are removed.
- The Stop-block cap does not apply on Codex. On Claude Code the host itself releases a Stop hook
  after 8 consecutive blocks, so yielding there mirrors a limit that exists anyway. Codex has no such
  limit, so a gate-side cap would be an in-session bypass, which the disablement contract
  (external-only) forbids. The gate now blocks every time on Codex; the block count is kept only for
  the stderr note. Cost: an unattended Codex session that refuses its stages loops until the operator
  ends it. Reinstating a cap on Codex requires explicit approval as a deliberately weaker policy.
