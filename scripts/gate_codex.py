#!/usr/bin/env python3
"""loadout gate, Codex CLI ledger — the rollout JSONL counterpart of gate.transcript_facts.

Codex appends ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl; every line is
{"timestamp", "type", "payload"}. Same tolerance contract as gate.py: bad lines are skipped,
a missing file is an empty Facts, nothing raises.
"""
import glob
import json
import os
import re
from pathlib import Path

import gate  # same directory; owns Facts, write_shaped and STOP_REASON

_CODEX_NAME = re.compile(r"^rollout-")
_SUCCESS_STATUS = "completed"  # the only outcome the recorded rollout uses for a command that worked


SKILL_READ_RE = re.compile(r"[\\/]skills[\\/]([^\\/]+)[\\/]SKILL\.md$", re.I)
# a plugin-managed skill lives at <...>/plugins/cache/<market>/<plugin>/<version>/skills/<name>/SKILL.md.
# LOADOUT.md names that skill qualified (`superpowers:systematic-debugging`), so the leaf alone can
# never satisfy the stage and the Stop block repeats for a session that really did load it.
PLUGIN_SKILL_READ_RE = re.compile(
    r"[\\/]plugins[\\/]cache[\\/][^\\/]+[\\/]([^\\/]+)[\\/][^\\/]+[\\/]skills[\\/]([^\\/]+)[\\/]SKILL\.md$", re.I)


def skill_reads(parsed_cmd):
    """Skill names whose SKILL.md a parsed shell command reads: the qualified `plugin:name` when
    the path carries a plugin, and the leaf either way. Its only consumer is ledger credit below
    -- gate.py used to reuse it to decide whether a pre-tool read was the pending stage's own
    prerequisite, but that admission was withdrawn in CX3 and there is no second side to agree
    with any more.
    Ceiling: the leaf is kept because a standalone root (`.agents/skills/<name>/SKILL.md`) has no
    qualified form, so an accepted line naming the bare leaf must still be satisfiable. That makes
    bare-leaf keys collision-prone -- any plugin's copy of the leaf satisfies them.
    Ceiling: `plugin:name` is a namespace key, not a physical origin. It discriminates the plugin
    segment and the leaf; the market segment, the version segment and the cache root above them are
    all outside the key, so a same-named plugin from another market or version -- or any tree of
    that shape, including one inside the project -- satisfies the same stage. Prefer qualified keys
    over bare leaves, but do not read them as proof of where the file came from."""
    names = set()
    for pc in parsed_cmd or []:
        if isinstance(pc, dict) and pc.get("type") == "read":
            path = str(pc.get("path") or "")
            m = PLUGIN_SKILL_READ_RE.search(path)
            if m:
                names.add(f"{m.group(1)}:{m.group(2)}")
            m = SKILL_READ_RE.search(path)
            if m:
                names.add(m.group(1))
    return names


def successful(item):
    """True only when a completion record positively says the command succeeded.

    The recorded rollout (tests/fixtures/codex-rollout.jsonl) is the whole supported outcome
    vocabulary: status="completed" with exit_code=0 for a command that worked, status="failed"
    with exit_code=1 for one that did not. Absent, null, unknown or wrong-typed status does not
    *prove* success, and neither does present exit evidence that contradicts it or cannot be read,
    so all of those are treated as unproven. Withholding is the safe direction here: a missed
    credit repeats a Stop block the agent can clear by loading the skill again, a false credit
    releases the gate on a load that never happened."""
    if item.get("status") != _SUCCESS_STATUS:
        return False
    if "exit_code" not in item:  # omitted entirely: no contradicting evidence to weigh
        return True
    code = item["exit_code"]
    return isinstance(code, int) and not isinstance(code, bool) and code == 0


def find_rollout(session_id, home=None):
    """The rollout JSONL for a Codex session id under <CODEX_HOME or ~/.codex>/sessions, or None.
    Codex's Stop hook payload carries session_id but no transcript_path."""
    if not session_id:
        return None
    root = Path(home or os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "sessions"
    try:
        hits = sorted(root.glob(f"*/*/*/rollout-*-{glob.escape(str(session_id))}.jsonl"))
    except OSError:
        return None
    return str(hits[-1]) if hits else None


def transcript_facts(path):
    """gate.Facts(invoked, edited, cwd, blocks) from a Codex rollout. Bad lines are skipped."""
    invoked, edited, cwd, events = set(), False, None, []  # events: "skill" | "block", in order
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return gate.Facts(invoked, edited, cwd, 0)
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("payload"), dict):
            continue
        kind, pl = d.get("type"), d["payload"]
        if kind == "session_meta":
            if cwd is None and pl.get("cwd"):
                cwd = str(pl["cwd"])
        elif kind == "event_msg" and pl.get("type") == "item_completed":
            it = pl.get("item")
            if not isinstance(it, dict):
                continue
            t = it.get("type")
            if t == "FileChange":
                edited = True
            elif t == "CommandExecution":
                cmd = it.get("command")
                parts = [str(c) for c in cmd] if isinstance(cmd, list) else [str(cmd or "")]
                # the list is shell + "-Command" + the real command; a write word is only recognised
                # in command position, so test the wrapped command on its own as well as the whole
                edited = edited or any(gate.write_shaped(c) for c in [" ".join(parts)] + parts)
                # Codex has no skill event (recorded live 2026-09-02): loading a skill shows up as the
                # agent reading <skills root>/<name>/SKILL.md, which is the strongest invocation signal.
                # Only a load the record says actually succeeded counts, and the credit and the progress
                # event move together: a read that did not demonstrably happen is not progress either.
                # `edited` above stays outcome-blind on purpose -- a failed write still requires the
                # stages, and each asymmetry errs towards keeping the gate closed.
                read = skill_reads(it.get("parsed_cmd")) if successful(it) else set()
                if read:
                    invoked |= read
                    events.append("skill")
            # UserMessage text is deliberately not an invocation signal: a `$name` mention is the user's
            # intent, not the agent loading the skill. Only the SKILL.md read above counts.
            elif t == "HookPrompt":
                # recorded live: an injected Stop-block reason lands here (fragments[].text). Codex has no
                # block cap of its own, so this count is the only runaway guard on this host
                text = "\n".join(str(f.get("text") or "") for f in it.get("fragments") or [] if isinstance(f, dict))
                if gate.STOP_REASON in text:
                    events.append("block")
        elif kind == "response_item" and pl.get("type") == "custom_tool_call" and pl.get("name") == "exec":
            # fallback when item_completed events are absent: the JS snippet names the tool
            src = str(pl.get("input") or "")
            if "apply_patch" in src or ("exec_command" in src and gate.write_shaped(src)):
                edited = True
    blocks = 0
    for ev in events:  # a skill invocation is progress and resets the run of blocks
        blocks = 0 if ev == "skill" else blocks + 1
    return gate.Facts(invoked, edited, cwd, blocks)


def is_codex_transcript(path):
    """True for a Codex rollout: named rollout-*, or whose first non-empty line is a session_meta with a cwd."""
    try:
        p = Path(path)
        if _CODEX_NAME.match(p.name):
            return True
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                return (isinstance(d, dict) and d.get("type") == "session_meta"
                        and isinstance(d.get("payload"), dict) and "cwd" in d["payload"])
    except (OSError, TypeError, ValueError):
        pass
    return False
