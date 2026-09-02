#!/usr/bin/env python3
"""loadout apply — persist the accepted loadout into the project's agent instruction files, idempotently.

Usage: python apply.py <project_dir> --host <host> [--loadout LOADOUT.md]

Reads the '## Accepted' section of LOADOUT.md (lines like '- <stage>: `<skill>`'), builds the
'## Loadout' block and upserts it (replace if present, else append, else create) into:
  - AGENTS.md (read natively by Codex, Cursor, OpenCode, Copilot, ...)
  - the running host's native file: CLAUDE.md / GEMINI.md / QWEN.md. Claude Code does not read
    AGENTS.md, so a missing CLAUDE.md is created with an '@AGENTS.md' import line.
  - any other native file that already exists in the project (keeps every harness consistent).
Re-runs replace the existing section; content before/after it is preserved. Stdlib only.
"""
import re
import sys
from pathlib import Path

NATIVE = {"claude-code": "CLAUDE.md", "gemini": "GEMINI.md", "qwen": "QWEN.md"}
SECTION_RE = re.compile(r"^## Loadout\b.*?(?=^## |\Z)", re.M | re.S)
ACCEPTED_RE = re.compile(r"^## Accepted\b.*?(?=^## |\Z)", re.M | re.S)
IMPORT_RE = re.compile(r"^@AGENTS\.md\s*$", re.M)
LINE_RE = re.compile(r"^\s*[-*]\s*([^:`]+?)\s*:\s*`?([^`\s]+)`?", re.M)


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


def apply(project, host, loadout="LOADOUT.md"):
    project = Path(project)
    text = (project / loadout).read_text(encoding="utf-8", errors="replace")
    accepted = parse_accepted(text)
    if not accepted:
        raise ValueError(f"no '- <stage>: `<skill>`' lines under '## Accepted' in {loadout}")
    blk = block(accepted)
    results = {"AGENTS.md": upsert(project / "AGENTS.md", blk)}
    native = NATIVE.get(host)
    if native:
        path = project / native
        if host == "claude-code" and not path.is_file():
            path.write_text("@AGENTS.md\n", encoding="utf-8")
            results[native] = "created with @AGENTS.md import"
        else:
            results[native] = upsert(path, blk)
    for other in NATIVE.values():
        if other != native and (project / other).is_file():
            results[other] = upsert(project / other, blk)
    return results


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    host = argv[argv.index("--host") + 1] if "--host" in argv else "unknown"
    loadout = argv[argv.index("--loadout") + 1] if "--loadout" in argv else "LOADOUT.md"
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or not argv[i - 1].startswith("--"))]
    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    try:
        results = apply(args[0], host, loadout)
    except (OSError, ValueError) as e:
        print(f"apply: {e}", file=sys.stderr)
        sys.exit(2)
    for f, action in results.items():
        print(f"- {f}: {action}")


if __name__ == "__main__":
    main()
