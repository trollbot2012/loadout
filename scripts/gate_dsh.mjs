/**
 * loadout gate — DeepSeek Harness adapter.
 *
 * Registered as a Cordis loader entry (an `insert` in `$DSH_HOME/cordis.patch.yml` or a profile's
 * own patch layer), naming this file by `file://` URL. It needs no package.json, no build step and
 * no harness imports, so it loads straight out of the installed skill directory.
 *
 *   - `tools/pre-execute`   denies a mutation before the stage-1 skill has been loaded.
 *   - `agent/turn-stopping` forces the turn to continue while a binding stage is missing, by
 *                           calling agent.steer(); dsh has no typed veto, so this is the only lever.
 *   - `tools/result` / `agent/pre-step` keep the ledger of what was really loaded and changed.
 *
 * Policy lives in Python (scripts/gate.py + gate_dsh.py). This file collects facts, spawns the
 * gate and applies its verdict.
 *
 * FAIL CLOSED. Unlike Claude Code and Codex, dsh does not enforce the block itself — this plugin
 * does. Every failure path here (spawn failure, timeout, bad JSON, an exception anywhere) still
 * denies the tool call and still steers at turn-stopping, and the Python gate mirrors that: with
 * `--host dsh` an internal fault emits a deny/block rather than staying silent. The only way out
 * is the operator's: LOADOUT_ENFORCE=0, or removing LOADOUT.md before the session starts.
 */
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import { dirname, join, parse } from 'node:path';

export const name = 'loadout-gate';

/**
 * Tools that cannot mutate anything, so they never pay for a subprocess. Everything else is gated,
 * including MCP tools and the delegation tools: an allowlist of "dangerous" names would miss every
 * tool added after this file was written, and the gate must not be the thing that is out of date.
 */
const SAFE = new Set(['read', 'glob', 'grep', 'todo_write', 'skill']);
const GATE_TIMEOUT_MS = 20000;
/** dsh renders a loaded skill as <skill_content name="…">; that is the authoritative load record. */
const SKILL_LOADED = /<skill_content\s+name="([^"]+)"/;

/** One ledger per process, so a second load of this plugin cannot start a fresh, empty one. */
const LEDGER = (globalThis.__loadoutGateLedger ??= { events: [] });

export function apply(ctx, config = {}) {
  const python = config.python ?? process.env.LOADOUT_PYTHON ?? 'python';
  const gate = config.gate ?? new URL('./gate.py', import.meta.url).pathname.replace(/^\//, '');
  const events = LEDGER.events;
  const note = (m) => { try { process.stderr.write(`loadout-gate: ${m}\n`); } catch { /* never throw from a note */ } };

  const cwd = () => { try { return process.cwd(); } catch { return undefined; } };

  /** Whether a LOADOUT.md governs this session, decided once at load so it cannot be deleted away. */
  const loadoutExpected = (() => {
    try {
      let dir = cwd();
      const root = dir ? parse(dir).root : undefined;
      while (dir) {
        if (existsSync(join(dir, 'LOADOUT.md'))) return true;
        if (dir === root) return false;
        const up = dirname(dir);
        if (up === dir) return false;
        dir = up;
      }
    } catch { /* fall through */ }
    return false;
  })();

  /** Run the Python gate. Returns its decision, null to allow, and throws on any failure. */
  const callGate = (mode, payload) => {
    const r = spawnSync(python, [gate, mode, '--host', 'dsh'], {
      input: JSON.stringify({ ...payload, events, loadout_expected: loadoutExpected }),
      encoding: 'utf8', timeout: GATE_TIMEOUT_MS, windowsHide: true, env: process.env,
    });
    if (r.error) throw r.error;
    if (r.status !== 0) throw new Error(`gate exited ${r.status}: ${(r.stderr || '').slice(0, 300)}`);
    const out = (r.stdout || '').trim();
    return out ? JSON.parse(out) : null;
  };

  ctx.on('tools/pre-execute', (exec, next) => {
    if (SAFE.has(exec.name)) return next();
    let decision;
    try {
      const args = (exec.arguments && typeof exec.arguments === 'object') ? exec.arguments : {};
      decision = callGate('pre', { cwd: cwd(), tool_name: exec.name, tool_input: args });
    } catch (err) {
      note(`pre-execute failed, denying (fail closed): ${err.message}`);
      return {
        kind: 'deny',
        reason: 'Loadout gate: the enforcement gate could not run, so this is denied rather than '
          + `allowed (fail closed). Operator hatch: LOADOUT_ENFORCE=0. Detail: ${err.message}`,
      };
    }
    const denial = decision?.hookSpecificOutput?.permissionDecisionReason;
    return denial ? { kind: 'deny', reason: denial } : next();
  });

  // The ledger counts what actually happened. A skill counts as loaded only when the result carries
  // the harness's own <skill_content name="…"> marker: a request that failed, or resolved to some
  // other skill, must not satisfy a stage.
  ctx.on('tools/result', (exec, result) => {
    try {
      if (result?.isError) return;
      const args = (exec.arguments && typeof exec.arguments === 'object') ? exec.arguments : {};
      if (exec.name === 'skill') {
        const loaded = SKILL_LOADED.exec(JSON.stringify(result?.content ?? result ?? ''));
        if (loaded) events.push({ t: 'skill', name: loaded[1] });
        else note(`skill result carried no <skill_content name>, not counting it as a load`);
      } else if (exec.name === 'pwsh' || exec.name === 'bash') {
        events.push({ t: 'tool', name: exec.name, cmd: String(args.command ?? '') });
      } else if (!SAFE.has(exec.name)) {
        events.push({ t: 'tool', name: exec.name, file: String(args.file_path ?? args.path ?? '') });
      }
    } catch (err) { note(`ledger update failed: ${err.message}`); }
  });

  // A user-typed /name loads the skill's instructions without a tool call. Only the user's own
  // messages count: an agent-authored message carrying that source would otherwise write its own pass.
  ctx.on('agent/pre-step', (payload, next) => {
    try {
      for (const m of payload?.messages ?? []) {
        if (m?.role === 'user' && m?.source?.kind === 'skill-invocation' && m.source.name) {
          events.push({ t: 'skill', name: String(m.source.name) });
        }
      }
    } catch (err) { note(`pre-step ledger update failed: ${err.message}`); }
    return next();
  });

  ctx.on('agent/turn-stopping', (payload) => {
    let reason = null;
    try {
      const decision = callGate('stop', { cwd: cwd() });
      if (decision?.decision === 'block') reason = decision.reason;
    } catch (err) {
      note(`turn-stopping failed, blocking anyway (fail closed): ${err.message}`);
      reason = 'Loadout gate: the enforcement gate could not run, so this turn is not allowed to end '
        + `(fail closed). Operator hatch: LOADOUT_ENFORCE=0. Detail: ${err.message}`;
    }
    if (!reason) return;
    events.push({ t: 'block' });
    try {
      payload.agent.steer(Object.freeze({
        id: randomUUID(), role: 'user',
        content: [{ type: 'text', text: reason }],
        source: { kind: 'plugin', plugin: name, form: 'snapshot' },
      }));
    } catch (err) {
      // Nothing else can hold the turn open; make the failure loud rather than silent.
      note(`steer failed, the turn will end unenforced: ${err.message}`);
    }
  });
}
