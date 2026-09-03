#!/usr/bin/env python3
"""loadout gate — make the accepted loadout binding via Claude Code hooks.

Registered by apply.py into <project>/.claude/settings.local.json:
  python gate.py pre    # PreToolUse for Edit|Write|MultiEdit|NotebookEdit|Bash|EnterWorktree|mcp__.*
  python gate.py stop   # Stop
Reads the hook JSON on stdin. Exit 0 with no output = allow. Never raises and never
exits non-zero: enforcement must not break the harness, so any parse problem allows
(and says so on stderr).
Ledger = the session transcript every hook receives as transcript_path; binding set =
LOADOUT.md Accepted lines whose stage label does not start with "situational".
The enforcement surface (LOADOUT.md, AGENTS.md, CLAUDE.md, .claude/settings*.json, gate.py,
apply.py) is operator-owned at every stage; only this skill's own apply.py, invoked in its
exact bootstrap form, may touch it. Operator hatch: LOADOUT_ENFORCE=0 (or remove LOADOUT.md).
There is no agent-side override.
"""
import json
import os
import re
import shlex
import sys
import traceback
from collections import namedtuple
from pathlib import Path

import apply  # same directory; owns the Accepted-line grammar and the CLI flag set

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "EnterWorktree", "apply_patch"}  # apply_patch: Codex
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", re.M)  # Move to: rename target
CODEX_SURFACE = {"hooks.json", "config.toml"}  # operator-owned only under a .codex directory
DELEGATION_TOOLS = {"Agent", "Task"}  # a delegated edit is still an edit of this session
MCP_MUTATING_RE = re.compile(
    r"^mcp__.*(?:write|create|edit|delete|remove|exec|run|bash|workbench|upload|update|apply|move|rename|save|patch)",
    re.I)
SURFACE_FILES = {"loadout.md", "agents.md", "claude.md", "settings.json", "settings.local.json", "gate.py", "apply.py"}  # lower-cased
COMMAND_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
HATCH = "Operator hatch: LOADOUT_ENFORCE=0."
STOP_BLOCK_CAP = 8  # own runaway guard; Claude Code applies the same cap on its side

# a redirect (>, >>, 1>, &>, fullwidth ＞) that is not a 2>&1-style fd dup and not to /dev/null or NUL
_REDIRECT = re.compile(r"(?<![<>&])(?:[>＞]{1,2}|&>)(?!&)(?!\s*(?:/dev/null|NUL\b))")
# a write-shaped word in command position: line start, after a shell operator, a subshell or a quote
# (sh -c '...'), allowing sudo/env/VAR=x/path prefixes
_PREFIX = r"(?:(?:sudo|env)\s+|\w+=\S*\s+)*(?:\S*[\\/])?"
_WORDS = (r"tee|sed\s+(?:-\w*i\S*|--in-place\S*)|perl\s+-\S*[ip]\S*|mv|cp|install|patch|dd|wget|touch"
          r"|rm|rmdir|rd|del|erase|unlink|truncate|Remove-Item"  # deleting the surface is a write too
          r"|curl\s+(?:\S+\s+)*-[oO]\b"
          r"|git\s+(?:apply|checkout|restore|commit|stash|cherry-pick|merge|rebase|reset|clean|mv|rm|am|pull)"
          r"|(?:python\S*|node|ruby|perl)\s+-[ce]\b|powershell|pwsh|Set-Content|Out-File|Add-Content")
_WRITE_WORDS = re.compile(r"(?:^|[|;&(\x27\"])\s*" + _PREFIX + r"(?:" + _WORDS + r")(?=\s|$|[\x27\"])", re.I)
# the enforcement surface: naming it in a command is gated before stage 1, and writing it is denied always
_SENSITIVE = re.compile(r"apply\.py|gate\.py|settings\.local\.json|settings\.json|LOADOUT\.md|AGENTS\.md|CLAUDE\.md"
                        r"|(?<![\w.])\.claude(?=[\\/\s'\"]|$)"  # the .claude dir itself (rm -rf .claude)
                        r"|\.codex[\\/](?:hooks\.json|config\.toml)", re.I)  # Codex hook registration + trust


def is_surface(path):
    """Operator-owned enforcement config: the Claude files anywhere, the Codex files under .codex."""
    parts = re.split(r"[\\/]", str(path))
    name = parts[-1].lower()
    return name in SURFACE_FILES or (name in CODEX_SURFACE and ".codex" in (p.lower() for p in parts[:-1]))

# cwd = where the session started (first transcript line); blocks = consecutive Stop blocks since the
# last real skill invocation, read from the transcript itself so nothing persists across sessions
Facts = namedtuple("Facts", "invoked edited cwd blocks")
STOP_FEEDBACK = "Stop hook feedback:"
STOP_REASON = "Loadout gate: stages not run this session"


def write_shaped(cmd):
    """True when a shell command looks like it writes a file. False positives are accepted."""
    return bool("<<" in cmd or _REDIRECT.search(cmd) or _WRITE_WORDS.search(cmd))


def sensitive(cmd):
    """True when a shell command names the enforcement surface."""
    return bool(_SENSITIVE.search(cmd))


def is_edit_tool(tool):
    return tool in EDIT_TOOLS or bool(MCP_MUTATING_RE.match(tool or ""))


def transcript_facts(path):
    """Facts(invoked skills, edited?, first cwd) from a JSONL transcript. Bad lines are skipped."""
    skills, errors, invoked, edited, first_cwd = [], set(), set(), False, None
    events = []  # ("skill", tool_use_id) and ("block",) in transcript order, for the consecutive count
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return Facts(invoked, edited, first_cwd, 0)
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if first_cwd is None and d.get("cwd"):
            first_cwd = str(d["cwd"])
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        # slash invocations count only from the user: an agent must not be able to write its own pass
        is_user = d.get("type") == "user" or (isinstance(msg, dict) and msg.get("role") == "user")
        if isinstance(content, str):
            if is_user:
                invoked.update(COMMAND_RE.findall(content))
                if content.startswith(STOP_FEEDBACK) and STOP_REASON in content:
                    events.append(("block",))
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                if is_user:
                    invoked.update(COMMAND_RE.findall(block.get("text") or ""))
            elif kind == "tool_result":
                if block.get("is_error"):
                    errors.add(block.get("tool_use_id"))
            elif kind == "tool_use":
                name, inp = block.get("name"), block.get("input") or {}
                if name == "Skill" and inp.get("skill"):
                    skills.append((block.get("id"), str(inp["skill"])))
                    events.append(("skill", block.get("id")))
                elif is_edit_tool(name) or name in DELEGATION_TOOLS:
                    edited = True
                elif name == "Bash" and write_shaped(str(inp.get("command") or "")):
                    edited = True
    invoked.update(n for i, n in skills if i not in errors)  # a failed Skill call is not an invocation
    blocks = 0
    for ev in events:  # a successful skill invocation is progress and resets the run of blocks
        blocks = 0 if ev[0] == "skill" and ev[1] not in errors else blocks + (ev[0] == "block")
    return Facts(invoked, edited, first_cwd, blocks)


def find_loadout(*starts):
    """Nearest LOADOUT.md walking up from the first start that has one, or None."""
    for start in starts:
        if not start:
            continue
        try:
            p = Path(start).resolve()
        except (OSError, TypeError):
            continue
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


def bootstrap_invocation(cmd, cwd=None):
    """True only for an exact, validated run of THIS skill's apply.py: the one-time bootstrap that
    may re-run on an enforced project. No shell operators, no extra tokens, no look-alike file."""
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
    p = Path(script)
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    try:
        if os.path.normcase(str(p.resolve())) != os.path.normcase(str(Path(apply.__file__).resolve())):
            return False
    except OSError:
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


def _target_paths(inp):
    """Every file a tool call touches: one for the edit tools, each patched file for Codex's apply_patch
    (whose tool_input.command is the patch text)."""
    cmd = str(inp.get("command") or "")
    if cmd.startswith("*** Begin Patch"):
        return PATCH_FILE_RE.findall(cmd)
    p = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
    return [p] if p else []


def _target_path(inp):
    paths = _target_paths(inp)
    return paths[0] if paths else ""


def _parent_transcript(tp):
    """<session>.jsonl for a subagent transcript at <session>/subagents/agent-*.jsonl, if it exists."""
    try:
        p = Path(tp).parent.parent.with_suffix(".jsonl")
        return p if p.is_file() else None
    except (OSError, TypeError, ValueError):
        return None


def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def resolve_host(tp, host=None):
    """'codex' when told so or when the transcript is a Codex rollout, else 'claude-code'."""
    if host == "codex":
        return "codex"
    if host is None and tp:
        import gate_codex  # same directory; imported lazily because it imports this module
        if gate_codex.is_codex_transcript(tp):
            return "codex"
    return "claude-code"


def ledger_facts(tp, host=None, session_id=None):
    """Facts from the host's own transcript format. Codex's Stop payload carries no transcript_path,
    only session_id, so the rollout is located by that id."""
    if resolve_host(tp, host) == "codex":
        import gate_codex
        return gate_codex.transcript_facts(tp or gate_codex.find_rollout(session_id))
    return transcript_facts(tp)


def decide(mode, hook, env=None, host=None):
    """The hook decision dict, or None to allow."""
    env = os.environ if env is None else env
    if env.get("LOADOUT_ENFORCE") == "0":
        return None
    tp = hook.get("transcript_path")
    facts = ledger_facts(tp, host, hook.get("session_id"))
    tool, inp = hook.get("tool_name"), hook.get("tool_input") or {}
    target = _target_path(inp) if mode == "pre" else ""
    # the hook cwd follows `cd`; the edited path and the session's starting directory do not
    loadout = find_loadout(hook.get("cwd"), Path(target).parent if target else None, facts.cwd)
    if not loadout:
        return None
    try:
        stages = binding_stages(loadout.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not stages:
        return None
    if mode == "pre":
        if tool == "Bash":
            cmd = str(inp.get("command") or "")
            if bootstrap_invocation(cmd, hook.get("cwd")):
                return None
            if sensitive(cmd) and write_shaped(cmd):
                return _deny("Loadout gate: the enforcement config (LOADOUT.md, AGENTS.md, CLAUDE.md, "
                             f".claude/settings*.json, gate.py, apply.py) is operator-owned; only the exact "
                             f"apply.py bootstrap may write it. {HATCH}")
            if not (write_shaped(cmd) or sensitive(cmd)):
                return None
        elif is_edit_tool(tool):
            hit = next((p for p in _target_paths(inp) if is_surface(p)), None)
            if hit:  # case-insensitive filesystems; every file of a multi-file patch is checked
                return _deny(f"Loadout gate: `{_basename(hit)}` is operator-owned enforcement config; "
                             f"the agent may not write it at any stage. Re-audits run under the hatch. {HATCH}")
        else:
            return None
        stage, skill = stages[0]
        invoked = set(facts.invoked)
        if hook.get("agent_id"):  # a subagent is judged against its parent session too
            parent = _parent_transcript(tp)
            if parent:
                invoked |= ledger_facts(parent, host).invoked
        if skill in invoked:
            return None
        return _deny(f"Loadout gate: invoke `{skill}` ({stage}) before editing. Details in LOADOUT.md. {HATCH}")
    if mode == "stop":
        if not facts.edited:
            return None
        missing = [(s, k) for s, k in stages if k not in facts.invoked]
        if not missing:
            return None
        if facts.blocks >= STOP_BLOCK_CAP:
            if resolve_host(tp, host) == "codex":
                # Codex has no host-side release. Disablement is external-only by contract, so the gate
                # keeps blocking; the operator ends the loop (LOADOUT_ENFORCE=0, interrupt, or remove
                # LOADOUT.md). The note makes a runaway visible in hook output.
                sys.stderr.write(f"loadout gate: {facts.blocks} consecutive Stop blocks without progress; "
                                 "still blocking (no cap on Codex; operator hatch LOADOUT_ENFORCE=0).\n")
            else:
                # Claude Code overrides a Stop hook itself after 8 consecutive blocks; yielding here only
                # mirrors that host limit instead of fighting it
                sys.stderr.write(f"loadout gate: {facts.blocks} consecutive Stop blocks without progress; allowing (host cap).\n")
                return None
        return {"decision": "block",
                "reason": "Loadout gate: stages not run this session: "
                          + ", ".join(f"{s} (`{k}`)" for s, k in missing)
                          + f". Invoke them, then stop. {HATCH}"}
    return None


def main():
    argv = sys.argv[1:]
    mode = argv[0] if argv else ""
    host = argv[argv.index("--host") + 1] if "--host" in argv and argv.index("--host") + 1 < len(argv) else None
    try:
        out = decide(mode, json.load(sys.stdin), host=host)
    except Exception:  # deliberate: a broken gate must allow, never wedge the harness; but say so
        sys.stderr.write("loadout gate: failed open (allowing) because of an internal error:\n")
        traceback.print_exc(file=sys.stderr)
        out = None
    if out:
        sys.stdout.write(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()
