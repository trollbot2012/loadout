# Host capability matrix for the enforcement gate

Researched 2026-09-02 from official documentation and source. Every row cites its source; "not
documented" means exactly that. **Proof status is the only column that counts for a claim of
enforcement**: config compatibility (a host reading Claude-format hooks) is not proof.

Proof status values: `proven` (headless live run in that host: an edit denied before stage 1 and
a stop blocked with stages missing, transcript inspected), `unverified` (mechanism documented,
no live run), `not started`, `n/a` (no mechanism).

| Host | Pre-tool deny | Stop block | Ledger source | Proof status |
|---|---|---|---|---|
| Claude Code | hard | hard (host cap 8) | `transcript_path` JSONL | **proven** 2026-09-02 (gate.py v1.4.0) |
| Codex CLI | hard | hard (no host cap; gate never yields, operator ends it) | rollout JSONL via `transcript_path` (pre) / session id (stop) | **opt-in only** (`--enforce-codex`): mechanism proven 2026-09-02 (gate_codex, v1.5.0), but registering it crashed the Codex 0.152.1 desktop app-server — see README |
| Qwen Code | hard | hard | `transcript_path` JSONL | not started |
| Gemini CLI | hard | hard (re-prompt) | `transcript_path` | not started |
| Copilot CLI | hard (timeout fails open) | hard (cap 8) | `session-state/<id>/events.jsonl` (schema undocumented) | not started |
| zcode | hard | block, cap 3 | temp `transcript_path` | not started |
| Hermes | hard (Python `pre_tool_call`) | middleware (finish-gate exists) | own session store | not started |
| Cursor | hard | follow-up inject only (loop_limit 5) | `transcript_path` | **unverified** (reads `.claude/settings.local.json` hooks; not proven live) |
| Mistral Vibe | hard | post_agent deny, max 3 retries | `messages.jsonl` | not started |
| OpenCode | hard (JS plugin) | none (session.idle is notify) | SQLite | not started |
| Grok Build | hard (fail-open on timeout) | none (Stop passive) | `updates.jsonl` | **unverified** (reads `.claude/settings.json`; not proven live) |
| Crush | hard (PreToolUse only, exit 49 halts) | none | SQLite | not started |
| Continue CLI | hard (undocumented) | undocumented | `~/.continue/sessions/*.json` | not started |
| DeepSeek Harness | plugin waterfall (undocumented shape) | undocumented | undocumented | not started |

## Per-host detail

### Claude Code (proven)
- Events used: PreToolUse (`Edit|Write|MultiEdit|NotebookEdit|Bash|EnterWorktree|mcp__*`), Stop.
- Deny: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":...}}`. Block: `{"decision":"block","reason":...}`; `stop_hook_active` on stdin; host overrides after 8 consecutive blocks.
- Ledger: `transcript_path` JSONL; `Skill` tool_use blocks carry `input.skill`; slash commands appear as `<command-name>/x</command-name>` in user content; Stop blocks appear as user lines starting `Stop hook feedback:`.
- Source: https://code.claude.com/docs/en/hooks-guide.md

### Codex CLI (proven)
- Config: `~/.codex/hooks.json`, `~/.codex/config.toml [hooks]`, `<repo>/.codex/hooks.json` (trusted layer only). Claude-shaped `{"hooks":{"PreToolUse":[{"matcher":"^Bash$","hooks":[{"type":"command","command":...}]}]}}`; `[features] hooks = true` default on.
- Events: SessionStart, SessionEnd, SubagentStart, SubagentStop, PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact, UserPromptSubmit, Stop, Interrupt.
- Deny: exit 2 + stderr, or exit 0 with `hookSpecificOutput.permissionDecision:"deny"` (+`permissionDecisionReason`); legacy `{"decision":"block"}`. Only sync hooks block. Reason surfaces as model-visible Feedback.
- Block: Stop `{"decision":"block","reason":...}` or exit 2; `stop_hook_active` on stdin; matchers ignored for Stop.
- Stdin: `session_id, turn_id, cwd, hook_event_name, model, permission_mode, transcript_path` (nullable); PreToolUse adds `tool_name, tool_input, tool_use_id`. Hook env = the Codex process's own environment snapshot replayed after `env_clear` (verified in source and by a recorder hook), so `LOADOUT_ENFORCE=0` set in the shell that launches Codex reaches the gate.
- Ledger: `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`; lines `session_meta`, `turn_context`, `event_msg`, `response_item`. Verified live: edits = `item_completed` FileChange, shell = CommandExecution (`parsed_cmd` reads), skill use = a parsed read of `<skills>/<name>/SKILL.md` only (a `$name` mention is not invocation), injected Stop-block reason = `HookPrompt` item. PreToolUse stdin is Claude-shaped (`Bash`, `apply_patch`); Stop stdin has no `transcript_path` (located by `session_id`). Hooks run through PowerShell here: `command_windows` uses the call operator. Codex has no block cap and the gate adds none on this host: blocking continues until the operator intervenes.
- Proof (headless `codex exec`, 2026-09-02, re-run after the contract tightening): run 1, no skill read: 4 edit denials, 36 consecutive Stop blocks, never released, ended only when the 150 s external timeout killed it (exit 124), README untouched. Run 2, SKILL.md actually read: no denials, the edit applied, 4 Stop blocks until both binding stages' SKILL.md had been read, then a clean finish (exit 0).
- Note: Codex does not surface hook stderr, so the gate's runaway note is invisible to the operator; repeated `hook: Stop Blocked` lines are the only signal. `hook: Stop Failed` lines in these runs come from an unrelated pre-existing Stop hook on the machine, not from the gate. Run 2 with `$planning-with-files`: skill read counted, edit allowed, Stop blocked with review missing until the review skill was read.
- Sources: https://learn.chatgpt.com/docs/hooks ; https://github.com/openai/codex/blob/main/codex-rs/hooks/src/events/pre_tool_use.rs ; https://github.com/openai/codex/blob/main/codex-rs/hooks/src/schema.rs ; https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder.rs ; https://github.com/openai/codex/blob/main/codex-rs/hooks/schema/generated/pre-tool-use.command.input.schema.json

### Qwen Code
- Config `~/.qwen/settings.json`, `.qwen/settings.json` (trusted folders). Events incl. PreToolUse, Stop, SubagentStart/Stop. Deny and block use the Claude shapes; `stop_hook_active`. Stdin `session_id, transcript_path, cwd, tool_name, tool_input, tool_use_id`. Ledger `~/.qwen/projects/<sanitized-cwd>/chats/<sessionId>.jsonl`; skill representation not documented.
- Source: https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/ ; https://raw.githubusercontent.com/QwenLM/qwen-code/main/docs/users/features/hooks.md

### Gemini CLI
- Config `settings.json` (`.gemini/settings.json` project > user). Events incl. BeforeTool, AfterTool, BeforeAgent, AfterAgent. Deny: BeforeTool `{"decision":"deny","reason"}` or exit 2 (reason sent as tool error). Block: AfterAgent `{"decision":"deny","reason"}` re-prompts; `stop_hook_active`; no cap documented. Stdin `session_id, transcript_path, cwd`. Ledger `~/.gemini/tmp/<project_hash>/chats/`; skills activate via the `activate_skill` tool.
- Source: https://geminicli.com/docs/hooks/reference/ ; https://geminicli.com/docs/tools/activate-skill/

### Copilot CLI
- Config `~/.copilot/hooks/*.json`, `.github/hooks/*.json`, settings inline. Events incl. preToolUse, agentStop, subagentStop. Deny `{"permissionDecision":"deny","permissionDecisionReason"}` (mandatory reason); non-zero exit = deny; **timeout fails open**. Block: agentStop `{"decision":"block","reason"}`, cap 8. Stdin camelCase `sessionId, cwd, toolName, toolArgs`; `transcriptPath` only on agentStop/subagentStop. Ledger `~/.copilot/session-state/<id>/events.jsonl` (schema not documented; `skill.invoked` reported unofficially).
- Source: https://docs.github.com/en/copilot/reference/hooks-reference ; https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference

### zcode
- Config `~/.zcode/cli/config.json`; project hooks ignored. Deny exit 2 or `hookSpecificOutput.permissionDecision:"deny"`. Stop `{"decision":"block"}` continues max 3 then force-end. `transcript_path` is a temp JSONL deleted afterwards.
- Source: https://zcode.z.ai/en/docs/hooks

### Cursor (unverified)
- Config `~/.cursor/hooks.json`, `.cursor/hooks.json`; also reads Claude Code `.claude/settings.json` and `.claude/settings.local.json` and maps Claude outputs. preToolUse/beforeShellExecution `{"permission":"deny","agent_message"}`; stop `{"followup_message"}` with `loop_limit` (default 5, null for Claude-format hooks). Stdin `transcript_path`; on-disk path not documented. CLI hook firing has an open bug report (forum.cursor.com/t/168326).
- Source: https://cursor.com/docs/hooks ; https://cursor.com/docs/reference/third-party-hooks

### Mistral Vibe
- `~/.vibe/hooks.toml`, `.vibe/hooks.toml`; pre_tool deny `{"decision":"deny"}`; post_agent deny retries max 3. Stdin `session_id, transcript_path, cwd, tool_name, tool_input`. Ledger `~/.vibe/logs/sessions/session_<ts>/messages.jsonl`.
- Source: https://github.com/mistralai/mistral-vibe (README, Hooks)

### OpenCode
- JS/TS plugins (`~/.config/opencode/plugins/`, `.opencode/plugins/`): `tool.execute.before` throw to block; `permission.ask` sets `output.status`. No stop hook. Ledger SQLite `~/.local/share/opencode/opencode.db`.
- Source: https://opencode.ai/docs/plugins/ ; https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/index.ts

### Grok Build (unverified)
- `~/.grok/hooks/*.json`, `.grok/hooks/*.json` (`/hooks-trust`); also reads `.claude/settings.json` and `.cursor/hooks.json`. PreToolUse deny `{"decision":"deny"}` or exit 2; timeouts fail open; Stop passive. Stdin incl. `transcript_path` (format "not stable"). Ledger `~/.grok/sessions/<encoded-cwd>/<uuid7>/updates.jsonl`.
- Source: https://docs.x.ai/build/features/hooks ; https://docs.x.ai/build/features/sessions

### Crush
- `crush.json` PreToolUse only; deny `{"decision":"deny"}`, exit 2 blocks, exit 49 halts the turn. No stop event. Ledger SQLite `crush.db`.
- Source: https://github.com/charmbracelet/crush/blob/main/docs/hooks/README.md

### Continue CLI, DeepSeek Harness
- Continue: hooks shipped (PR #11029) but undocumented (issue #11678); PreToolUse/Stop exist; exit 2 = block. DeepSeek Harness: TypeScript plugin waterfalls (`tools/*`, `agent/turn-stopping`), contract undocumented.
- Sources: https://docs.continue.dev/cli/tool-permissions ; https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md

## Adapter checklist (per host)

1. Input normalisation to the gate's fields (`tool_name`, `tool_input`, `cwd`, `transcript_path`, `session_id`, `stop_hook_active`).
2. Output dialect for deny and block.
3. Ledger parser: how skill invocations, edits and prior Stop blocks appear in that host's transcript. Discovered live, then pinned by a fixture test.
4. Registration in `apply.py` for that host's config file, idempotent.
5. Headless live proof; only then does the matrix row say `proven`.
