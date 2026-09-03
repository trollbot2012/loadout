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

import gate  # same directory; owns Facts, write_shaped, COMMAND_RE and STOP_REASON

_CODEX_NAME = re.compile(r"^rollout-")


SKILL_READ_RE = re.compile(r"[\\/]skills[\\/]([^\\/]+)[\\/]SKILL\.md$", re.I)


def _skill_reads(parsed_cmd):
    """Skill names whose SKILL.md a parsed shell command reads."""
    names = set()
    for pc in parsed_cmd or []:
        if isinstance(pc, dict) and pc.get("type") == "read":
            m = SKILL_READ_RE.search(str(pc.get("path") or ""))
            if m:
                names.add(m.group(1))
    return names


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
                # agent reading <skills root>/<name>/SKILL.md, which is the strongest invocation signal
                read = _skill_reads(it.get("parsed_cmd"))
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
