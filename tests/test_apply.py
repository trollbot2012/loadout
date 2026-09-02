"""Tests for scripts/apply.py: activation and idempotent re-audit of the ## Loadout section."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import apply  # noqa: E402

LOADOUT = ("# Loadout: x\nHarness: claude-code | Project type: cli\nDate: 2026-09-02\n\n"
           "## Recommended workflow\n1. plan → `planner` — why\n\n"
           "## Accepted\n- planning: `planner`\n- implementation: `tdd-skill`\n")


def test_fresh_claude_activation(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "claude-code")
    assert res == {"AGENTS.md": "created", "CLAUDE.md": "created with @AGENTS.md import"}
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("## Loadout\n")
    assert "- planning: invoke `planner`" in agents and "- implementation: invoke `tdd-skill`" in agents
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert not (tmp_path / "GEMINI.md").exists()


def test_reaudit_replaces_section_and_preserves_neighbours(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    old = ("# notes\nUse pnpm.\n\n## Loadout\nAccepted skill workflow (details in LOADOUT.md):\n"
           "- planning: invoke `old-planner`\nInvoke these.\n\n## After\nkeep me\n")
    (tmp_path / "AGENTS.md").write_text(old, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text(old, encoding="utf-8")
    res = apply.apply(tmp_path, "claude-code")
    assert res == {"AGENTS.md": "replaced", "CLAUDE.md": "replaced"}
    for f in ("AGENTS.md", "CLAUDE.md"):
        text = (tmp_path / f).read_text(encoding="utf-8")
        assert text.count("## Loadout") == 1
        assert "old-planner" not in text and "invoke `planner`" in text
        assert text.startswith("# notes\nUse pnpm.\n\n## Loadout\n")
        assert text.endswith("\n\n## After\nkeep me\n")


def test_idempotent_rerun(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# a\n", encoding="utf-8")
    first = apply.apply(tmp_path, "codex")
    assert first == {"AGENTS.md": "appended"}
    snapshot = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert apply.apply(tmp_path, "codex") == {"AGENTS.md": "replaced"}
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == snapshot


def test_native_file_per_host_and_mirroring(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "gemini")
    assert res == {"AGENTS.md": "created", "GEMINI.md": "created"}
    assert "## Loadout" in (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# c\n", encoding="utf-8")
    res = apply.apply(tmp_path, "qwen")
    assert res == {"AGENTS.md": "replaced", "QWEN.md": "created", "CLAUDE.md": "appended", "GEMINI.md": "replaced"}
    assert not (tmp_path / "GEMINI.md").read_text(encoding="utf-8").count("## Loadout") > 1


def test_missing_accepted_is_an_error(tmp_path):
    (tmp_path / "LOADOUT.md").write_text("# Loadout\n## Recommended workflow\n- x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        apply.apply(tmp_path, "claude-code")


def test_cli(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), str(tmp_path), "--host", "claude-code"],
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "- AGENTS.md: created" in r.stdout and "- CLAUDE.md: created with @AGENTS.md import" in r.stdout
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py")], capture_output=True, encoding="utf-8")
    assert r.returncode == 2
