#!/usr/bin/env python3
"""loadout gate, DeepSeek Harness ledger — facts from the plugin's live event stream.

dsh persists its session as `session.jsonl.zstd` (multi-frame Zstandard), which stdlib Python
cannot decode, so the in-process plugin (scripts/gate_dsh.mjs) reports what it observed on the
hook payload under "events" and this module turns that into gate.Facts. Policy itself — binding
stages, the enforcement surface, the bootstrap boundary, write-shaped detection — stays in gate.py.

Event shapes the plugin emits (anything else is ignored):
  {"t": "skill", "name": "<skill>"}          a successful `skill` tool call, or a /name injection
  {"t": "tool",  "name": "write", "file": …} an edit tool
  {"t": "tool",  "name": "pwsh",  "cmd":  …} a shell command
  {"t": "block"}                             a Stop block this adapter itself steered
"""
import gate  # same directory; owns Facts and write_shaped

# dsh tool names (verified from the installed tree's tool packages, 0.1.1-rc.2)
EDIT_TOOLS = {"write", "edit", "str_replace_editor"}
SHELL_TOOLS = {"pwsh", "bash"}
# read, glob, grep, todo_write and skill are never gated: gating `skill` would make stage 1
# unreachable, and the gate has no reason to stand between the agent and a read.


def normalise(tool, inp):
    """Map a dsh tool call onto the shapes gate.py's policy already understands."""
    if tool in EDIT_TOOLS:
        return "Write", inp
    if tool in SHELL_TOOLS:
        return "Bash", inp
    return tool, inp


def facts_from_events(events, cwd=None):
    """gate.Facts from the plugin's event list. Malformed entries are skipped, never raised."""
    invoked, edited, blocks = set(), False, 0
    for e in events or []:
        if not isinstance(e, dict):
            continue
        kind = e.get("t")
        if kind == "skill":
            name = e.get("name")
            if name:
                invoked.add(str(name))
                blocks = 0  # a real skill load is progress, so the block run restarts
        elif kind == "block":
            blocks += 1
        elif kind == "tool":
            name = e.get("name")
            if name in EDIT_TOOLS:
                edited = True
            elif name in SHELL_TOOLS and gate.write_shaped(str(e.get("cmd") or "")):
                edited = True
    return gate.Facts(invoked, edited, cwd, blocks)
