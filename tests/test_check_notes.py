"""Tests for scripts/check_notes.py against synthetic notes files.

The validator judges references/skill-notes.md on its own contents only: it cannot know
which skills are installed, so coverage is out of scope and every case here is a rule the
file itself can prove.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECK = REPO / "scripts" / "check_notes.py"

HEAD = """# Skill notes (loadout step 3 reference)

## Overlap groups: prefer one

| group | prefer | why |
|---|---|---|
{prefer}

## Skills

| skill | category | does | overlap | tier | upstream |
|---|---|---|---|---|---|
{rows}
"""


def notes(tmp_path, rows, prefer="| delegate (2) | `codex-delegate` | default |"):
    p = tmp_path / "skill-notes.md"
    p.write_text(HEAD.format(prefer=prefer, rows="\n".join(rows)), encoding="utf-8")
    return p


def run(path):
    return subprocess.run([sys.executable, str(CHECK), str(path)],
                          capture_output=True, encoding="utf-8")


GOOD = ["| codex-delegate | delegation | Delegates one task to the Codex CLI | delegate | domain | - |",
        "| cursor-delegate | delegation | Delegates one task to the Cursor CLI | delegate | domain | - |",
        "| planning-with-files | planning | Keeps a plan on disk | - | broad | - |"]


def test_valid_file_passes(tmp_path):
    r = run(notes(tmp_path, GOOD))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "3 rows" in r.stdout


def test_unknown_category_fails(tmp_path):
    rows = list(GOOD)
    rows[2] = "| planning-with-files | organising | Keeps a plan on disk | - | broad | - |"
    r = run(notes(tmp_path, rows))
    assert r.returncode == 1 and "category" in r.stdout


def test_unknown_tier_fails(tmp_path):
    rows = list(GOOD)
    rows[2] = "| planning-with-files | planning | Keeps a plan on disk | - | essential | - |"
    r = run(notes(tmp_path, rows))
    assert r.returncode == 1 and "tier" in r.stdout


def test_duplicate_skill_fails(tmp_path):
    r = run(notes(tmp_path, GOOD + [GOOD[0]]))
    assert r.returncode == 1 and "duplicate" in r.stdout


def test_instruction_shaped_cell_fails(tmp_path):
    """A cell opening in the second person is prompt text lifted from the skill body;
    the table promises its readers data, not instructions."""
    rows = list(GOOD)
    rows[0] = "| codex-delegate | delegation | You are the orchestrator. Hand off a task | delegate | domain | - |"
    r = run(notes(tmp_path, rows))
    assert r.returncode == 1 and "instruction" in r.stdout


def test_group_without_a_preference_fails(tmp_path):
    r = run(notes(tmp_path, GOOD, prefer="| planning (1) | `planning-with-files` | only one |"))
    assert r.returncode == 1 and "no preference" in r.stdout


def test_preferred_skill_outside_its_group_fails(tmp_path):
    r = run(notes(tmp_path, GOOD, prefer="| delegate (2) | `planning-with-files` | wrong group |"))
    assert r.returncode == 1 and "not a member" in r.stdout


def test_group_with_split_category_fails(tmp_path):
    """Members of an overlap group do the same job, so a split category means a row is wrong."""
    rows = list(GOOD)
    rows[1] = "| cursor-delegate | review-verification | Delegates one task | delegate | domain | - |"
    r = run(notes(tmp_path, rows))
    assert r.returncode == 1 and "category" in r.stdout


def test_missing_file_fails_cleanly(tmp_path):
    r = run(tmp_path / "absent.md")
    assert r.returncode == 2 and "not found" in (r.stdout + r.stderr)


def test_preferred_skill_absent_on_this_machine_falls_back(tmp_path):
    """A table is generated on one machine. Where its preferred skill is not installed here,
    the check names the installed member to use instead rather than the absent one."""
    r = subprocess.run([sys.executable, str(CHECK), str(notes(tmp_path, GOOD)),
                        "--installed", "cursor-delegate,planning-with-files"],
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 1
    assert "codex-delegate" in r.stdout and "not installed" in r.stdout
    assert "cursor-delegate" in r.stdout.split("not installed", 1)[1]


def test_group_with_no_installed_member_is_not_reported(tmp_path):
    """The delegate group is simply irrelevant on a host with none of its skills."""
    r = subprocess.run([sys.executable, str(CHECK), str(notes(tmp_path, GOOD)),
                        "--installed", "planning-with-files"],
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout


def test_installed_accepts_a_file(tmp_path):
    lst = tmp_path / "installed.txt"
    lst.write_text("cursor-delegate\nplanning-with-files\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(CHECK), str(notes(tmp_path, GOOD)),
                        "--installed", str(lst)], capture_output=True, encoding="utf-8")
    assert r.returncode == 1 and "not installed" in r.stdout


def test_local_notes_file_is_valid_when_present():
    """The table is machine-local and gitignored, so a clone may not have one."""
    import pytest
    target = REPO / "references" / "skill-notes.md"
    if not target.is_file():
        pytest.skip("no local skill-notes.md generated")
    r = run(target)
    assert r.returncode == 0, r.stdout + r.stderr
