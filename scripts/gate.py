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
import traceback
from pathlib import Path

import apply  # same directory; owns the Accepted-line grammar

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
COMMAND_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
HATCH = "Operator hatch: LOADOUT_ENFORCE=0."

# a redirect that is not a 2>&1-style fd dup and not to /dev/null or NUL
_REDIRECT = re.compile(r"(?<![<>&])(?:>{1,2}|&>)(?!&)(?!\s*(?:/dev/null|NUL\b))")
# a write-shaped word in command position (line start or after a shell operator)
_WRITE_WORDS = re.compile(
    r"(?:^|[|;&])\s*(?:tee|sed\s+-i|perl\s+-i|mv|cp|install|patch|git\s+apply|git\s+checkout\s+--|git\s+restore|touch)(?=\s|$)")


# the enforcement surface: gated before stage 1 unless the command is the exact bootstrap (pre mode only)
_SENSITIVE = re.compile(r"apply\.py|gate\.py|settings\.local\.json|LOADOUT\.md|AGENTS\.md|CLAUDE\.md", re.I)


def write_shaped(cmd):
    """True when a shell command looks like it writes a file. False positives are accepted."""
    return bool("<<" in cmd or _REDIRECT.search(cmd) or _WRITE_WORDS.search(cmd))


def sensitive(cmd):
    """True when a shell command names the enforcement surface. Pre-mode only: reading it is not an edit."""
    return bool(_SENSITIVE.search(cmd))


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
        # slash invocations count only from the user: an agent must not be able to write its own pass
        is_user = d.get("type") == "user" or (isinstance(msg, dict) and msg.get("role") == "user")
        if isinstance(content, str):
            if is_user:
                invoked.update(COMMAND_RE.findall(content))
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                if is_user:
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


def find_loadout(cwd):
    """Nearest LOADOUT.md walking up from cwd, or None."""
    try:
        p = Path(cwd or os.getcwd()).resolve()
    except (OSError, TypeError):
        return None
    for d in (p, *p.parents):
        f = d / "LOADOUT.md"
        if f.is_file():
            return f
    return None


def binding_stages(text):
    """Accepted lines minus those whose stage label starts with 'situational'."""
    return [(s, k) for s, k in apply.parse_accepted(text) if not s.lower().startswith("situational")]


_INTERPRETERS = {"python", "python3", "python.exe", "py", "py.exe"}


def _basename(tok):
    # both separators: the hook may see a Windows path on any OS
    return re.split(r"[\\/]", tok)[-1]


def bootstrap_invocation(cmd):
    """True only for an exact, validated `apply.py` run: the one-time bootstrap that may
    re-run on an enforced project. No shell operators, no extra tokens. Everything else that
    touches LOADOUT.md / AGENTS.md / CLAUDE.md stays gated like any other mutation."""
    if re.search(r"[|;&<>`\n]|\$\(", cmd):
        return False
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return False
    if len(toks) < 3:
        return False
    exe, script, rest = toks[0], toks[1], toks[2:]
    if _basename(exe).lower() not in _INTERPRETERS and exe != sys.executable:
        return False
    if _basename(script) != "apply.py":
        return False
    positional, i = 0, 0
    while i < len(rest):
        tok = rest[i]
        if tok in apply.VALUE_FLAGS:
            if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
                return False
            i += 2
        elif tok == "--no-enforce":
            i += 1
        elif tok.startswith("-"):
            return False
        else:
            positional += 1
            i += 1
    return positional == 1


def decide(mode, hook, env=None):
    """The hook decision dict, or None to allow."""
    env = os.environ if env is None else env
    if env.get("LOADOUT_ENFORCE") == "0":
        return None
    loadout = find_loadout(hook.get("cwd"))
    if not loadout:
        return None
    try:
        stages = binding_stages(loadout.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not stages:
        return None
    if mode == "pre":
        tool, inp = hook.get("tool_name"), hook.get("tool_input") or {}
        if tool == "Bash":
            cmd = str(inp.get("command") or "")
            if bootstrap_invocation(cmd) or not (write_shaped(cmd) or sensitive(cmd)):
                return None
        elif tool not in EDIT_TOOLS:
            return None
        stage, skill = stages[0]
        invoked, _ = transcript_facts(hook.get("transcript_path"))
        if skill in invoked:
            return None
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason":
                f"Loadout gate: invoke `{skill}` ({stage}) before editing. Details in LOADOUT.md. {HATCH}"}}
    if mode == "stop":
        if hook.get("stop_hook_active"):
            return None
        invoked, edited = transcript_facts(hook.get("transcript_path"))
        if not edited:
            return None
        missing = [(s, k) for s, k in stages if k not in invoked]
        if not missing:
            return None
        return {"decision": "block",
                "reason": "Loadout gate: stages not run this session: "
                          + ", ".join(f"{s} (`{k}`)" for s, k in missing)
                          + f". Invoke them, then stop. {HATCH}"}
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        out = decide(mode, json.load(sys.stdin))
    except Exception:  # deliberate: a broken gate must allow, never wedge the harness; but say so
        sys.stderr.write("loadout gate: failed open (allowing) because of an internal error:\n")
        traceback.print_exc(file=sys.stderr)
        out = None
    if out:
        sys.stdout.write(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()
