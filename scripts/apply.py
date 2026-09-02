#!/usr/bin/env python3
"""loadout apply — persist the accepted loadout into the project's agent instruction files, idempotently.

Usage: python apply.py <project_dir> --host <host> [--loadout LOADOUT.md] [--no-enforce]

Reads the '## Accepted' section of LOADOUT.md (lines like '- <stage>: `<skill>`'), builds the
'## Loadout' block and upserts it (replace if present, else append, else create) into:
  - AGENTS.md (read natively by Codex, Cursor, OpenCode, Copilot, ...)
  - the running host's native file: CLAUDE.md / GEMINI.md / QWEN.md. Claude Code does not read
    AGENTS.md, so a missing CLAUDE.md is created with an '@AGENTS.md' import line.
  - any other native file that already exists in the project (keeps every harness consistent).
  - on claude-code, the enforcement gate (scripts/gate.py) as PreToolUse + Stop hooks in
    .claude/settings.local.json, unless --no-enforce. Hooks load at the next session.
Re-runs replace the existing section; content before/after it is preserved. Stdlib only.
"""
import json
import re
import sys
from pathlib import Path

NATIVE = {"claude-code": "CLAUDE.md", "gemini": "GEMINI.md", "qwen": "QWEN.md"}
GATE = Path(__file__).resolve().parent / "gate.py"
GATE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
SETTINGS_LOCAL = ".claude/settings.local.json"
VALUE_FLAGS = {"--host", "--loadout"}  # CLI flags that consume the next token; gate.py validates against this
SECTION_RE = re.compile(r"^## Loadout\b.*?(?=^## |\Z)", re.M | re.S)
ACCEPTED_RE = re.compile(r"^## Accepted\b.*?(?=^## |\Z)", re.M | re.S)
IMPORT_RE = re.compile(r"^@AGENTS\.md\s*$", re.M)
LINE_RE = re.compile(r"^\s*[-*]\s*([^:`]+?)\s*:\s*`?([^`\s]+)`?", re.M)


def gate_hooks():
    """Hook registrations for this machine's copy of gate.py (hence settings.local.json)."""
    def cmd(mode):
        return f'"{sys.executable}" "{GATE}" {mode}'
    return {"PreToolUse": [{"matcher": GATE_MATCHER, "hooks": [{"type": "command", "command": cmd("pre")}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("stop")}]}]}


def load_settings(path):
    """Parsed settings.local.json, or {} when absent. Validated up front so a bad file fails before any write."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ValueError(f"{path} is not valid JSON; fix it or pass --no-enforce")


def register_gate(project, data=None):
    """Upsert the gate hooks into <project>/.claude/settings.local.json. Returns the action."""
    path = Path(project) / SETTINGS_LOCAL
    existed = path.is_file()
    data = load_settings(path) if data is None else data
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, entries in gate_hooks().items():
        current = hooks.get(event, [])
        kept = []
        for e in current:  # drop only our own hook commands; sibling hooks in the same entry survive
            inner = [h for h in e.get("hooks", []) if "gate.py" not in h.get("command", "")]
            if inner:
                kept.append({**e, "hooks": inner} if len(inner) != len(e.get("hooks", [])) else e)
        new = kept + entries
        if new != current:
            hooks[event] = new
            changed = True
    if existed and not changed:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "updated" if existed else "created"


def parse_accepted(text):
    m = ACCEPTED_RE.search(text)
    if not m:
        return []
    return [(stage.strip(), skill.strip()) for stage, skill in LINE_RE.findall(m.group(0))]


def block(accepted):
    lines = ["## Loadout", "Accepted skill workflow for this project (details in LOADOUT.md):"]
    lines += [f"- {stage}: invoke `{skill}`" for stage, skill in accepted]
    lines += ["Invoke these at their stage without being asked. Do not use skills",
              'listed under "Skip" in LOADOUT.md for this project.']
    return "\n".join(lines) + "\n"


def imports_agents(path):
    try:
        return bool(IMPORT_RE.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def upsert_native(path, blk):
    """A CLAUDE.md that imports AGENTS.md stays import-only: the section lives in AGENTS.md,
    and Claude Code would otherwise read it twice. Any duplicate left by an older apply is removed."""
    if path.name == "CLAUDE.md" and imports_agents(path):
        text = path.read_text(encoding="utf-8", errors="replace")
        m = SECTION_RE.search(text)
        if not m:
            return "imports AGENTS.md (unchanged)"
        path.write_text((text[:m.start()] + text[m.end():]).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
        return "duplicate ## Loadout removed (imports AGENTS.md)"
    return upsert(path, blk)


def upsert(path, blk, create_with=None):
    """Replace the ## Loadout section, else append it, else create the file. Returns the action."""
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        m = SECTION_RE.search(text)
        if m:
            sep = "\n" if m.end() < len(text) else ""
            new = text[:m.start()] + blk + sep + text[m.end():]
            action = "replaced"
        else:
            new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + blk
            action = "appended"
    else:
        new = blk if create_with is None else create_with
        action = "created"
    path.write_text(new, encoding="utf-8")
    return action


def apply(project, host, loadout="LOADOUT.md", enforce=True):
    project = Path(project)
    text = (project / loadout).read_text(encoding="utf-8", errors="replace")
    accepted = parse_accepted(text)
    if not accepted:
        raise ValueError(f"no '- <stage>: `<skill>`' lines under '## Accepted' in {loadout}")
    blk = block(accepted)
    gate = enforce and host == "claude-code"
    settings = load_settings(project / SETTINGS_LOCAL) if gate else None  # validate before touching anything
    results = {"AGENTS.md": upsert(project / "AGENTS.md", blk)}
    native = NATIVE.get(host)
    if native:
        path = project / native
        if host == "claude-code" and not path.is_file():
            path.write_text("@AGENTS.md\n", encoding="utf-8", newline="\n")
            results[native] = "created with @AGENTS.md import"
        else:
            results[native] = upsert_native(path, blk)
    for other in NATIVE.values():
        if other != native and (project / other).is_file():
            results[other] = upsert_native(project / other, blk)
    if gate:
        results[SETTINGS_LOCAL] = register_gate(project, settings) + " (gate hooks take effect from the next Claude Code session)"
    return results


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    host = argv[argv.index("--host") + 1] if "--host" in argv else "unknown"
    loadout = argv[argv.index("--loadout") + 1] if "--loadout" in argv else "LOADOUT.md"
    enforce = "--no-enforce" not in argv
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] not in VALUE_FLAGS)]
    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    try:
        results = apply(args[0], host, loadout, enforce)
    except (OSError, ValueError) as e:
        print(f"apply: {e}", file=sys.stderr)
        sys.exit(2)
    for f, action in results.items():
        print(f"- {f}: {action}")


if __name__ == "__main__":
    main()
