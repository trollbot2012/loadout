# DeepSeek Harness (dsh) adapter for the enforcement gate

Date: 2026-09-03 | Status: proven live 2026-09-03 | Target: loadout v1.6.0 | Matrix row: docs/host-capability-matrix.md

## Goal

The same enforcement contract Claude Code and Codex have, on dsh: no file mutation or
write-shaped shell command before the stage-1 skill has been loaded, no ending the turn while a
binding stage is still missing, operator-only disablement, exact-bootstrap boundary. dsh has no
typed veto on turn-stopping, so this adapter is the first one where the plugin itself — not the
host — has to be the thing that fails closed.

## Verified facts this design rests on

- `tools/pre-execute` runs as a waterfall; a listener denies by returning `{kind:'deny',reason}`
  without calling `next()`, and the dispatcher turns that into a model-visible `Error: <reason>`
  result. (`docs/host-capability-matrix.md:96-100`, source `dsh-tool-cordis/lib/index.js:4305-4307`,
  `dsh-tools/lib/types/index.js:870-889`)
- `agent/turn-stopping` is serial and returns void — a listener cannot veto the stop. The only
  lever is `agent.steer(message)`, which re-queues the message so the agent loop re-reads its
  inbox instead of closing the turn; there is no host-side cap on consecutive blocks anywhere in
  the shipped tree. (`docs/host-capability-matrix.md:103-108`, source
  `dsh-tool-cordis/lib/index.js:3868-3872`, `dsh-agent-loop/lib/index.js:564-571`)
- The native `skill` tool takes `{name}`, resolves through the skills registry (not the `read`
  tool), and is logged as an ordinary `tool/call` + `tool/result` pair; a user-typed `/name`
  instead injects the instructions at `agent/pre-step` as a `UserMessage` with
  `source:{kind:"skill-invocation",name,...}` and carries no `tool/call`. Both are real loads; a
  bare mention in text is neither. (`docs/host-capability-matrix.md:115-122`, source
  `dsh-tool-skill/lib/index.js:37-135`, `dsh-skill-filesystem/lib/index.js:155-177`)
- The on-disk session log (`~/.dsh/sessions/<slugified-cwd>/<session-uuid>/session.jsonl.zstd`) is
  JSONL inside multi-frame Zstandard, one frame per flush; a single-shot decompress returns only
  the first frame, and Python 3.9-3.13 ship no stdlib zstd module, so this project cannot parse it.
  (`docs/host-capability-matrix.md:123-129`, source
  `dsh-session-persistence-jsonl/lib/index.js:26-29,450-484`)
- Plugin registration is a Cordis loader entry composed from profile `package.json` bundles, each
  bundle's `cordis.patch.yml`, the profile's own `cordis.patch.yml`, `$DSH_HOME/cordis.patch.yml`,
  then `--patch` overlays; `dsh plugin add` is a thin pnpm forwarder and pnpm is absent on this
  machine. There is no project-level plugin config and no trust gate on plugin load; patch YAML
  permits `!!js` expressions. (`docs/host-capability-matrix.md:131-139`, source
  `dsh/lib/profile-boot-*.js:150-177`, `dsh-app-boot/lib/index.js:291-311`, `dsh/lib/bin.js:96-101`)
- `tools/pre-execute` has no timeout of its own; a plugin that spawns a subprocess and never
  resolves stalls the waterfall indefinitely, so the adapter must impose and enforce its own
  timeout rather than rely on the host. (source `dsh-tool-cordis/lib/index.js:4305-4307`, no
  timeout parameter on the waterfall call)

## The difference that matters

On Claude Code and Codex the host itself enforces the block: a hook that returns nothing, throws,
times out, or is never invoked leaves the turn running under the host's own default, and the gate
process still exits 0/allow only by explicit choice inside `decide()`. A broken gate on those hosts
degrades gracefully because the enforcement point is outside the gate.

On dsh there is no such outer point. `agent/turn-stopping` cannot veto; the only way a turn is kept
open is the plugin itself calling `agent.steer()` inside its own listener. If the plugin throws
before calling `steer()`, times out waiting on the Python subprocess, or is skipped by a loader
misconfiguration, the turn ends silently with nothing left to stop it. Equivalence to Claude Code
and Codex is therefore not a property of the mechanism reading alone — it has to be demonstrated
under an induced adapter failure. No equivalence claim is permitted until a test that deliberately
breaks the adapter (see run C below) shows the Stop path still blocks. Until that run has a result,
this design gets `runtime proof blocked` / `in progress` on the matrix, not `proven`.

## Components

- `scripts/gate_dsh.mjs` — the Cordis plugin (`export const name = 'loadout-gate'`,
  `export function apply(ctx, config)`). Subscribes to four events:
  - `tools/pre-execute`: for a tool in `GATED = {write, edit, str_replace_editor, pwsh, bash}`,
    spawns `python gate.py pre --host dsh` synchronously (`spawnSync`, `GATE_TIMEOUT_MS = 20000`)
    with the accumulated event ledger on stdin, and returns `{kind:'deny', reason}` when the gate's
    `hookSpecificOutput.permissionDecisionReason` is set, else calls `next()`. Anything ungated
    (read, glob, grep, skill, …) runs free without paying for a subprocess.
  - `tools/result`: appends to the in-memory ledger only for a call that did not error — a skill
    call becomes `{t:'skill',name}`, a shell call becomes `{t:'tool',name,cmd}`, a gated edit
    becomes `{t:'tool',name,file}`.
  - `agent/pre-step`: scans `payload.messages` for a `source.kind === 'skill-invocation'` entry (a
    user-typed `/name`) and records it as `{t:'skill',name}` too, since it loads the skill without
    a tool call.
  - `agent/turn-stopping`: spawns `python gate.py stop --host dsh`; on a `{decision:'block'}`
    result, pushes `{t:'block'}` onto the ledger and calls `payload.agent.steer()` with the reason
    as a frozen user message tagged `source:{kind:'plugin',plugin:name,form:'snapshot'}`.
  - Every failure path denies or steers: a `pre-execute` spawn error, non-zero exit, or bad JSON is
    caught and turned into a deny reason naming the operator hatch; a `turn-stopping` failure is
    caught and turned into a steer with the same fail-closed reason. The one place that cannot be
    made to fail closed is documented in code: if `agent.steer()` itself throws, nothing else can
    hold the turn open, so the handler only writes a loud stderr note and lets the turn end.
- `scripts/gate_dsh.py` — pure translation, no policy. `facts_from_events(events, cwd=None)` walks
  the plugin's event list (`{"t":"skill"|"tool"|"block", ...}`) and returns a `gate.Facts`: a
  `skill` event adds to `invoked` and resets the block run; a `block` event increments `blocks`; a
  `tool` event marks `edited` when the tool is in `EDIT_TOOLS` or is a `SHELL_TOOLS` command that
  `gate.write_shaped()` recognizes. `normalise(tool, inp)` maps dsh's `write`/`edit`/
  `str_replace_editor` onto `"Write"` and `pwsh`/`bash` onto `"Bash"` so the rest of the pipeline
  is unchanged. Malformed or unrecognized entries are skipped, never raised.
- `scripts/gate.py`, `--host dsh` path — in `decide()`, when `host == "dsh"` the function skips
  `ledger_facts()` entirely (there is no readable transcript) and instead calls
  `dsh_facts(hook)` → `gate_dsh.facts_from_events(hook.get("events"), hook.get("cwd"))`, then
  `tool, inp = gate_dsh.normalise(tool, inp)` before falling into the same edit/Bash decision logic
  every other host shares. The Stop-block cap (`STOP_BLOCK_CAP = 8`) still counts `facts.blocks`
  for dsh, but the release only fires for `resolve_host(...) == "claude-code"`: Claude Code
  overrides its own Stop hook after 8 consecutive blocks, so yielding there mirrors a limit that
  exists anyway. Every other host, dsh included, has no such host cap, so the gate keeps blocking
  and only logs a runaway note to stderr — an in-gate cap on dsh would be a bypass the
  external-only disablement contract forbids.
- Enforcement surface additions for dsh, in `gate.py`: `DSH_SURFACE = {"settings.yaml",
  "package.json"}` (operator-owned only when a `.dsh` directory is among the path's parents,
  checked in `is_surface()`); `SURFACE_FILES` also carries `"cordis.patch.yml"`, `"cordis.yml"` and
  `"gate_dsh.mjs"` unconditionally (the dsh loader config and the plugin file itself, wherever they
  sit); the `_SENSITIVE` regex used for shell-command gating adds the same
  `cordis\.patch\.yml|cordis\.yml|gate_dsh\.mjs` names plus `\.dsh[\\/](?:settings\.yaml|profiles)`.
  There is no `register_dsh_gate()` in `scripts/apply.py` and no `host == "dsh"` branch in
  `apply()` — registration into a real `cordis.patch.yml` is not automated yet; the live-proof rig
  below builds its `--patch` overlay by hand instead.

## Live proof (required)

Rig: a stock `headless` dsh profile, driven through a `--patch` overlay so the operator's own
`~/.dsh` is never modified. The overlay does two things: inserts `gate_dsh.mjs` by `file://` URL
(no `package.json`, no pnpm, matching the registration path already proven live on 2026-09-03 per
the matrix), and redirects the settings file with a `- id: settings` entry whose `config.path`
points at a throwaway copy rather than the profile's real `settings.yaml`. Model backend: local
Ollama at `http://127.0.0.1:11434/v1`, model `qwen3:8b` pinned to CPU (this machine's CUDA backend
fails PTX JIT compilation), reached with an `apiKeyEnv` naming a throwaway environment variable —
this replaces the three unreachable providers that blocked the 2026-09-03 proof attempt in the
matrix.

Three runs, headless, transcript/ledger and file state inspected afterwards:

- **(A) no skill loaded** — a write is attempted with no stage-1 skill invoked. Expected: the
  write is denied by `tools/pre-execute`, and the turn does not end (or if it tries to stop,
  `agent/turn-stopping` steers it back). Result: TBD (in progress).
- **(B) both binding skills loaded** — the stage-1 skill (and whichever second binding stage the
  scenario exercises) are invoked via the native `skill` tool before the write. Expected: the write
  is allowed, and the turn is permitted to end cleanly. Result: TBD (in progress).
- **(C) induced adapter failure** — the gate's Python interpreter path is deliberately pointed at a
  non-existent binary (`config.python` in the plugin, or `LOADOUT_PYTHON`), so every `spawnSync`
  call in `gate_dsh.mjs` fails. Expected: the write is still denied (the `pre-execute` catch path)
  and the turn is still blocked from ending (the `turn-stopping` catch path calls `steer()` with
  the fail-closed reason). This is the run "The difference that matters" requires before any
  equivalence claim is made. Result: TBD (in progress).

## Out of scope

Every other host (Qwen Code, Gemini CLI, Copilot CLI, zcode, Hermes, Cursor, Mistral Vibe,
OpenCode, Grok Build, Crush, Continue CLI).

## Live proof results (2026-09-03)

The rig is the one described above: stock `headless` profile, a `--patch` overlay that inserts the
plugin by `file://` URL and redirects the settings file to a copy, so the operator's `~/.dsh` is
untouched. The model is a deterministic local OpenAI-compatible provider; the only reachable real
model (Ollama `qwen3:8b`, CPU-pinned because this box's CUDA backend fails PTX JIT) would not emit
tool calls inside dsh's agentic prompt. Everything downstream of the model is real dsh.

| Run | Result |
|---|---|
| No skill loaded | The write never happened; the model got `Error: Loadout gate: invoke \`planning-with-files\` (planning) before editing.` |
| Skill loaded, review missing | The write succeeded (harness reported `Updated file`); the turn was then held open for 3701 model turns until the external timeout |
| Gate binary missing | `pre-execute failed, denying (fail closed)` and `turn-stopping failed, blocking anyway (fail closed)`; 9962 fail-closed decisions, held open to the timeout |
| Fault raised inside `decide` | The model got `Error: Loadout gate: the gate hit an internal error, so this is denied rather than allowed (fail closed).`; turn held open |

Both induced-error classes therefore fail closed, which is what the equivalence claim was gated on.

Two things this does not cover. A scripted model proves the adapter, not a particular model's
willingness to comply — the gate never depended on that. And these paths cover a gate that breaks
while loaded: if the plugin never loads at all, nothing enforces, which is true of every host whose
hook is not registered, and easier to hit on dsh because there is no trust gate and no per-repo
config to notice.

## Debugging notes worth keeping

- `pkill` does not reach native Windows processes. Eighteen stale stub providers held port 18099 and
  served the wrong script, which invalidated two full proof runs before it was spotted; kill by PID
  from `netstat -ano` plus `taskkill`, and check the port is free before trusting a run.
- Denials reach the model as tool results, not on stderr. Grepping the harness's stdout/stderr for
  the deny text finds nothing and reads as a pass; the provider's view of the conversation is the
  place to assert.
- `JSON.stringify` escapes the quotes in the harness's `<skill_content name="…">` marker, so the
  plugin's regex has to accept `name=\"…\"` as well as `name="…"`.
