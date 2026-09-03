#!/usr/bin/env python3
"""Validate references/skill-notes.md, the table step 3 reads instead of a thin description.

The table is generated from skill bodies, so its failure mode is quiet: a wrong category or a
group with no preferred skill still renders as a valid table and silently misleads the audit.
This checks the rules the file can prove on its own.

Coverage is deliberately out of scope: which skills are installed differs per machine, so a
missing row is not something this file can know about. `scan.py` reports what is installed.

    python3 scripts/check_notes.py [path]     # default: references/skill-notes.md

Exit 0 clean, 1 rule violations (listed), 2 file missing or unparseable.
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

CATEGORIES = {"planning", "brainstorming", "tdd-testing", "debugging", "review-verification",
              "frontend-design", "delegation", "research-docs", "security", "git-vcs",
              "style-modifier", "other"}
TIERS = {"broad", "domain", "niche"}
# A cell opening in the second person or the imperative is prompt text lifted from a skill
# body rather than a description of it; the table promises its readers data, not instructions.
INSTRUCTION = re.compile(r"^(You |Use only|Use when|Always |Prefer |Do not|Never |MUST)")
PREFER_ROW = re.compile(r"^\|\s*([\w:-]+)\s*\(\d+\)\s*\|\s*`([^`]+)`\s*\|")


def parse(text):
    """(rows, preferences) from the notes markdown. Rows are 6-cell skill lines; preferences
    map an overlap group to its preferred skill. Header and separator lines are skipped."""
    rows, prefer = {}, {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        m = PREFER_ROW.match(line)
        if m:
            prefer[m.group(1)] = m.group(2)
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == 6 and cells[0] not in ("skill", "---") and not cells[0].startswith("--"):
            if cells[0] in rows:
                rows[cells[0]] = None  # marks a duplicate; reported below
            else:
                rows[cells[0]] = cells
    return rows, prefer


def check(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        print(f"not found: {path}")
        return 2
    rows, prefer = parse(text)
    if not rows:
        print(f"no skill rows parsed from {path}")
        return 2

    problems = []
    for name, cells in sorted(rows.items()):
        if cells is None:
            problems.append(f"{name}: duplicate row")
            continue
        if cells[1] not in CATEGORIES:
            problems.append(f"{name}: unknown category {cells[1]!r}")
        if cells[4] not in TIERS:
            problems.append(f"{name}: unknown tier {cells[4]!r}")
        if INSTRUCTION.match(cells[2]):
            problems.append(f"{name}: instruction-shaped cell {cells[2][:40]!r}")

    groups = defaultdict(list)
    for name, cells in rows.items():
        if cells and cells[3] != "-":
            groups[cells[3]].append(name)
    for group, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        if group not in prefer:
            problems.append(f"{group}: no preference recorded for {len(members)} members")
        elif prefer[group] not in members:
            problems.append(f"{group}: preferred skill {prefer[group]!r} is not a member")
        cats = sorted({rows[n][1] for n in members})
        if len(cats) > 1:
            problems.append(f"{group}: members disagree on category {cats}")

    for p in problems:
        print(p)
    print(f"{len(rows)} rows, {sum(1 for m in groups.values() if len(m) > 1)} groups, "
          f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "references" / "skill-notes.md")
    sys.exit(check(target))
