"""Tests for scripts/apply.py: activation and idempotent re-audit of the ## Loadout section."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import apply  # noqa: E402
import scan  # noqa: E402

LOADOUT = ("# Loadout: x\nHarness: claude-code | Project type: cli\nDate: 2026-09-02\n\n"
           "## Recommended workflow\n1. plan → `planner` — why\n\n"
           "## Accepted\n- planning: `planner`\n- implementation: `tdd-skill`\n")


def test_fresh_claude_activation(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "claude-code")
    assert res.pop(".claude/settings.local.json").startswith("created")  # gate registered too
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
    assert res.pop(".claude/settings.local.json").startswith("created")
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


def test_claude_md_with_agents_import_stays_import_only(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    apply.apply(tmp_path, "claude-code")
    first = (tmp_path / "CLAUDE.md").read_bytes()
    assert first == b"@AGENTS.md\n"
    res = apply.apply(tmp_path, "claude-code")
    assert (tmp_path / "CLAUDE.md").read_bytes() == first, "re-apply must be byte-identical"
    assert res["CLAUDE.md"] == "imports AGENTS.md (unchanged)"
    # a duplicate block left behind by an older apply is removed, not retained
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n\n" + apply.block([("planning", "planner")]), encoding="utf-8")
    res = apply.apply(tmp_path, "claude-code")
    assert (tmp_path / "CLAUDE.md").read_bytes() == first
    assert res["CLAUDE.md"] == "duplicate ## Loadout removed (imports AGENTS.md)"
    # mirroring from another host respects the import as well
    res = apply.apply(tmp_path, "gemini")
    assert (tmp_path / "CLAUDE.md").read_bytes() == first and res["CLAUDE.md"] == "imports AGENTS.md (unchanged)"


def test_native_file_table_matches_the_scanner():
    # guard against the two copies drifting when a host is added to scan.py only
    assert apply.NATIVE == scan.NATIVE_FILES


# ---------------------------------------------------------------- enforcement gate registration

def test_claude_registers_gate_hooks_idempotently(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "claude-code")
    assert res[".claude/settings.local.json"].startswith("created")
    data = json.loads((tmp_path / ".claude/settings.local.json").read_text(encoding="utf-8"))
    pre, stop = data["hooks"]["PreToolUse"], data["hooks"]["Stop"]
    assert pre[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit|Bash|EnterWorktree|mcp__.*"
    assert pre[0]["hooks"][0]["command"].endswith('gate.py" pre')
    assert stop[0]["hooks"][0]["command"].endswith('gate.py" stop')
    assert sys.executable in pre[0]["hooks"][0]["command"]
    res = apply.apply(tmp_path, "claude-code")
    assert res[".claude/settings.local.json"].startswith("unchanged")
    data = json.loads((tmp_path / ".claude/settings.local.json").read_text(encoding="utf-8"))
    assert len(data["hooks"]["PreToolUse"]) == 1 and len(data["hooks"]["Stop"]) == 1


def test_register_gate_preserves_other_hooks_and_keys(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    p = tmp_path / ".claude/settings.local.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "other-stop"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": 'python "old/gate.py" pre'}]}]}}),
        encoding="utf-8")
    assert apply.register_gate(tmp_path) == "updated"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "other-stop"
    assert len(data["hooks"]["Stop"]) == 2
    assert len(data["hooks"]["PreToolUse"]) == 1 and "old/gate.py" not in json.dumps(data)


def test_no_enforce_and_other_hosts_skip_registration(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    assert ".claude/settings.local.json" not in apply.apply(tmp_path, "claude-code", enforce=False)
    assert ".claude/settings.local.json" not in apply.apply(tmp_path, "codex")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), "--no-enforce", str(tmp_path), "--host", "claude-code"],
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "settings.local.json" not in r.stdout
    assert not (tmp_path / ".claude/settings.local.json").exists()


def test_invalid_settings_json_is_an_error_before_any_write(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    p = tmp_path / ".claude/settings.local.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        apply.apply(tmp_path, "claude-code")
    assert not (tmp_path / "AGENTS.md").exists() and not (tmp_path / "CLAUDE.md").exists()


def test_register_gate_keeps_sibling_hooks_inside_the_same_entry(tmp_path):
    p = tmp_path / ".claude/settings.local.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "other-stop"},
        {"type": "command", "command": 'python "old/gate.py" stop'}]}]}}), encoding="utf-8")
    assert apply.register_gate(tmp_path) == "updated"
    data = json.loads(p.read_text(encoding="utf-8"))
    cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "other-stop" in cmds and "old/gate.py" not in json.dumps(data)
    assert sum("gate.py" in c for c in cmds) == 1
