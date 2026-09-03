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
| DeepSeek Harness | hard (`tools/pre-execute` returns `{kind:'deny',reason}`) | hard, but the PLUGIN enforces it (`agent.steer()`); every adapter failure path is fail-closed, proven by induced-error tests | native `skill` tool result marker + the plugin's live event stream | **proven** 2026-09-03 (gate_dsh, v1.6.0) — model scripted, see below |

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
- **Crash investigation, 2026-09-03 — the gate is not the cause.** A parallel session reported that a
  registered gate crashed the Codex 0.152.1 desktop app-server (0xc0000409 about 20 s after launch)
  and made enforcement opt-in behind `--enforce-codex`. The crashes are real: Windows Error
  Reporting shows four `codex.exe` faults on 2026-09-02 (22:56 and 23:00 desktop app-server, 23:40
  twice standalone CLI). Attribution to the gate does not survive:
  - Minidump analysis (stdlib parser; no debugger on the box): `ExceptionInformation[0] = 7` and
    `Rcx = 7`, i.e. `__fastfail(FAST_FAIL_FATAL_APP_EXIT)` — Rust's `abort_internal`, not a stack
    cookie. The faulting thread is `tokio-rt-worker` in all five dumps, and the five shared frames
    are the std panic-to-abort runtime under four *different* callers: four root causes, one abort
    path.
  - No positive trace of the gate: zero hits for `gate.py` or the Python interpreter path in any
    dump. Every `hooks.json` hit is a `hooks.json:<event>:<i>:<j>` trust key from parsing
    `config.toml` at startup, not hook execution. The one `PreToolUse` hit is a skill description.
  - The two CLI crashes burned 0 s user and 0 s kernel CPU in 26 s and 14 s and wrote no session
    rollout, so no turn ran and neither `PreToolUse` nor `Stop` could have fired. Their memory is
    dominated by plugin-marketplace JSON, `plugins/cache` paths and an age/scrypt secrets stanza on
    the faulting stack: startup config, plugin and secret loading.
  - Upstream reports the same desktop fault with identical offsets and an *empty* `CODEX_HOME`,
    no hooks configured (openai/codex #37164, #36096); no upstream issue attributes 0xc0000409 to
    hooks. Codex's hook subsystem has three reachable panic sites, all `unreachable!` invariant
    guards, none reachable from spawning a script.
  - A dump from 14:36 that day predates any gate registration (the 20:38 `hooks.json` backup has no
    `gate.py`), and the reported bisect also varied trust bookkeeping, the presence of any
    `PreToolUse` group at all, and three `hooks.json` rewrites inside the trial window.
  Enforcement stays opt-in as cheap caution, but the justification above is the accurate one. Note
  also openai/codex #38168: on Windows a hook command with embedded quotes can silently never run
  while still reporting `Completed` — a live enforcement-integrity trap, though a recorder probe
  confirmed the `&`-prefixed `commandWindows` form does execute on this machine.
- Scope of the `proven` status: headless `codex exec`. The desktop app-server path was never
  exercised by these proofs and is not claimed.
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

Read from the installed tree, not from docs: `%LOCALAPPDATA%/Programs/DeepSeek Harness/resources/app/node_modules/@deepseek-ai/` (196 unminified ESM packages; the API catalog with signatures is `dsh-tool-cordis/lib/index.js`).

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
  turn end silently. That is why equivalence needed proving rather than arguing, and on 2026-09-03 it
  was: induced-error runs show both failure classes — a missing gate binary and a fault raised inside
  the gate — denying the tool call and holding the turn open. The adapter is written so that every
  path through it, including its own exception handlers, denies and steers.
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
- **Proof (headless `dsh --profile headless`, 2026-09-03).** The rig changes nothing in the operator's
  `~/.dsh`: the stock `headless` profile plus a `--patch` overlay that inserts the plugin by `file://`
  URL and redirects the settings file to a copy (`- id: settings` / `config.path`). The model is a
  deterministic local OpenAI-compatible provider, because the only reachable real model (Ollama
  `qwen3:8b`, CPU-pinned — this box's CUDA backend fails PTX JIT) would not emit tool calls inside
  dsh's agentic prompt. Everything downstream of the model is real: dsh dispatches the tool, the
  plugin sees it, the Python gate decides.
  - **Deny before stage 1**: the write never happened and the model received
    `Error: Loadout gate: invoke \`planning-with-files\` (planning) before editing.`
  - **Allow after a real load, then block**: with the skill loaded the write succeeded
    (`notes.txt` written, harness reported `Updated file`), and the turn was then held open for 3701
    model turns until the external timeout, because the review stage was never loaded.
  - **Fail closed, spawn class** (gate binary missing): `pre-execute failed, denying (fail closed)`
    and `turn-stopping failed, blocking anyway (fail closed)`; 9962 fail-closed decisions, held open
    to the timeout.
  - **Fail closed, internal class** (a fault raised inside `decide`): the model received
    `Error: Loadout gate: the gate hit an internal error, so this is denied rather than allowed
    (fail closed).`, and the turn was held open.
- **What the scripted model does and does not prove.** It proves the adapter: dispatch, denial,
  the ledger, the steer loop and both failure classes, deterministically. It does not prove anything
  about a particular model's willingness to comply — but the gate never depended on that.
- **Residual gap, stated plainly.** These failure paths cover a gate that breaks *while loaded*. If
  the plugin never loads at all (a bad `cordis.patch.yml`, a `dsh` upgrade that moves the loader),
  nothing enforces — the same hole every host has when its hook is not registered. dsh makes it
  easier to hit because there is no trust gate and no per-repo config to notice.


## Adapter checklist (per host)

1. Input normalisation to the gate's fields (`tool_name`, `tool_input`, `cwd`, `transcript_path`, `session_id`, `stop_hook_active`).
2. Output dialect for deny and block.
3. Ledger parser: how skill invocations, edits and prior Stop blocks appear in that host's transcript. Discovered live, then pinned by a fixture test.
4. Registration in `apply.py` for that host's config file, idempotent.
5. Headless live proof; only then does the matrix row say `proven`.
