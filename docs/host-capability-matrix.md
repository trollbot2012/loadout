# Host capability matrix for the enforcement gate

Researched 2026-09-02 from official documentation and source. Every row cites its source; "not
documented" means exactly that. **Proof status is the only column that counts for a claim of
enforcement**: config compatibility (a host reading Claude-format hooks) is not proof.

Proof status values: `proven` (headless live run in that host: an edit denied before stage 1 and
a stop blocked with stages missing, transcript inspected), `unverified` (mechanism documented,
no live run), `runtime proof blocked` (mechanism read from source, but a live run is currently
impossible for an environmental reason named in the row), `not started`, `n/a` (no mechanism).

A host is never called equivalent to Claude Code on the strength of a mechanism reading alone.
Where the host does not itself enforce the block, equivalence additionally requires showing that
an adapter failure fails **closed**, or an explicit decision to accept the weaker behaviour.

| Host | Pre-tool deny | Stop block | Ledger source | Proof status |
|---|---|---|---|---|
| Claude Code | hard | hard (host cap 8) | `transcript_path` JSONL | **proven** 2026-09-02 (gate.py v1.4.0) |
| Codex CLI | hard | hard (no host cap; gate never yields, operator ends it) | rollout JSONL via `transcript_path` (pre) / session id (stop) | **proven** 2026-09-02 (gate_codex, v1.5.0) |
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
| DeepSeek Harness | hard (`tools/pre-execute` waterfall returns `{kind:'deny',reason}`) | **not established**: no typed veto; the plugin forces continuation via `agent.steer()`, so an adapter failure fails open | native `skill` tool + `tool/call` rows; on-disk log is multi-frame zstd | **runtime proof blocked** (no reachable model backend on this machine; implementation stopped 2026-09-03) |

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

### Continue CLI
- Hooks shipped (PR #11029) but undocumented (issue #11678); PreToolUse/Stop exist; exit 2 = block.
- Source: https://docs.continue.dev/cli/tool-permissions

### DeepSeek Harness (verified from source 2026-09-03, dsh 0.1.1-rc.2)

Read from the installed tree, not from docs: `%LOCALAPPDATA%\Programs\DeepSeek Harness
esourcespp
ode_modules\@deepseek-ai\` (196 unminified ESM packages; the API catalog with signatures is `dsh-tool-cordis/lib/index.js`).

- **Pre-tool denial — meets the contract.** Event `tools/pre-execute`, waterfall mode
  (`dsh-tool-cordis/lib/index.js:4305-4307`). A listener returns
  `{kind:'deny', reason}` (it does not throw and does not call `next()`); `PreToolDecision` is
  `{kind:'allow'} | {kind:'deny',reason} | {kind:'ask',reason?}` (`:4400`ff). The dispatcher turns a
  denial into a model-visible error result, text `Error: <reason>`
  (`dsh-tools/lib/types/index.js:870-889`). A second, non-vetoable path exists:
  `ctx.tools.guard(fn)`, synchronous, where returning a string denies (`:512-520`).
- **Stop blocking — equivalence NOT established.** `agent/turn-stopping`
  is serial and returns void (`dsh-tool-cordis/lib/index.js:3868-3872`): a listener cannot veto.
  It calls `agent.steer(message)` and the loop re-reads its inbox, so the turn does not close
  (`dsh-agent-loop/lib/index.js:564-571`). The reason reaches the model only as the text of that
  injected message, not as a typed field — the same way Codex renders a block as a `HookPrompt`.
  **No consecutive-block cap exists anywhere in the shipped tree**, which matches the external-only
  disablement policy. But the failure mode differs from Claude Code and Codex in a way that blocks
  any equivalence claim: there the host enforces the block, so a broken gate still fails safe; here
  the plugin forces continuation itself, so a plugin that throws, times out, or is skipped lets the
  turn end silently. Calling this equivalent requires one of two things, neither of which has been
  done: a demonstration that an induced adapter failure still fails closed, or an explicit decision
  to accept the weaker behaviour on this host.
- **Skill invocation — meets the contract, and is the strongest signal of any host.** A native
  model-facing `skill` tool takes `{name}` and resolves through the skills registry rather than the
  `read` tool (`dsh-tool-skill/lib/index.js:37-135`); roots are `<project>/.dsh/skills`,
  `<project>/.agents/skills`, `$DSH_HOME/skills`, `$AGENTS_HOME/skills`
  (`dsh-skill-filesystem/lib/index.js:155-177`), bundle form `<name>/SKILL.md`. Each call is logged
  as an ordinary `tool/call` + `tool/result` pair. A user-typed `/name` instead injects the
  instructions at `agent/pre-step` as a UserMessage with `source:{kind:"skill-invocation",name,...}`
  (`:167-176`) and is **not** a `tool/call`. Both are real loads; a bare mention is neither.
- **Ledger — differs in encoding.** `~/.dsh/sessions/<slugified-cwd>/<session-uuid>/session.jsonl.zstd`,
  JSONL inside **multi-frame** Zstandard, one frame per flush, so a single-shot decompress returns
  only the first frame (`dsh-session-persistence-jsonl/lib/index.js:26-29`, decoder `:450-484`).
  There is also a SQLite query index (`dsh-session-query-sqlite/lib/index.js:49`). Records are
  `{"type","seq","time","data"}` with `type` = the Cordis event name; a write is
  `tool/call` with `name:"write"`, a shell command `name:"pwsh"`, a read `name:"read"`, a skill load
  `name:"skill"`. **Python 3.9–3.13 has no stdlib zstd**, so this project cannot parse that file: the
  adapter takes its facts from the live event stream inside the plugin instead.
- **Registration — differs.** Plugins are Cordis loader entries composed from the profile's
  `package.json` `dsh.profile.bundles`, each bundle's `cordis.patch.yml`, then the profile's own
  `cordis.patch.yml`, then `$DSH_HOME/cordis.patch.yml`, then `--patch` overlays
  (`dsh/lib/profile-boot-*.js:150-177`, `dsh-app-boot/lib/index.js:291-311`). `settings.yaml` holds
  per-plugin settings only, not registration. `dsh plugin add` is a thin **pnpm** forwarder
  (`dsh/lib/bin.js:96-101`) and pnpm is absent on this machine. There is **no project-level plugin
  config** (a repo's `.dsh/` holds skills and AGENTS.md only) and **no trust gate on plugin load**;
  patch YAML permits `!!js` expressions. The enforcement surface on this host therefore has to
  include `cordis.patch.yml`, the profile `package.json` and `settings.yaml`.
- **Language — meets the contract via a wrapper.** A plugin is an in-process ESM module
  (`apply(ctx, config)`); there is no external-process hook runner (`hook/invoked` and `hook/result`
  are reserved session-event names with zero producers). But `tools/pre-execute` is an awaited async
  waterfall, so a plugin can spawn and await the Python gate and return its decision. Note
  `ctx.subprocess` scrubs `DSH_*`/secret environment from children
  (`dsh-subprocess/lib/index.js:26-44, 52-85`).
- **Headless proof path exists but could not be exercised**: `dsh --profile headless "<task>"` answers
  one task and exits. All three configured providers are local endpoints; on 2026-09-03 two
  (`:18090`, `:8080`) were not listening and the third (`:18093`) accepted TCP but never answered
  HTTP — `/v1/models` failed and a minimal completion hung for 91 s. No dsh task can complete, so
  the live proof is blocked on infrastructure, not on the design.

**Proven live on 2026-09-03, before the blocker (registration only):** a plain `.mjs` plugin loads
into the headless profile through a `--patch` overlay naming it by `file://` URL, with no pnpm and
no package.json; its `apply()` ran and `agent/pre-step` fired with real payloads. The settings file
can also be redirected with `- id: settings
  config: {path: <copy>}`, so an adapter never needs to
modify the operator's own `settings.yaml`. Nothing was left registered: the profile's
`cordis.patch.yml` is back to `[]` and the probe module was removed.

**Status: implementation stopped.** Resume when a model backend answers; the remaining work is the
plugin plus its Python side, and the equivalence question above must be settled before the row can
claim more than `runtime proof blocked`.

## Adapter checklist (per host)

1. Input normalisation to the gate's fields (`tool_name`, `tool_input`, `cwd`, `transcript_path`, `session_id`, `stop_hook_active`).
2. Output dialect for deny and block.
3. Ledger parser: how skill invocations, edits and prior Stop blocks appear in that host's transcript. Discovered live, then pinned by a fixture test.
4. Registration in `apply.py` for that host's config file, idempotent.
5. Headless live proof; only then does the matrix row say `proven`.
