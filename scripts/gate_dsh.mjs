/**
 * loadout gate — DeepSeek Harness adapter.
 *
 * Registered as a Cordis loader entry (an `insert` in a profile's `cordis.patch.yml`, or in
 * `$DSH_HOME/cordis.patch.yml`), naming this file by `file://` URL or a path relative to the
 * profile directory. It needs no package.json, no build step and no harness imports, so it can be
 * loaded straight out of the installed skill directory.
 *
 * What it does:
 *   - `tools/pre-execute`  denies an edit or a write-shaped shell command before the stage-1 skill
 *                          has been loaded, by returning {kind:'deny', reason}.
 *   - `agent/turn-stopping` forces the turn to continue while a binding stage is still missing, by
 *                          calling agent.steer(). dsh has no typed veto, so this is the only lever.
 *   - `tools/result` and `agent/pre-step` keep the ledger: which skills were really loaded, whether
 *                          anything was edited, and how many times we have blocked.
 *
 * Policy lives in Python (scripts/gate.py + gate_dsh.py), not here: this file collects facts,
 * spawns the gate and applies its verdict. The one thing duplicated is the set of tool names worth
 * asking about, so that a read never pays for a subprocess.
 *
 * FAIL CLOSED. Unlike Claude Code and Codex, dsh does not enforce the block itself — this plugin
 * does. So every failure path here (spawn failure, timeout, bad JSON, a throw anywhere in the
 * handler) still denies the tool call and still steers at turn-stopping. A broken adapter must not
 * become a silent bypass. The only way out is the operator's: LOADOUT_ENFORCE=0, or removing
 * LOADOUT.md.
 */
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

export const name = 'loadout-gate';

/** dsh tool names worth asking the gate about; everything else (read, glob, grep, skill, …) runs free. */
const GATED = new Set(['write', 'edit', 'str_replace_editor', 'pwsh', 'bash']);
const GATE_TIMEOUT_MS = 20000;

export function apply(ctx, config = {}) {
  const python = config.python ?? process.env.LOADOUT_PYTHON ?? 'python';
  const gate = config.gate ?? new URL('./gate.py', import.meta.url).pathname.replace(/^\//, '');
  const events = [];
  const note = (m) => { try { process.stderr.write(`loadout-gate: ${m}\n`); } catch { /* never throw from a note */ } };

  /** Run the Python gate. Returns its decision, null to allow, and throws on any failure. */
  const callGate = (mode, payload) => {
    const r = spawnSync(python, [gate, mode, '--host', 'dsh'], {
      input: JSON.stringify({ ...payload, events }),
      encoding: 'utf8', timeout: GATE_TIMEOUT_MS, windowsHide: true, env: process.env,
    });
    if (r.error) throw r.error;
    if (r.status !== 0) throw new Error(`gate exited ${r.status}: ${(r.stderr || '').slice(0, 300)}`);
    const out = (r.stdout || '').trim();
    return out ? JSON.parse(out) : null;
  };

  const cwd = () => { try { return process.cwd(); } catch { return undefined; } };

  ctx.on('tools/pre-execute', (exec, next) => {
    if (!GATED.has(exec.name)) return next();
    let decision;
    try {
      decision = callGate('pre', { cwd: cwd(), tool_name: exec.name, tool_input: exec.arguments ?? {} });
    } catch (err) {
      note(`pre-execute failed, denying (fail closed): ${err.message}`);
      return {
        kind: 'deny',
        reason: 'Loadout gate: the enforcement gate could not run, so this edit is denied rather than '
          + `allowed (fail closed). Operator hatch: LOADOUT_ENFORCE=0. Detail: ${err.message}`,
      };
    }
    const denial = decision?.hookSpecificOutput?.permissionDecisionReason;
    return denial ? { kind: 'deny', reason: denial } : next();
  });

  // The ledger only counts what actually happened: a skill call that errored is not a load.
  ctx.on('tools/result', (exec, result) => {
    try {
      if (result?.isError) return;
      const args = exec.arguments ?? {};
      if (exec.name === 'skill' && args.name) events.push({ t: 'skill', name: String(args.name) });
      else if (exec.name === 'pwsh' || exec.name === 'bash') events.push({ t: 'tool', name: exec.name, cmd: String(args.command ?? '') });
      else if (GATED.has(exec.name)) events.push({ t: 'tool', name: exec.name, file: String(args.file_path ?? '') });
    } catch (err) { note(`ledger update failed: ${err.message}`); }
  });

  // A user-typed /name loads the skill's instructions without a tool call; that is a real load too.
  ctx.on('agent/pre-step', (payload, next) => {
    try {
      for (const m of payload?.messages ?? []) {
        if (m?.source?.kind === 'skill-invocation' && m.source.name) events.push({ t: 'skill', name: String(m.source.name) });
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
