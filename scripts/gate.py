#!/usr/bin/env python3
"""loadout gate — make the accepted loadout binding via Claude Code hooks.

Registered by apply.py into <project>/.claude/settings.local.json:
  python gate.py pre    # PreToolUse for Edit|Write|MultiEdit|NotebookEdit|Bash
  python gate.py stop   # Stop
Reads the hook JSON on stdin. Exit 0 with no output = allow. Never raises and never
exits non-zero: enforcement must not break the harness, so any parse problem allows.
Ledger = the session transcript every hook receives as transcript_path; binding set =
LOADOUT.md Accepted lines whose stage label does not start with "situational".
Operator hatch: LOADOUT_ENFORCE=0 (or remove LOADOUT.md). There is no agent-side override.
"""
import json
import os
import re
import shlex
import sys
from pathlib import Path

import apply  # same directory; owns the Accepted-line grammar

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
COMMAND_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
HATCH = "Operator hatch: LOADOUT_ENFORCE=0."

# a redirect that is not a 2>&1-style fd dup and not to /dev/null or NUL
_REDIRECT = re.compile(r"(?<![<>0-9&])>{1,2}(?!&)(?!\s*(?:/dev/null|NUL\b))")
# a write-shaped word in command position (line start or after a shell operator)
_WRITE_WORDS = re.compile(
    r"(?:^|[|;&])\s*(?:tee|sed\s+-i|perl\s+-i|mv|cp|install|patch|git\s+apply|git\s+checkout\s+--|git\s+restore|touch)(?=\s|$)")


def write_shaped(cmd):
    """True when a shell command looks like it writes a file. False positives are accepted."""
    return bool("<<" in cmd or _REDIRECT.search(cmd) or _WRITE_WORDS.search(cmd))


def transcript_facts(path):
    """(invoked skill names, edited?) from a JSONL transcript. Bad lines are skipped."""
    invoked, edited = set(), False
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return invoked, edited
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            invoked.update(COMMAND_RE.findall(content))
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                invoked.update(COMMAND_RE.findall(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                name, inp = block.get("name"), block.get("input") or {}
                if name == "Skill" and inp.get("skill"):
                    invoked.add(str(inp["skill"]))
                elif name in EDIT_TOOLS:
                    edited = True
                elif name == "Bash" and write_shaped(str(inp.get("command") or "")):
                    edited = True
    return invoked, edited
