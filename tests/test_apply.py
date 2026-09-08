"""Tests for scripts/apply.py: activation and idempotent re-audit of the ## Loadout section."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import apply  # noqa: E402
import scan  # noqa: E402
import gate  # noqa: E402

LOADOUT = ("# Loadout: x\nHarness: claude-code | Project type: cli\nDate: 2026-09-02\n\n"
           "## Recommended workflow\n1. plan → `planner` — why\n\n"
           "## Accepted\n- planning: `planner`\n- implementation: `tdd-skill`\n")
CODEX_NOTE = "; trust granted in config.toml (Codex loads hooks at the next session"


@pytest.fixture(autouse=True)
def codex_hooks(tmp_path, monkeypatch):
    """Never touch the real ~/.codex/hooks.json from tests."""
    path = tmp_path / "codex-home" / "hooks.json"
    monkeypatch.setattr(apply, "CODEX_HOOKS", path, raising=False)
    monkeypatch.setattr(apply, "CODEX_CONFIG", tmp_path / "codex-home" / "config.toml", raising=False)
    return path


@pytest.fixture(autouse=True)
def dsh_patch(tmp_path, monkeypatch):
    """Never touch the real ~/.dsh/cordis.patch.yml from tests."""
    path = tmp_path / "dsh-home" / "cordis.patch.yml"
    monkeypatch.setattr(apply, "DSH_PATCH", path, raising=False)
    return path


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
    first = apply.apply(tmp_path, "codex", enforce=False)
    assert first == {"AGENTS.md": "appended"}
    snapshot = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert apply.apply(tmp_path, "codex", enforce=False) == {"AGENTS.md": "replaced"}
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


def test_devteam_table_rows_are_invisible_to_the_accepted_parser():
    """A LOADOUT.md may carry a `| stage | skill |` table under ## Accepted for the devteam
    pipeline, whose parser reads only `|` rows. Loadout reads only the `- stage: `skill`` lines,
    so the two must not see each other's entries."""
    text = ("# Loadout: x\n\n## Accepted\n"
            "- planning: `planner`\n- review: `reviewer`\n"
            "- situational, gated work: `unlazy`\n\n"
            "| stage | skill |\n|---|---|\n"
            "| spec | planner |\n| review | reviewer |\n")
    assert apply.parse_accepted(text) == [
        ("planning", "planner"), ("review", "reviewer"), ("situational, gated work", "unlazy")]
    assert gate.binding_stages(text) == [("planning", "planner"), ("review", "reviewer")]


def test_upsert_writes_lf_for_new_files_and_keeps_non_ascii(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT.replace("planner", "planñer"), encoding="utf-8")
    apply.apply(tmp_path, "unknown")
    raw = (tmp_path / "AGENTS.md").read_bytes()
    assert b"\r\n" not in raw
    assert b"\n" in raw
    assert "planñer" in raw.decode("utf-8")
    again = apply.apply(tmp_path, "unknown")
    assert again["AGENTS.md"] == "replaced"
    assert (tmp_path / "AGENTS.md").read_bytes() == raw


def test_upsert_preserves_existing_crlf_and_neighbours(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    old = "# notes\r\nUse pnpm.\r\n\r\n## Loadout\r\nold\r\n\r\n## After\r\nkeep me\r\n"
    (tmp_path / "AGENTS.md").write_bytes(old.encode("utf-8"))
    apply.apply(tmp_path, "unknown")
    raw = (tmp_path / "AGENTS.md").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    assert text.startswith("# notes\nUse pnpm.\n\n## Loadout\n")
    assert "old-planner" not in text and "invoke `planner`" in text
    assert text.endswith("\n\n## After\nkeep me\n")


def test_missing_accepted_is_an_error(tmp_path):
    (tmp_path / "LOADOUT.md").write_text("# Loadout\n## Recommended workflow\n- x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        apply.apply(tmp_path, "claude-code")


def test_parse_accepted_stops_at_any_heading_and_keeps_dash_and_pipe():
    text = ("# Loadout\n\n## Accepted\n"
            "- planning: `planner`\n- review: reviewer\n"
            "- situational, gated work: `unlazy`\n\n"
            "| stage | skill |\n|---|---|\n| leak | tableskill |\n"
            "### Notes\n- leak: `badskill`\n"
            "## Skip\n- skipstage: `skipped`\n")
    assert apply.parse_accepted(text) == [
        ("planning", "planner"), ("review", "reviewer"), ("situational, gated work", "unlazy")]


def test_parse_accepted_rejects_template_placeholders_and_apply_stays_empty_guard(tmp_path):
    template = ("# Loadout\n\n## Accepted\n"
                "- <stage>: `<skill>`        <- filled in at step 5; exactly this line format\n"
                "- situational, <when>: `<skill>`   <- accepted but not binding on the gate\n")
    assert apply.parse_accepted(template) == []
    (tmp_path / "LOADOUT.md").write_text(template, encoding="utf-8")
    with pytest.raises(ValueError, match="Accepted"):
        apply.apply(tmp_path, "claude-code")
    assert not (tmp_path / "AGENTS.md").exists()


def test_cli(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), str(tmp_path), "--host", "claude-code"],
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "- AGENTS.md: created" in r.stdout and "- CLAUDE.md: created with @AGENTS.md import" in r.stdout
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py")], capture_output=True, encoding="utf-8")
    assert r.returncode == 2


def test_apply_help_exits_0_with_usage(tmp_path):
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), "--help"],
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "Usage:" in r.stdout
    assert "apply.py" in r.stdout
    assert "Traceback" not in r.stderr
    assert not (tmp_path / "AGENTS.md").exists()


def test_apply_valueless_flags_exit_2_without_traceback(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    for args in (["--host"], [str(tmp_path), "--host"], [str(tmp_path), "--loadout"],
                 [str(tmp_path), "--host", "--no-enforce"]):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), *args],
                           capture_output=True, encoding="utf-8")
        assert r.returncode == 2, args
        assert "Traceback" not in r.stderr
        assert "Usage:" in r.stderr or "needs a value" in r.stderr or "apply:" in r.stderr
        assert not (tmp_path / "AGENTS.md").exists(), args


def test_apply_unknown_or_repeated_flags_exit_2_without_writes(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    for args in ([str(tmp_path), "--bogus"],
                 [str(tmp_path), "--host", "unknown", "--host"],
                 [str(tmp_path), "--host", "unknown", "--host", "claude-code"]):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), *args],
                           capture_output=True, encoding="utf-8")
        assert r.returncode == 2, args
        assert "Traceback" not in r.stderr
        assert "Usage:" in r.stderr or "apply:" in r.stderr
        assert not (tmp_path / "AGENTS.md").exists(), args


def test_apply_dash_tokens_blank_values_and_extra_paths_exit_2_without_writes(tmp_path):
    """Each case wrote AGENTS.md (or errored with an OSError) before argv validation was complete."""
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    for args, want in (([str(tmp_path), "-x"], "unknown option"),           # a lone dash token was a positional
                       ([str(tmp_path), "--host", "claude-code", "-y"], "unknown option"),
                       ([str(tmp_path), "--host", ""], "non-empty"),        # applied with an empty host, rc 0
                       ([str(tmp_path), "--loadout", "   "], "non-empty"),  # reached open() and raised OSError
                       ([str(tmp_path), str(other)], "expected one")):      # second path silently dropped
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), *args],
                           capture_output=True, encoding="utf-8")
        assert r.returncode == 2, args
        assert "Traceback" not in r.stderr
        assert want in r.stderr, (args, r.stderr)
        assert not (tmp_path / "AGENTS.md").exists(), args
        assert not (other / "AGENTS.md").exists(), args


def test_cli_host_claude_alias_writes_claude_md_and_registers_gate(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), str(tmp_path), "--host", "claude"],
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "- CLAUDE.md: created with @AGENTS.md import" in r.stdout
    assert "- .claude/settings.local.json:" in r.stdout
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / ".claude/settings.local.json").is_file()


def test_cli_unknown_and_generic_hosts_are_prose_only(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    for host in ("unknown", "cursor", "opencode"):
        dest = tmp_path / host
        dest.mkdir()
        (dest / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), str(dest), "--host", host],
                           capture_output=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr + r.stdout
        assert "- AGENTS.md: created" in r.stdout
        assert "settings.local.json" not in r.stdout
        assert not (dest / "CLAUDE.md").exists()


def test_cli_misspelled_host_exits_2_before_writes(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), str(tmp_path), "--host", "claud"],
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 2
    assert "claud" in r.stderr
    assert "claude-code" in r.stderr
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_claude_md_with_agents_import_stays_import_only(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    apply.apply(tmp_path, "claude-code")
    first = (tmp_path / "CLAUDE.md").read_bytes()
    assert first == b"@AGENTS.md\n"
    res = apply.apply(tmp_path, "claude-code")
    assert (tmp_path / "CLAUDE.md").read_bytes() == first, "re-apply must be byte-identical"
    assert res["CLAUDE.md"] == "imports AGENTS.md (unchanged)"
    # a duplicate block left behind by an older apply is removed, not retained
    (tmp_path / "CLAUDE.md").write_bytes(
        ("@AGENTS.md\n\n" + apply.block([("planning", "planner")])).encode("utf-8"))
    res = apply.apply(tmp_path, "claude-code")
    assert (tmp_path / "CLAUDE.md").read_bytes() == first
    assert res["CLAUDE.md"] == "duplicate ## Loadout removed (imports AGENTS.md)"
    # mirroring from another host respects the import as well
    res = apply.apply(tmp_path, "gemini")
    assert (tmp_path / "CLAUDE.md").read_bytes() == first and res["CLAUDE.md"] == "imports AGENTS.md (unchanged)"


def test_upsert_native_import_duplicate_preserves_crlf(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    blk = apply.block([("planning", "planner")]).replace("\n", "\r\n").encode("utf-8")
    (tmp_path / "CLAUDE.md").write_bytes(b"@AGENTS.md\r\n\r\n" + blk + b"\r\n## After\r\nkeep me\r\n")
    res = apply.apply(tmp_path, "claude-code")
    raw = (tmp_path / "CLAUDE.md").read_bytes()
    assert res["CLAUDE.md"] == "duplicate ## Loadout removed (imports AGENTS.md)"
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert raw.startswith(b"@AGENTS.md\r\n")
    assert b"## Loadout" not in raw
    assert raw.endswith(b"## After\r\nkeep me\r\n")


def test_native_file_table_matches_the_scanner():
    # guard against the two copies drifting when a host is added to scan.py only
    assert apply.NATIVE == scan.NATIVE_FILES


def test_host_aliases_match_the_scanner():
    assert apply.HOST_ALIASES == scan.HOST_ALIASES
    assert apply.KNOWN_HOSTS == frozenset(scan.HOSTS)


def test_flag_sets_cover_every_flag_main_reads():
    assert apply.VALUE_FLAGS == {"--host", "--loadout"}
    assert apply.BOOL_FLAGS == {"--no-enforce", "--enforce-codex", "--enforce-dsh"}
    src = Path(apply.__file__).read_text(encoding="utf-8")
    main = src[src.index("def main():"):]
    for flag in re.findall(r'"--[a-z0-9-]+"', main):
        name = flag.strip('"')
        if name == "--help":
            continue  # usage only; not a gate-allowed apply switch
        assert name in apply.VALUE_FLAGS | apply.BOOL_FLAGS, name


def test_this_repos_loadout_table_matches_its_accepted_list():
    """LOADOUT.md states the accepted set twice: the `- stage: `skill`` lines loadout parses and
    a `| stage | skill |` table the devteam pipeline parses. Nothing else keeps them in step, so
    every skill named in the table must still be one this repo actually accepted."""
    text = (REPO / "LOADOUT.md").read_text(encoding="utf-8")
    accepted = {skill for _, skill in apply.parse_accepted(text)}
    if "| stage | skill |" not in text:
        pytest.skip("no devteam table in LOADOUT.md")
    rows = [ln for ln in text.splitlines() if ln.startswith("|") and "---" not in ln]
    table = {ln.strip("|").split("|")[1].strip().strip("`") for ln in rows[1:]}
    assert table <= accepted, f"table names skills the accepted list does not: {table - accepted}"


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


# ---------------------------------------------------------------- Codex CLI (user-level hooks.json)

def assert_codex_schema_valid(data):
    """Codex's hooks.json root accepts ONLY "description"/"hooks" -- an unrecognised root key fails
    to load every hook in the file, not just an invalid one (the live bug this module fixes). This
    checks Codex-schema validity, not merely that the file is valid JSON: root keys, the hooks-map
    shape, each group's shape, and that every handler carries only known fields."""
    assert isinstance(data, dict)
    assert set(data) <= {"description", "hooks"}
    hooks = data.get("hooks", {})
    assert isinstance(hooks, dict)
    for event, groups in hooks.items():
        assert isinstance(event, str)
        assert isinstance(groups, list)
        for group in groups:
            assert isinstance(group, dict)
            assert set(group) <= {"matcher", "hooks"}
            handlers = group.get("hooks", [])
            assert isinstance(handlers, list)
            for h in handlers:
                assert isinstance(h, dict)
                assert set(h) <= {"type", "command", "commandWindows", "timeout", "description"}
                assert h.get("type") == "command"
                assert h.get("command")


def gate_cmds(data, event):
    return [(h["command"], h["commandWindows"]) for e in data["hooks"][event] for h in e["hooks"] if "gate.py" in h["command"]]


def test_codex_hooks_created_with_both_events_and_command_forms(codex_hooks):
    assert apply.register_codex_gate(codex_hooks) == "created"
    data = json.loads(codex_hooks.read_text(encoding="utf-8"))
    assert_codex_schema_valid(data)
    assert set(data) == {"hooks"}  # fresh file: a valid envelope, gate under "hooks"
    assert set(data["hooks"]) == {"PreToolUse", "Stop"}
    assert "matcher" not in data["hooks"]["PreToolUse"][0]
    for event, mode in (("PreToolUse", "pre"), ("Stop", "stop")):
        (posix, win), = gate_cmds(data, event)
        assert posix == f'"{sys.executable}" "{apply.GATE}" {mode} --host codex'
        assert win == "& " + posix
        assert data["hooks"][event][0]["hooks"][0]["timeout"] == 20
    raw = codex_hooks.read_bytes()
    assert raw.endswith(b"}\n") and b"\r\n" not in raw
    assert apply.register_codex_gate(codex_hooks) == "unchanged"
    assert len(json.loads(codex_hooks.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]) == 1


def test_codex_hooks_preserve_foreign_entries_and_replace_stale_gate(codex_hooks):
    # a realistic real-world file: every tool's entries (including devteam-codex's) already live under
    # the root "hooks" object, one of them already using commandWindows -- none of this is ours to touch
    codex_hooks.parent.mkdir(parents=True)
    existing = {"description": "team hooks", "hooks": {
        "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
            "command": 'node devteam-codex.mjs hook PostToolUse',
            "commandWindows": '& node devteam-codex.mjs hook PostToolUse'}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "session-a"}]},
                         {"hooks": [{"type": "command", "command": "session-b"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "foreign-stop"},
                            {"type": "command", "command": 'python "old/gate.py" stop --host codex',
                             "commandWindows": '& python "old/gate.py" stop --host codex'}]}],
        "PreToolUse": [{"hooks": [{"type": "command", "command": 'python "old/gate.py" pre --host codex'}]}]}}
    before = json.loads(json.dumps(existing))  # deep copy: compare untouched parts against this, not `existing`
    codex_hooks.write_text(json.dumps(existing), encoding="utf-8")
    assert apply.register_codex_gate(codex_hooks) == "updated"
    data = json.loads(codex_hooks.read_text(encoding="utf-8"))
    assert_codex_schema_valid(data)
    assert set(data) == {"hooks", "description"}
    assert data["description"] == "team hooks"
    assert data["hooks"]["PostToolUse"] == before["hooks"]["PostToolUse"]
    assert data["hooks"]["SessionStart"] == before["hooks"]["SessionStart"]
    assert "old/gate.py" not in json.dumps(data)
    stop_cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "foreign-stop" in stop_cmds
    for event in ("PreToolUse", "Stop"):
        assert len(gate_cmds(data, event)) == 1
    assert len(data["hooks"]["PreToolUse"]) == 1


def test_codex_migrates_stray_root_level_gate_keys_from_the_live_bug(codex_hooks):
    # the live bug, reproduced: an earlier apply wrote its own gate entries as root-level "PreToolUse"/
    # "Stop" keys (invalid per Codex's schema), alongside a valid root "hooks" object for another tool
    codex_hooks.parent.mkdir(parents=True)
    posix_pre = f'"{sys.executable}" "{apply.GATE}" pre --host codex'
    posix_stop = f'"{sys.executable}" "{apply.GATE}" stop --host codex'
    existing = {
        "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "devteam-codex.mjs hook PostToolUse"}]}]},
        "PreToolUse": [{"hooks": [{"type": "command", "command": posix_pre,
                                    "commandWindows": "& " + posix_pre, "timeout": 20}]}],
        "Stop": [{"hooks": [{"type": "command", "command": posix_stop,
                              "commandWindows": "& " + posix_stop, "timeout": 20}]}]}
    codex_hooks.write_text(json.dumps(existing), encoding="utf-8")
    assert apply.register_codex_gate(codex_hooks) == "updated"
    data = json.loads(codex_hooks.read_text(encoding="utf-8"))
    assert_codex_schema_valid(data)
    assert set(data) == {"hooks"}
    assert "PreToolUse" not in data and "Stop" not in data  # the invalid root-level keys are gone
    assert data["hooks"]["PostToolUse"] == existing["hooks"]["PostToolUse"]  # nothing else was lost
    for event in ("PreToolUse", "Stop"):
        assert len(gate_cmds(data, event)) == 1  # migrated, not duplicated
    before = codex_hooks.read_bytes()
    assert apply.register_codex_gate(codex_hooks) == "unchanged"
    assert codex_hooks.read_bytes() == before


def test_codex_foreign_root_level_key_is_preserved_and_reported(codex_hooks):
    # a root-level event key that is NOT ours (someone/something else's mistake, or a future Codex
    # feature this code doesn't know about yet) must never be silently deleted or absorbed
    codex_hooks.parent.mkdir(parents=True)
    existing = {"hooks": {}, "PreToolUse": [{"hooks": [{"type": "command", "command": "someone-elses-tool"}]}]}
    codex_hooks.write_text(json.dumps(existing), encoding="utf-8")
    action = apply.register_codex_gate(codex_hooks)
    data = json.loads(codex_hooks.read_text(encoding="utf-8"))
    assert data["PreToolUse"] == existing["PreToolUse"], "third-party root-level data must never be destroyed"
    assert "PreToolUse" in action, "the leftover stray key is reported back to the caller"
    assert len(gate_cmds(data, "PreToolUse")) == 1  # our own gate entry still lands correctly under hooks
    # the root stays invalid on purpose (that foreign key is not ours to move), so the oracle is applied
    # to the part we do own: everything we wrote under "hooks" is still a schema-valid envelope
    assert_codex_schema_valid({"hooks": data["hooks"]})


def test_codex_apply_is_idempotent(codex_hooks):
    codex_hooks.parent.mkdir(parents=True)
    apply.register_codex_gate(codex_hooks)
    first = codex_hooks.read_bytes()
    assert apply.register_codex_gate(codex_hooks) == "unchanged"
    assert codex_hooks.read_bytes() == first
    data = json.loads(first)
    assert_codex_schema_valid(data)
    for event in ("PreToolUse", "Stop"):
        assert len(gate_cmds(data, event)) == 1


def test_codex_invalid_hooks_json_is_an_error_before_any_write(tmp_path, codex_hooks):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    codex_hooks.parent.mkdir(parents=True)
    codex_hooks.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="hooks.json"):
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert not (tmp_path / "AGENTS.md").exists()


def test_codex_host_writes_agents_md_and_user_hooks(tmp_path, codex_hooks):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "codex", enforce_codex=True)
    assert res["AGENTS.md"] == "created" and set(res) == {"AGENTS.md", "~/.codex/hooks.json"}
    assert res["~/.codex/hooks.json"].startswith("created" + CODEX_NOTE)
    assert codex_hooks.is_file() and not (tmp_path / ".claude").exists()
    assert_codex_schema_valid(json.loads(codex_hooks.read_text(encoding="utf-8")))
    assert apply.apply(tmp_path, "codex", enforce_codex=True)["~/.codex/hooks.json"].startswith(
        "unchanged; trust already present")
    assert "~/.codex/hooks.json" not in apply.apply(tmp_path, "codex", enforce=False, enforce_codex=True)
    assert "~/.codex/hooks.json" not in apply.apply(tmp_path, "claude-code", enforce_codex=True)


# ---------------------------------------------------------------- codex hook trust

RECORDED_TRIM_HOOK_HASH = "sha256:bf4e354026f5ca05bfdaabfe890dabeb551faa59c9e039ccfc62617bdec94b87"


def test_codex_hook_hash_matches_a_recorded_trusted_hash():
    # handler + hash copied verbatim from a real ~/.codex (hooks.json + config.toml [hooks.state]); Codex
    # recorded it on Windows, so the commandWindows form is the one hashed. description is ignored.
    group = {"matcher": "Bash", "hooks": [{
        "type": "command", "timeout": 10,
        "command": 'node "$HOME/.codex/hooks/trim-noisy-command-output.mjs"',
        "commandWindows": r'"C:\Users\Waxilliam\AppData\Local\OpenAI\Codex\runtimes\cua_node\950613ca46815e82\bin\node.exe" "C:\Users\Waxilliam\.codex\hooks\trim-noisy-command-output.mjs"',
        "description": "Compress noisy build-like command output before model context"}]}
    assert apply.codex_hook_hash("post_tool_use", group, group["hooks"][0], windows=True) == RECORDED_TRIM_HOOK_HASH
    assert apply.codex_hook_hash("post_tool_use", group, group["hooks"][0], windows=False) != RECORDED_TRIM_HOOK_HASH


def test_trust_codex_gate_upserts_hooks_state_and_preserves_the_rest(tmp_path):
    hooks, cfg = tmp_path / "hooks.json", tmp_path / "config.toml"
    apply.register_codex_gate(hooks)
    head = 'model = "x"\n\n[features]\nhooks = true\n\n[hooks.state]\n\n[hooks.state.\'C:\\other\\hooks.json:stop:0:0\']\ntrusted_hash = "sha256:keep"\n'
    cfg.write_bytes(head.encode("utf-8"))
    assert apply.trust_codex_gate(hooks, cfg) == "trusted"
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith(head), "existing lines are preserved byte for byte"
    for event in ("pre_tool_use", "stop"):
        assert f"[hooks.state.'{hooks}:{event}:0:0']" in text
    assert text.count("trusted_hash") == 3
    assert apply.trust_codex_gate(hooks, cfg) == "unchanged"
    assert cfg.read_text(encoding="utf-8") == text
    # a stale hash for our handler is replaced in place, not appended
    import re
    stale = re.sub(r"(pre_tool_use:0:0'\]\ntrusted_hash = )\"[^\"]+\"", r'\1"sha256:stale"', text)
    assert "sha256:stale" in stale
    cfg.write_bytes(stale.encode("utf-8"))
    assert apply.trust_codex_gate(hooks, cfg) == "trusted"
    again = cfg.read_text(encoding="utf-8")
    assert "sha256:stale" not in again and again.count("trusted_hash") == 3
    # a missing config.toml is created with just the state table
    fresh = tmp_path / "fresh.toml"
    assert apply.trust_codex_gate(hooks, fresh) == "trusted"
    assert fresh.read_text(encoding="utf-8").count("trusted_hash") == 2


def test_trust_codex_gate_keys_match_nested_positions_not_root(tmp_path):
    # a foreign group sits before ours under "hooks"; the trust key's gi must reflect that nested
    # index. Nothing lives at the (invalid) hooks.json root, so the pre-fix code -- which walked
    # data.get("PreToolUse", []) at the root -- would find nothing here at all.
    hooks, cfg = tmp_path / "hooks.json", tmp_path / "config.toml"
    ours = {"type": "command", "command": f'"{sys.executable}" "{apply.GATE}" pre --host codex',
            "commandWindows": "& x", "timeout": 20}
    data = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "foreign"}]},
        {"hooks": [ours]}]}}
    hooks.write_text(json.dumps(data), encoding="utf-8")
    assert apply.trust_codex_gate(hooks, cfg) == "trusted"
    text = cfg.read_text(encoding="utf-8")
    assert f"[hooks.state.'{hooks}:pre_tool_use:1:0']" in text
    assert f"[hooks.state.'{hooks}:pre_tool_use:0:0']" not in text


def test_apply_codex_grants_trust(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, "CODEX_CONFIG", tmp_path / "codex-home" / "config.toml")
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "trust granted" in res["~/.codex/hooks.json"] and "skipped" not in res["~/.codex/hooks.json"]
    assert (tmp_path / "codex-home" / "config.toml").read_text(encoding="utf-8").count("trusted_hash") == 2


def test_trust_codex_gate_ignores_comments_and_never_duplicates_a_key(tmp_path):
    hooks, cfg = tmp_path / "hooks.json", tmp_path / "config.toml"
    apply.register_codex_gate(hooks)
    key = f"{hooks}:pre_tool_use:0:0"
    # a commented-out header must not be treated as the section; a section with an extra line between
    # header and hash must be replaced whole, never left with two trusted_hash keys (Codex would fail to parse)
    cfg.write_bytes((f"# [hooks.state.'{key}']\n# trusted_hash = \"sha256:old\"\n\n[hooks.state.'{key}']\n"
                     f"# note\ntrusted_hash = \"sha256:stale\"\n\n[other]\nk = 1\n").encode("utf-8"))
    assert apply.trust_codex_gate(hooks, cfg) == "trusted"
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith(f"# [hooks.state.'{key}']\n# trusted_hash = \"sha256:old\"\n"), "comment lines untouched"
    assert "sha256:stale" not in text and text.count("trusted_hash") == 3  # commented one + 2 real
    assert text.rstrip().endswith("[other]\nk = 1") or "[other]\nk = 1" in text
    section = text.split(f"[hooks.state.'{key}']\n")[-1]
    assert section.startswith("trusted_hash = \"sha256:")


# ---------------------------------------------------------------- DeepSeek Harness (user-level cordis.patch.yml)

DSH_NOTE = " (dsh loads the plugin at the next session"


def dsh_text(path):
    return path.read_text(encoding="utf-8")


def test_dsh_entry_name_is_a_file_url():
    entry = apply.dsh_entry()
    assert entry.endswith("\n") and "\r" not in entry
    assert entry.startswith("- insert:\n    - id: loadout-gate\n      name: file:///")
    name = entry.rstrip("\n").rsplit("name: ", 1)[1]
    assert name.startswith("file:///") and "\\" not in name and name.endswith("/scripts/gate_dsh.mjs")
    assert apply.dsh_entry(Path("C:/x/y/gate_dsh.mjs")).rstrip("\n").endswith("name: file:///C:/x/y/gate_dsh.mjs")


def test_dsh_patch_created_when_absent(dsh_patch):
    assert not dsh_patch.exists()
    assert apply.register_dsh_gate(dsh_patch) == "created"
    text = dsh_text(dsh_patch)
    assert text.startswith("#") and "\r\n" not in text
    assert text.endswith(apply.dsh_entry())
    assert text.count("- insert:") == 1


def test_dsh_empty_array_is_replaced_and_comments_kept(dsh_patch):
    dsh_patch.parent.mkdir(parents=True)
    dsh_patch.write_bytes(b"# cordis loader patches\n# edit freely\n[]\n")
    assert apply.register_dsh_gate(dsh_patch) == "updated"
    text = dsh_text(dsh_patch)
    assert "[]" not in text
    assert text == "# cordis loader patches\n# edit freely\n" + apply.dsh_entry()


def test_dsh_foreign_entry_survives_and_ours_is_appended(dsh_patch):
    dsh_patch.parent.mkdir(parents=True)
    other = "# top\n- insert:\n    - id: other-plugin\n      name: file:///C:/x/other.mjs\n"
    dsh_patch.write_bytes(other.encode("utf-8"))
    assert apply.register_dsh_gate(dsh_patch) == "updated"
    text = dsh_text(dsh_patch)
    assert text.startswith(other), "foreign entry and comments preserved byte for byte"
    assert text.endswith(apply.dsh_entry())
    assert text.count("- insert:") == 2


def test_dsh_stale_entry_is_replaced_in_place_not_duplicated(dsh_patch):
    dsh_patch.parent.mkdir(parents=True)
    stale = ("# head\r\n- insert:\r\n    - id: loadout-gate\r\n      name: file:///C:/old/scripts/gate_dsh.mjs\r\n"
             "- insert:\r\n    - id: zzz\r\n      name: file:///C:/x/zzz.mjs\r\n")
    dsh_patch.write_bytes(stale.encode("utf-8"))
    assert apply.register_dsh_gate(dsh_patch) == "updated"
    raw = dsh_patch.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "CRLF preserved"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    assert text == ("# head\n" + apply.dsh_entry()
                    + "- insert:\n    - id: zzz\n      name: file:///C:/x/zzz.mjs\n")
    assert "C:/old/scripts" not in text and text.count("loadout-gate") == 1


def test_dsh_rerun_is_unchanged_and_byte_identical(dsh_patch):
    assert apply.register_dsh_gate(dsh_patch) == "created"
    first = dsh_patch.read_bytes()
    assert apply.register_dsh_gate(dsh_patch) == "unchanged"
    assert dsh_patch.read_bytes() == first


def test_dsh_host_writes_agents_md_and_registers(tmp_path, monkeypatch, dsh_patch):
    # Subject is registration, not interpreter discovery: pin the interpreter already running this
    # test so a PATH without `python`/`python3` fails this on its own subject or not at all. The
    # dedicated resolver negatives below cover discovery and stay unpinned.
    monkeypatch.setenv("LOADOUT_PYTHON", sys.executable)
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "deepseek", enforce_dsh=True)
    assert set(res) == {"AGENTS.md", "~/.dsh/cordis.patch.yml"}
    assert res["AGENTS.md"] == "created"
    assert res["~/.dsh/cordis.patch.yml"].startswith("created" + DSH_NOTE)
    assert "no per-repo config" in res["~/.dsh/cordis.patch.yml"]
    assert "## Loadout" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert dsh_patch.is_file()
    assert not (tmp_path / ".claude").exists() and not (tmp_path / "CLAUDE.md").exists()
    assert apply.apply(tmp_path, "dsh", enforce_dsh=True)["~/.dsh/cordis.patch.yml"].startswith("unchanged" + DSH_NOTE)


def test_dsh_no_enforce_and_other_hosts_skip_registration(tmp_path, dsh_patch, codex_hooks):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    assert "~/.dsh/cordis.patch.yml" not in apply.apply(tmp_path, "deepseek", enforce=False, enforce_dsh=True)
    assert not dsh_patch.exists()
    assert "~/.dsh/cordis.patch.yml" not in apply.apply(tmp_path, "codex")
    assert not dsh_patch.exists()


def test_dsh_gate_is_opt_in(tmp_path, monkeypatch, dsh_patch):
    """New DSH registration is machine-wide (no per-repo plugin config). Default apply
    must neither create nor rewrite cordis.patch.yml; --enforce-dsh opts in. Foreign
    entries survive; --no-enforce still wins."""
    # Registration subject, not discovery -- see test_dsh_host_writes_agents_md_and_registers.
    # setenv reaches the apply.py subprocess below too, which is the point.
    monkeypatch.setenv("LOADOUT_PYTHON", sys.executable)
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "deepseek")
    assert set(res) == {"AGENTS.md"}
    assert not dsh_patch.exists(), "new DSH registration requires --enforce-dsh"
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "apply.py"), str(tmp_path), "--host", "dsh"],
        capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "cordis.patch.yml" not in r.stdout
    assert not dsh_patch.exists()

    foreign = "# top\n- insert:\n    - id: other-plugin\n      name: file:///C:/x/other.mjs\n"
    dsh_patch.parent.mkdir(parents=True, exist_ok=True)
    dsh_patch.write_bytes(foreign.encode("utf-8"))
    assert "~/.dsh/cordis.patch.yml" not in apply.apply(tmp_path, "deepseek")
    assert dsh_text(dsh_patch) == foreign, "reapply without --enforce-dsh must not rewrite existing registrations"

    res = apply.apply(tmp_path, "deepseek", enforce_dsh=True)
    assert res["~/.dsh/cordis.patch.yml"].startswith("updated" + DSH_NOTE)
    assert dsh_text(dsh_patch).startswith(foreign)
    assert "loadout-gate" in dsh_text(dsh_patch)
    assert "~/.dsh/cordis.patch.yml" not in apply.apply(tmp_path, "deepseek", enforce=False, enforce_dsh=True)


# ------------------------------------------- dsh interpreter agreement (real PATH, real processes)

def _node_says(argv, env, timeout):
    """Run a node candidate headless and return (stdout, problem). Every way the run can fail --
    launch error, timeout, nonzero exit, nothing on stdout -- comes back as a problem string naming
    the executable and the reason, so none of them can be mistaken for node being absent."""
    try:
        r = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return "", f"{argv[0]} did not answer {argv[1:]} within {timeout}s"
    except OSError as e:
        return "", f"{argv[0]} could not be launched: {e}"
    if r.returncode != 0:
        return "", f"{argv[0]} exited {r.returncode} on {argv[1:]}: {r.stderr.strip()[:300]}"
    if not r.stdout.strip():
        return "", f"{argv[0]} answered {argv[1:]} with nothing"
    return r.stdout.strip(), None


def _discover_node(timeout=60):
    """The node the plugin tests below spawn, as (executable, problem). `shutil.which` can land on a
    launcher -- mise's shim is one -- that re-resolves through `mise` on PATH, and those tests
    replace PATH with a python-only fixture, which is exactly the dependency they strip. So ask the
    candidate for its own `process.execPath`, the real binary a launcher starts, and keep it only if
    that binary still runs with PATH constrained the way the fixture constrains it.

    No node at all is a capability skip. A node that is present but fails either step is a problem,
    never a skip: it is precisely the state that used to suppress the three checks it breaks, which
    is how a broken launcher read as a clean run."""
    cand = shutil.which("node")
    if not cand:
        return None, None
    real, problem = _node_says([cand, "-p", "process.execPath"], os.environ, timeout)
    if problem:
        return None, f"node is on PATH but unusable -- {problem}"
    _, problem = _node_says([real, "-p", "0"], dict(os.environ, PATH=""), timeout)
    if problem:
        return None, f"node from {cand} does not run with PATH emptied -- {problem}"
    return real, None


def _node_or_reason(node, problem):
    """Absence is a capability skip; present-but-unusable is a failure. Splitting the two is the
    whole point: a skip on the second would hide the tests that prove interpreter agreement."""
    if problem:
        pytest.fail(problem)
    if not node:
        pytest.skip("no node on this host")
    return node


NODE, NODE_PROBLEM = _discover_node()


@pytest.fixture
def node():
    return _node_or_reason(NODE, NODE_PROBLEM)


def _shim(directory, name, command):
    """A launcher on a real PATH that forwards to `command`. Windows gets a `.cmd`, POSIX a `sh`
    script; `shutil.which` finds either and both really run, so PATH resolution and the process
    result are native here, not mocked."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = directory / (name + ".cmd")
        path.write_text("@echo off\r\n" + subprocess.list2cmdline(command) + " %*\r\n", encoding="utf-8")
    else:
        path = directory / name
        path.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    return path


def _reported_executable(path):
    """What `path` reports as its own sys.executable -- the value both sides pin and spawn. A
    relocated executable reports itself; a launcher reports the interpreter it starts."""
    r = subprocess.run([str(path), "-c", "import sys; sys.stdout.write(sys.executable)"],
                       capture_output=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _native_python(directory, name, monkeypatch):
    """A real interpreter executable named `name`, alone on a real PATH. Node's spawnSync will not
    run a `.cmd` (EINVAL) and does not PATHEXT-resolve one, so the plugin-side tests need a genuine
    executable, not a launcher. On Windows that means hardlinking (or copying) this interpreter and
    the DLLs beside it, because a trimmed PATH is exactly what stops it finding them; PYTHONHOME
    then points the relocated copy at the real stdlib. Skipped, not faked, if it will not run."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        return _shim(directory, name, [sys.executable])
    path = directory / (name + ".exe")
    for src in [Path(sys.executable)] + sorted(Path(sys.executable).parent.glob("*.dll")):
        dest = directory / (path.name if src.name == Path(sys.executable).name else src.name)
        try:
            os.link(src, dest)
        except OSError:  # pragma: no cover - host dependent (cross-volume, or no link support)
            shutil.copy2(src, dest)
    monkeypatch.setenv("PYTHONHOME", sys.prefix)
    env = dict(os.environ, PATH=str(directory), PYTHONHOME=sys.prefix)
    try:
        r = subprocess.run([str(path), "-c", "import sys; sys.stdout.write(sys.executable)"],
                           capture_output=True, encoding="utf-8", timeout=60, env=env)
    except OSError as e:  # pragma: no cover - host dependent
        pytest.skip(f"a relocated interpreter does not run on this host: {e}")
    if r.returncode != 0 or r.stdout.strip() != str(path):  # pragma: no cover - host dependent
        pytest.skip(f"a relocated interpreter does not run on this host: {r.returncode} {r.stderr[:200]}")
    return path


def test_dsh_pins_the_interpreter_it_validated_on_a_python3_only_path(tmp_path, monkeypatch, dsh_patch):
    """Regression: on a python3-only PATH apply validated `python3` and the plugin then spawned
    `python`, which is not there -- and on this fail-closed host a spawn failure denies everything.
    The PATH here really carries only python3, and the entry pins what apply proved."""
    real = _native_python(tmp_path / "bin", "python3", monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("LOADOUT_PYTHON", raising=False)
    assert shutil.which("python") is None, "fixture PATH must not offer `python`"
    picked = apply._dsh_python()
    assert Path(picked).samefile(_reported_executable(real)), "PATH case may differ; the file must not"

    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "dsh", enforce_dsh=True)
    assert res["~/.dsh/cordis.patch.yml"].startswith("created" + DSH_NOTE)
    assert res["~/.dsh/cordis.patch.yml"].endswith(f"; plugin interpreter pinned to {picked}")
    text = dsh_text(dsh_patch)
    assert "      config:\n        python: " + json.dumps(picked) + "\n" in text
    assert apply.apply(tmp_path, "dsh", enforce_dsh=True)["~/.dsh/cordis.patch.yml"].startswith("unchanged")


def test_dsh_entry_pins_python_only_when_one_is_given():
    assert "config:" not in apply.dsh_entry()
    entry = apply.dsh_entry(Path("C:/x/y/gate_dsh.mjs"), r"C:\Program Files\py 3\python.exe")
    assert entry == ('- insert:\n    - id: loadout-gate\n      name: file:///C:/x/y/gate_dsh.mjs\n'
                     '      config:\n        python: "C:\\\\Program Files\\\\py 3\\\\python.exe"\n')


def test_dsh_rejects_an_existing_but_unusable_interpreter(tmp_path, monkeypatch, dsh_patch):
    """A `which` hit is not proof of usability. The stub here is a real file on a real PATH that
    really runs and really fails; selection falls through to the candidate that works."""
    binm = tmp_path / "bin"
    _shim(binm, "python", [sys.executable, "-c", "raise SystemExit(9)"])
    monkeypatch.setenv("PATH", str(binm))
    monkeypatch.delenv("LOADOUT_PYTHON", raising=False)
    assert shutil.which("python") is not None, "the unusable candidate resolves on PATH"
    assert apply._usable_python("python") is None
    assert apply._dsh_python() is None, "no fallback exists yet"

    _shim(binm, "python3", [sys.executable])
    assert apply._dsh_python() == sys.executable, "a launcher resolves to the interpreter it starts"


def test_dsh_no_usable_interpreter_keeps_prose_and_names_both_candidates(tmp_path, monkeypatch, dsh_patch):
    binm = tmp_path / "bin"
    for name in ("python", "python3"):
        _shim(binm, name, [sys.executable, "-c", "raise SystemExit(9)"])
    monkeypatch.setenv("PATH", str(binm))
    monkeypatch.delenv("LOADOUT_PYTHON", raising=False)
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "dsh", enforce_dsh=True)
    assert "python3" in str(ei.value) and "LOADOUT_PYTHON" in str(ei.value)
    assert ei.value.results["AGENTS.md"] == "created"
    assert (tmp_path / "AGENTS.md").is_file() and not dsh_patch.exists()


def test_explicit_loadout_python_is_reported_not_silently_replaced(tmp_path, monkeypatch, dsh_patch):
    """An explicit choice that cannot run is an error naming that choice. Registering some other
    interpreter under it would claim a selection the operator never made."""
    binm = tmp_path / "bin"
    _shim(binm, "python", [sys.executable])
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    monkeypatch.setenv("PATH", str(binm))

    # the case that used to register: an explicit choice that exists and does not run. It was
    # pinned on the strength of being a file, and every spawn then failed into a total deny.
    broken = _shim(binm, "not-a-python", [sys.executable, "-c", "raise SystemExit(9)"])
    monkeypatch.setenv("LOADOUT_PYTHON", str(broken))
    assert Path(broken).is_file() and apply._dsh_python() is None
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "dsh", enforce_dsh=True)
    assert "LOADOUT_PYTHON" in str(ei.value) and broken.name in str(ei.value)
    assert not dsh_patch.exists() and (tmp_path / "AGENTS.md").is_file()
    assert apply._dsh_python() != str(_shim(binm, "python", [sys.executable])), "no silent fallback"

    monkeypatch.setenv("LOADOUT_PYTHON", str(tmp_path / "no-such-python"))
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "dsh", enforce_dsh=True)
    assert "no-such-python" in str(ei.value)
    assert not dsh_patch.exists()

    monkeypatch.setenv("LOADOUT_PYTHON", sys.executable)
    apply.apply(tmp_path, "dsh", enforce_dsh=True)
    assert "python: " + json.dumps(sys.executable) in dsh_text(dsh_patch)


def test_dsh_registration_failure_after_prose_reports_the_real_writes(tmp_path, monkeypatch, dsh_patch, capsys):
    """A registration write that fails after the prose landed must report what really landed: the
    prose result is kept in the failure, the CLI prints it and exits 2, and nothing claims a clean
    skip, a rollback or a registration that did not happen."""
    # Registration subject, not discovery -- see test_dsh_host_writes_agents_md_and_registers.
    monkeypatch.setenv("LOADOUT_PYTHON", sys.executable)
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")

    def boom(*a, **k):
        raise OSError("cordis.patch.yml is not writable")

    monkeypatch.setattr(apply, "register_dsh_gate", boom)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "dsh", enforce_dsh=True)
    assert ei.value.results["AGENTS.md"] == "created"
    assert "~/.dsh/cordis.patch.yml" not in ei.value.results
    assert "not writable" in str(ei.value)
    assert "## Loadout" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert not dsh_patch.exists()

    monkeypatch.setattr(sys, "argv", ["apply.py", str(tmp_path), "--host", "dsh", "--enforce-dsh"])
    with pytest.raises(SystemExit) as si:
        apply.main()
    assert si.value.code == 2
    out = capsys.readouterr().out
    assert "- AGENTS.md: " in out
    assert "enforcement: skipped: cordis.patch.yml is not writable" in out
    assert "registered" not in out and "cordis.patch.yml: " not in out


def _drive_plugin(node, tmp_path, config, env=None):
    """Load the real plugin the way dsh does -- by file:// URL, with an entry's `config` -- and put
    one mutating tool through `tools/pre-execute`. Returns (decision, interpreter that ran the gate).
    The gate it spawns is a stand-in that records `sys.executable` and allows."""
    record = tmp_path / "spawned-by.txt"
    probe = tmp_path / "probe_gate.py"
    probe.write_text("import sys, pathlib\nsys.stdin.read()\n"
                     f"pathlib.Path({json.dumps(str(record))}).write_text(sys.executable, encoding='utf-8')\n",
                     encoding="utf-8")
    url = "file:///" + str(apply.DSH_PLUGIN).replace("\\", "/").lstrip("/")
    driver = tmp_path / "drive.mjs"
    driver.write_text(
        f"import {{ apply }} from {json.dumps(url)};\n"
        "const handlers = {};\n"
        "apply({ on: (evt, fn) => { handlers[evt] = fn; } }, JSON.parse(process.argv[2]));\n"
        "const out = handlers['tools/pre-execute']({ name: 'write', arguments: { file_path: 'a.txt' } },"
        " () => ({ kind: 'allow' }));\n"
        "process.stdout.write(JSON.stringify(out ?? null));\n", encoding="utf-8")
    cfg = dict(config)
    cfg.setdefault("gate", str(probe))
    r = subprocess.run([node, str(driver), json.dumps(cfg)], capture_output=True,
                       encoding="utf-8", errors="replace", timeout=120, cwd=str(tmp_path), env=env)
    assert r.returncode == 0, r.stderr
    ran = record.read_text(encoding="utf-8") if record.is_file() else None
    return json.loads(r.stdout), ran, r.stderr


def test_node_discovery_survives_the_fixture_path(tmp_path, node):
    """Regression: discovery cached whatever `which` found, and where that was mise's shim the two
    plugin tests below could not start node at all once they had trimmed PATH to their fixture --
    the failure was the harness, not the adapter. Whatever discovery pins must run under that same
    trimmed PATH, and must be the binary that then runs, not a launcher standing in front of it."""
    r = subprocess.run([node, "-p", "process.execPath"], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=60, env=dict(os.environ, PATH=str(tmp_path / "bin")))
    assert r.returncode == 0, f"discovered node does not run under the fixture PATH: {r.stderr[:300]}"
    assert Path(r.stdout.strip()).samefile(node), "discovery must pin the executable that runs"


def _fake_node(bin_dir, script, body):
    """A `node` on a real PATH that is a real process: it launches, runs `body`, and exits for real.
    Only the answers are chosen -- the launch, the exit status and the pipes are the host's."""
    script.write_text(body, encoding="utf-8")
    return _shim(bin_dir, "node", [sys.executable, str(script)])


def _unusable_node(kind, bin_dir, tmp_path):
    """One genuinely unusable `node` per way discovery can be defeated, plus the fragment its report
    must carry. `hangs` and `reported-binary-hangs` SIMULATE a hang with a short sleep: the caller's
    1s discovery timeout fires well inside it, so the timeout branch runs without waiting on a real
    hang. The sleep is kept brief because `subprocess.run` drains the pipes after killing the
    candidate, and a launcher's own child holds them open past the timeout."""
    stub, relay_py = tmp_path / "stub.py", tmp_path / "relay.py"
    if kind == "nonzero":
        return _fake_node(bin_dir, stub, "raise SystemExit(3)"), "exited 3"
    if kind == "silent":
        return _fake_node(bin_dir, stub, "pass\n"), "with nothing"
    if kind == "unlaunchable":
        bin_dir.mkdir(parents=True, exist_ok=True)
        path = bin_dir / ("node.exe" if os.name == "nt" else "node")
        path.write_bytes(b"this file is not a program\n")
        if os.name != "nt":
            path.chmod(0o755)
        return path, "could not be launched"
    if kind == "hangs":
        return _fake_node(bin_dir, stub, "import time\ntime.sleep(3)\n"), "did not answer"
    # Two-stage: the candidate answers with a second binary, and that one is the broken half -- the
    # shape of a launcher whose helper is only reachable through the PATH the fixture strips.
    if kind == "reported-binary-breaks":
        relay = _shim(bin_dir, "relay", ["loadout-no-such-tool"])
    else:
        relay_py.write_text("import time\ntime.sleep(3)\n", encoding="utf-8")
        relay = _shim(bin_dir, "relay", [sys.executable, str(relay_py)])
    return _fake_node(bin_dir, stub, f"print({str(relay)!r})\n"), "does not run with PATH emptied"


@pytest.mark.parametrize("kind", ["nonzero", "silent", "unlaunchable", "hangs",
                                  "reported-binary-breaks", "reported-binary-hangs"])
def test_a_present_but_unusable_node_fails_instead_of_skipping(tmp_path, monkeypatch, kind):
    """Regression: discovery collapsed every failure into `None`, and `None` meant skip. So a node
    that was present but broken -- a launcher whose helper had moved, a half-finished install --
    silently suppressed the three checks that exist to catch exactly that, and the suite still
    reported a clean run. A rejected candidate must name itself and its reason, and must fail."""
    bin_dir = tmp_path / "bin"
    cand, expected = _unusable_node(kind, bin_dir, tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.chdir(tmp_path)

    found, problem = _discover_node(timeout=1)
    assert found is None, "an unusable candidate must not be handed to the plugin tests"
    assert problem and expected in problem, problem
    assert str(cand).lower() in problem.lower(), f"the report must name the candidate: {problem}"
    with pytest.raises(pytest.fail.Exception):
        _node_or_reason(found, problem)


def test_no_node_at_all_stays_a_capability_skip(tmp_path, monkeypatch):
    """The other half of the contract. Absence has to stay distinguishable from rejection, or the
    repair would just trade a hidden skip for a failure on every host that has no node at all."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.chdir(empty)
    assert _discover_node(timeout=1) == (None, None)
    with pytest.raises(pytest.skip.Exception):
        _node_or_reason(None, None)


def test_dsh_plugin_launches_exactly_the_interpreter_apply_pinned(tmp_path, monkeypatch, dsh_patch, node):
    """End to end on the real artefacts: apply writes the registration on a python3-only PATH, the
    pin is read back out of that file, and the real plugin -- given that entry's config -- spawns
    exactly it. Agreement is proven by the interpreter that actually ran, not by both sides
    computing the same string."""
    real = _native_python(tmp_path / "bin", "python3", monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("LOADOUT_PYTHON", raising=False)
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    apply.apply(tmp_path, "dsh", enforce_dsh=True)

    pinned = json.loads(dsh_text(dsh_patch).split("python: ", 1)[1].splitlines()[0])
    assert Path(pinned).samefile(_reported_executable(real))
    decision, ran, _ = _drive_plugin(node, tmp_path, {"python": pinned})
    assert ran == pinned, "the plugin ran exactly the interpreter the registration pinned"
    assert decision == {"kind": "allow"}, "a gate that ran and allowed is not a fail-closed deny"


def test_dsh_plugin_falls_back_to_python3_when_python_is_absent(tmp_path, monkeypatch, node):
    """A registration written by hand pins nothing, so the plugin resolves candidates itself. With
    only python3 on PATH the old default `python` failed to spawn and denied every tool call."""
    real = _native_python(tmp_path / "bin", "python3", monkeypatch)
    env = {k: v for k, v in os.environ.items() if k != "LOADOUT_PYTHON"}
    env["PATH"] = str(tmp_path / "bin")
    decision, ran, stderr = _drive_plugin(node, tmp_path, {}, env=env)
    assert ran and Path(ran).samefile(_reported_executable(real)), \
        f"plugin did not fall back to python3 (stderr: {stderr[:300]})"
    assert decision == {"kind": "allow"}


def test_trust_codex_gate_drops_its_own_stale_entries_when_the_gate_moves(tmp_path):
    """Regression: trust keys are positional (<event>:<group>:<handler>), but the gate is re-found by
    its "gate.py" command. When another hook is added ahead of it the gate moves, and the entry left at
    the old index silently squats a foreign handler -- reporting our hash for their command, which Codex
    then reads as "modified since last trusted" and refuses to run. Observed live on 2026-09-02: the
    gate's hash sat on `pre_tool_use:0:0` (devteam-codex) and `stop:0:0` (an ADE notify hook)."""
    hooks, cfg = tmp_path / "hooks.json", tmp_path / "config.toml"
    apply.register_codex_gate(hooks)
    assert apply.trust_codex_gate(hooks, cfg) == "trusted"
    first = cfg.read_text(encoding="utf-8")
    gate_pre = f"[hooks.state.'{hooks}:pre_tool_use:0:0']"
    gate_stop = f"[hooks.state.'{hooks}:stop:0:0']"
    assert gate_pre in first and gate_stop in first

    # a foreign hook is added ahead of ours in both events: the gate shifts to index 1
    foreign = {"hooks": [{"type": "command", "command": "devteam-codex hook", "timeout": 10}]}
    data = json.loads(hooks.read_text(encoding="utf-8"))
    for event in ("PreToolUse", "Stop"):
        data["hooks"][event].insert(0, json.loads(json.dumps(foreign)))
    hooks.write_text(json.dumps(data, indent=2), encoding="utf-8")

    assert apply.trust_codex_gate(hooks, cfg) == "trusted"
    text = cfg.read_text(encoding="utf-8")
    assert f"[hooks.state.'{hooks}:pre_tool_use:1:0']" in text, "the gate is trusted at its new index"
    assert f"[hooks.state.'{hooks}:stop:1:0']" in text
    assert gate_pre not in text, "our stale entry must not be left squatting the foreign handler"
    assert gate_stop not in text
    assert text.count("trusted_hash") == 2


def test_trust_codex_gate_prunes_its_entries_when_the_gate_is_gone(tmp_path):
    """Removing the gate from hooks.json must take its trust entries with it, so a later hook that
    lands on the freed index is not pre-judged against our hash. Foreign entries are never touched."""
    hooks, cfg = tmp_path / "hooks.json", tmp_path / "config.toml"
    apply.register_codex_gate(hooks)
    keep = "[hooks.state.'D:/other/hooks.json:stop:0:0']\ntrusted_hash = \"sha256:keep\"\n"
    cfg.write_text(keep, encoding="utf-8")
    apply.trust_codex_gate(hooks, cfg)
    assert cfg.read_text(encoding="utf-8").count("trusted_hash") == 3

    hooks.write_text(json.dumps({"hooks": {}}, indent=2), encoding="utf-8")
    apply.trust_codex_gate(hooks, cfg)
    text = cfg.read_text(encoding="utf-8")
    assert "sha256:keep" in text, "another file's entry is not ours to remove"
    assert f"{hooks}:pre_tool_use" not in text and f"{hooks}:stop" not in text
    assert text.count("trusted_hash") == 1


def test_codex_gate_is_opt_in(tmp_path, codex_hooks):
    """Registering the gate in ~/.codex/hooks.json crashed the Codex desktop app-server on
    0.152.1 (hard abort ~20s after every launch, no respawn), with a schema-correct nested
    entry just as much as with the malformed one. Until that is understood, --host codex
    wires the prose section only; the gate needs an explicit --enforce-codex."""
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "codex")
    assert set(res) == {"AGENTS.md"}
    assert not codex_hooks.exists(), "the user's hooks.json is not touched without an opt-in"
    res = apply.apply(tmp_path, "codex", enforce_codex=True)
    assert res["~/.codex/hooks.json"].startswith("created" + CODEX_NOTE)
    assert codex_hooks.is_file()
    # --no-enforce still wins over the opt-in
    assert "~/.codex/hooks.json" not in apply.apply(tmp_path, "codex", enforce=False, enforce_codex=True)


def test_missing_gate_keeps_prose_and_skips_registration(tmp_path, monkeypatch):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    monkeypatch.setattr(apply, "GATE", tmp_path / "missing-gate.py")
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "claude-code")
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "CLAUDE.md").is_file()
    assert not (tmp_path / ".claude/settings.local.json").exists()
    assert "gate.py" in str(ei.value).lower()
    assert ei.value.results["AGENTS.md"] == "created"
    assert ei.value.results["CLAUDE.md"] == "created with @AGENTS.md import"


def test_missing_gate_main_exits_2_after_prose(tmp_path, monkeypatch, capsys):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    monkeypatch.setattr(apply, "GATE", tmp_path / "missing-gate.py")
    monkeypatch.setattr(sys, "argv", ["apply.py", str(tmp_path), "--host", "claude-code"])
    with pytest.raises(SystemExit) as ei:
        apply.main()
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "- AGENTS.md: created" in out
    assert "- CLAUDE.md: created with @AGENTS.md import" in out
    assert "enforcement: skipped:" in out
    assert "gate.py" in out.lower()
    assert not (tmp_path / ".claude/settings.local.json").exists()


def test_missing_gate_codex_keeps_prose_and_does_not_write_hooks(tmp_path, monkeypatch):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    monkeypatch.setattr(apply, "GATE_CODEX", tmp_path / "missing-gate_codex.py")
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert (tmp_path / "AGENTS.md").is_file()
    assert "gate_codex.py" in str(ei.value).lower()
    assert not (tmp_path / "codex-home" / "hooks.json").exists()
    assert ei.value.results["AGENTS.md"] == "created"


NEEDS_TOMLLIB = pytest.mark.skipif(
    apply.tomllib is None,
    reason="malformed-TOML detection needs the stdlib tomllib (3.11+); documented boundary")


def _codex_config(tmp_path, monkeypatch, raw):
    """An isolated CODEX_HOME config.toml holding `raw`, with a LOADOUT.md ready to apply."""
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    cfg = tmp_path / "codex-home" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(raw)
    monkeypatch.setattr(apply, "CODEX_CONFIG", cfg)
    return cfg


def test_unreadable_codex_trust_config_fails_before_any_write(tmp_path, monkeypatch):
    cfg = _codex_config(tmp_path, monkeypatch, b"\xff\xfe not utf-8")
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "config.toml" in str(ei.value).lower()
    assert ei.value.results == {}
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "codex-home" / "hooks.json").exists()
    assert cfg.read_bytes() == b"\xff\xfe not utf-8"


@NEEDS_TOMLLIB
def test_malformed_codex_trust_config_fails_before_any_write(tmp_path, monkeypatch):
    raw = b'[model]\nname = "gpt"\n\n[broken\n'
    cfg = _codex_config(tmp_path, monkeypatch, raw)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "config.toml" in str(ei.value).lower()
    assert ei.value.results == {}
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "codex-home" / "hooks.json").exists()
    assert cfg.read_bytes() == raw  # the foreign [model] table keeps its bytes


@NEEDS_TOMLLIB
def test_malformed_codex_trust_config_exits_2_with_an_actionable_message(tmp_path, monkeypatch, capsys):
    _codex_config(tmp_path, monkeypatch, b"[broken\n")
    monkeypatch.setattr(sys, "argv", ["apply.py", str(tmp_path), "--host", "codex", "--enforce-codex"])
    with pytest.raises(SystemExit) as ei:
        apply.main()
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "enforcement: skipped" in out and "config.toml" in out and "--enforce-codex" in out


def test_codex_config_problem_without_tomllib_rejects_every_existing_config(tmp_path, monkeypatch):
    """The 3.9/3.10 boundary: no stdlib TOML parser, so no existing config can be validated there.

    Forced `tomllib = None` on this interpreter, not a real 3.9/3.10 run — see the disclosure in
    docs/host-capability-matrix.md."""
    monkeypatch.setattr(apply, "tomllib", None)
    unreadable = tmp_path / "bad.toml"
    unreadable.write_bytes(b"\xff\xfe not utf-8")
    assert "not readable" in apply.codex_config_problem(unreadable)
    for name, raw in (("broken.toml", b"[broken\n"), ("valid.toml", b'[model]\nname = "gpt"\n')):
        p = tmp_path / name
        p.write_bytes(raw)
        assert "no stdlib TOML parser" in apply.codex_config_problem(p)  # valid-looking too
    assert apply.codex_config_problem(tmp_path / "absent.toml") is None  # nothing to validate


@NEEDS_TOMLLIB
def test_codex_config_problem_passes_a_readable_valid_or_absent_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[model]\nname = "gpt"\n', encoding="utf-8")
    assert apply.codex_config_problem(cfg) is None
    assert apply.codex_config_problem(tmp_path / "absent.toml") is None


def _codex_sentinels(tmp_path):
    """Pre-existing bytes at the actual patched targets, so a rejection can be proved non-mutating."""
    (tmp_path / "AGENTS.md").write_bytes(b"# kept prose\n")
    hooks = tmp_path / "codex-home" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_bytes(b'{"hooks": {}}')
    return hooks


@pytest.mark.parametrize("raw", [b'[model]\nname = "gpt"\n', b"[broken\n"])
def test_codex_enforcement_without_a_toml_parser_fails_before_any_write(tmp_path, monkeypatch, raw):
    """Forced no-parser: valid-looking and malformed existing configs are both rejected, unmutated."""
    cfg = _codex_config(tmp_path, monkeypatch, raw)
    hooks = _codex_sentinels(tmp_path)
    monkeypatch.setattr(apply, "tomllib", None)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "tomllib" in str(ei.value) and "--enforce-codex" in str(ei.value)
    assert ei.value.results == {}
    assert cfg.read_bytes() == raw
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# kept prose\n"
    assert hooks.read_bytes() == b'{"hooks": {}}'


def test_codex_enforcement_without_a_toml_parser_exits_2(tmp_path, monkeypatch, capsys):
    _codex_config(tmp_path, monkeypatch, b'[model]\nname = "gpt"\n')
    monkeypatch.setattr(apply, "tomllib", None)
    monkeypatch.setattr(sys, "argv", ["apply.py", str(tmp_path), "--host", "codex", "--enforce-codex"])
    with pytest.raises(SystemExit) as ei:
        apply.main()
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "enforcement: skipped" in out and "3.11+" in out
    assert not (tmp_path / "AGENTS.md").exists()


def test_prose_only_codex_is_unaffected_without_a_toml_parser(tmp_path, monkeypatch):
    """The core 3.9/3.10 path must not be denied: no --enforce-codex, no validation, prose written."""
    _codex_config(tmp_path, monkeypatch, b"[broken\n")
    monkeypatch.setattr(apply, "tomllib", None)
    res = apply.apply(tmp_path, "codex")
    assert res == {"AGENTS.md": "created"}
    assert not (tmp_path / "codex-home" / "hooks.json").exists()


def test_absent_codex_config_still_registers_without_a_toml_parser(tmp_path, monkeypatch, codex_hooks):
    """Genuine absence has nothing to validate; trust_codex_gate creates a fresh [hooks.state] file.

    The generated file's own TOML validity is not asserted here and remains unverified this slice."""
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    monkeypatch.setattr(apply, "tomllib", None)
    res = apply.apply(tmp_path, "codex", enforce_codex=True)
    assert CODEX_NOTE in res["~/.codex/hooks.json"]
    assert codex_hooks.is_file() and (tmp_path / "codex-home" / "config.toml").is_file()


def test_directory_codex_trust_config_fails_before_any_write(tmp_path, monkeypatch):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    cfg = tmp_path / "codex-home" / "config.toml"
    cfg.mkdir(parents=True)
    monkeypatch.setattr(apply, "CODEX_CONFIG", cfg)
    hooks = _codex_sentinels(tmp_path)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "not a regular file" in str(ei.value)
    assert ei.value.results == {}
    assert list(cfg.iterdir()) == []  # the directory is neither read nor written into
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# kept prose\n"
    assert hooks.read_bytes() == b'{"hooks": {}}'


@pytest.mark.parametrize("target", ["dangling", "directory"])
def test_symlinked_codex_trust_config_that_is_not_a_regular_file_fails_before_any_write(
        tmp_path, monkeypatch, target):
    """A dangling link is an existing target, not an absence — exists() alone would miss it."""
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    home = tmp_path / "codex-home"
    home.mkdir()
    dest = home / "dest"
    if target == "directory":
        dest.mkdir()
    cfg = home / "config.toml"
    try:
        cfg.symlink_to(dest, target_is_directory=(target == "directory"))
    except (OSError, NotImplementedError) as e:  # Windows needs privilege/developer mode
        pytest.skip(f"symlinks unavailable on this OS/account: {e}")
    monkeypatch.setattr(apply, "CODEX_CONFIG", cfg)
    hooks = _codex_sentinels(tmp_path)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "not a regular file" in str(ei.value)
    assert ei.value.results == {}
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# kept prose\n"
    assert hooks.read_bytes() == b'{"hooks": {}}'


@NEEDS_TOMLLIB
def test_symlink_to_a_regular_codex_trust_config_stays_supported(tmp_path, monkeypatch):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "real.toml").write_text('[model]\nname = "gpt"\n', encoding="utf-8")
    cfg = home / "config.toml"
    try:
        cfg.symlink_to(home / "real.toml")
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlinks unavailable on this OS/account: {e}")
    monkeypatch.setattr(apply, "CODEX_CONFIG", cfg)
    assert apply.codex_config_problem(cfg) is None
    res = apply.apply(tmp_path, "codex", enforce_codex=True)
    assert CODEX_NOTE in res["~/.codex/hooks.json"]


def _deny_inspection(monkeypatch, target):
    """Refuse os.stat/os.lstat for `target` only: a mock, not a real permission-denied file.

    What that refusal reaches depends on the interpreter. On 3.13.15 Path.is_file/exists go through
    Path.stat() -> os.stat and the PermissionError propagates; on 3.14.6 they answer from os.path's
    nt._path_* accelerators, which never consult os.stat -- so under this mock they keep reading the
    real file, and is_file/exists/is_symlink were observed as True/True/False. That is this mock's
    result, not the accelerators failing to inspect a path: their documented False-rather-than-raise
    answer is about an inspection that really is denied to them, which is not what happens here and
    is not exercised natively anywhere in this suite. So this denies the direct os.lstat probe on
    both interpreters, but on 3.14 it does not reach the predicates at all -- forcing that reading is
    _simulate_all_false_predicates' job. Both calls are patched because stat follows symlinks and
    lstat does not."""
    real_stat, real_lstat = os.stat, os.lstat

    def denied(real):
        def probe(p, *a, **kw):
            if str(p) == str(target):
                raise PermissionError(13, "permission denied")
            return real(p, *a, **kw)
        return probe

    monkeypatch.setattr(os, "stat", denied(real_stat))
    monkeypatch.setattr(os, "lstat", denied(real_lstat))


def _simulate_all_false_predicates(monkeypatch, target):
    """Force is_file/exists/is_symlink to False for `target` only -- a simulation of a branch.

    A path whose inspection the OS refuses can read as all-false instead of raising: os.path's
    isfile/exists/islink (the nt._path_* builtins on both 3.13.15 and 3.14.6) are documented to
    answer False rather than raise for a path they cannot inspect, and 3.14's pathlib answers these
    predicates from them. Producing that natively needs a real permission-denied file on such an
    interpreter, which these tests do not have, so
    the reading is forced for this one path while every other path answers for real. This models the
    capability, not a Python version, and is evidence about no interpreter it was not run on."""
    for name in ("is_file", "exists", "is_symlink"):
        real = getattr(Path, name)

        def false_for_target(self, *a, _real=real, **kw):
            return False if self == target else _real(self, *a, **kw)

        monkeypatch.setattr(Path, name, false_for_target)


def _file_parent_config(tmp_path, monkeypatch):
    """A config.toml path whose parent is a regular file: nothing can ever be created there.

    Windows raises FileNotFoundError for this exactly as it does for a genuine absence, so the
    error is not evidence of a creatable path -- only the parent's own shape is."""
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    parent = tmp_path / "not-a-dir"
    parent.write_bytes(b"# a regular file, not a directory\n")
    cfg = parent / "config.toml"
    monkeypatch.setattr(apply, "CODEX_CONFIG", cfg)
    return cfg


def test_codex_trust_config_under_a_file_parent_fails_before_any_write(tmp_path, monkeypatch):
    """An impossible destination is not a usable absence: refuse before AGENTS.md or hooks.json."""
    cfg = _file_parent_config(tmp_path, monkeypatch)
    hooks = _codex_sentinels(tmp_path)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "config.toml" in str(ei.value) and "--enforce-codex" in str(ei.value)
    assert ei.value.results == {}
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# kept prose\n"
    assert hooks.read_bytes() == b'{"hooks": {}}'
    assert cfg.parent.read_bytes() == b"# a regular file, not a directory\n"


def test_codex_trust_config_under_a_file_parent_exits_2(tmp_path, monkeypatch, capsys):
    _file_parent_config(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["apply.py", str(tmp_path), "--host", "codex", "--enforce-codex"])
    with pytest.raises(SystemExit) as ei:
        apply.main()
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "enforcement: skipped" in out and "config.toml" in out and "--enforce-codex" in out
    assert not (tmp_path / "AGENTS.md").exists()


def test_codex_trust_config_that_cannot_be_inspected_fails_before_any_write(tmp_path, monkeypatch):
    """A refused os.lstat is classified as uninspectable, not as a creatable absence.

    Direct-denial coverage: it pins how the probe's own error is classified. It says nothing about
    the predicates, which on 3.13.15 raise here and on 3.14.6 never see the refusal -- the dangerous
    all-false reading is covered by the simulated control below."""
    cfg = _codex_config(tmp_path, monkeypatch, b'[model]\nname = "gpt"\n')
    hooks = _codex_sentinels(tmp_path)
    _deny_inspection(monkeypatch, cfg)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "cannot be inspected" in str(ei.value) and "--enforce-codex" in str(ei.value)
    assert ei.value.results == {}
    assert cfg.read_bytes() == b'[model]\nname = "gpt"\n'
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# kept prose\n"
    assert hooks.read_bytes() == b'{"hooks": {}}'


def test_codex_trust_config_reading_as_absent_under_denial_fails_before_any_write(tmp_path, monkeypatch):
    """The dangerous branch: inspection refused *and* the predicates reporting False for that path.

    Simulated, not a native run -- see _simulate_all_false_predicates. This is the silent case at
    7b11934: the preflight returned None, apply reported success, and trust_codex_gate's own
    is_file() read False too, so it replaced config.toml with a bare [hooks.state] and the foreign
    [model] table was gone at exit 0. Byte-verified against that source on 3.13.15 and 3.14.6."""
    cfg = _codex_config(tmp_path, monkeypatch, b'[model]\nname = "gpt"\n')
    hooks = _codex_sentinels(tmp_path)
    _deny_inspection(monkeypatch, cfg)
    _simulate_all_false_predicates(monkeypatch, cfg)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "cannot be inspected" in str(ei.value) and "--enforce-codex" in str(ei.value)
    assert ei.value.results == {}
    assert cfg.read_bytes() == b'[model]\nname = "gpt"\n'  # the config the base replaced
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# kept prose\n"
    assert hooks.read_bytes() == b'{"hooks": {}}'


def test_codex_config_problem_separates_a_file_parent_from_a_genuine_absence(tmp_path):
    """The unit boundary: same FileNotFoundError on Windows, opposite verdicts."""
    parent = tmp_path / "not-a-dir"
    parent.write_bytes(b"x")
    assert "not a directory" in apply.codex_config_problem(parent / "config.toml")
    assert apply.codex_config_problem(tmp_path / "deep" / "nested" / "config.toml") is None


@NEEDS_TOMLLIB
def test_absent_codex_trust_config_under_absent_parents_is_still_created(tmp_path, monkeypatch):
    """Positive control: a genuinely missing config below missing directories is created, not refused."""
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    cfg = tmp_path / "deep" / "nested" / "codex-home" / "config.toml"
    monkeypatch.setattr(apply, "CODEX_CONFIG", cfg)
    assert apply.codex_config_problem(cfg) is None
    res = apply.apply(tmp_path, "codex", enforce_codex=True)
    assert CODEX_NOTE in res["~/.codex/hooks.json"]
    assert "[hooks.state" in cfg.read_text(encoding="utf-8")


def test_codex_trust_config_read_error_fails_before_any_write(tmp_path, monkeypatch):
    """A read that fails with OSError, not just undecodable bytes; mocked, so no real file is chmod-ed."""
    cfg = _codex_config(tmp_path, monkeypatch, b'[model]\nname = "gpt"\n')
    hooks = _codex_sentinels(tmp_path)
    real_read_text = Path.read_text

    def boom(self, *a, **kw):
        if self == cfg:
            raise PermissionError(13, "permission denied")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert "not readable" in str(ei.value)
    assert ei.value.results == {}
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# kept prose\n"
    assert hooks.read_bytes() == b'{"hooks": {}}'


def test_missing_dsh_python_adapter_keeps_prose_and_does_not_write_patch(tmp_path, dsh_patch, monkeypatch):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    monkeypatch.setattr(apply, "GATE_DSH", tmp_path / "missing-gate_dsh.py")
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "deepseek", enforce_dsh=True)
    assert (tmp_path / "AGENTS.md").is_file()
    assert "gate_dsh.py" in str(ei.value).lower()
    assert not dsh_patch.exists()


def test_codex_trust_failure_after_hooks_reports_partial_write(tmp_path, monkeypatch):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")

    def boom(*a, **k):
        raise OSError("trust denied")

    monkeypatch.setattr(apply, "trust_codex_gate", boom)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "codex", enforce_codex=True)
    assert (tmp_path / "codex-home" / "hooks.json").is_file()
    assert ei.value.results["~/.codex/hooks.json"].startswith("created")
    assert "trust denied" in str(ei.value)


def test_unusable_dsh_python_keeps_prose_and_does_not_write_patch(tmp_path, dsh_patch, monkeypatch):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    monkeypatch.setattr(apply, "_dsh_python", lambda: None)
    with pytest.raises(apply.EnforcementFailed) as ei:
        apply.apply(tmp_path, "deepseek", enforce_dsh=True)
    assert "python" in str(ei.value).lower()
    assert (tmp_path / "AGENTS.md").is_file()
    assert not dsh_patch.exists()


def test_missing_dsh_plugin_keeps_prose_and_does_not_write_patch(tmp_path, dsh_patch, monkeypatch, capsys):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    monkeypatch.setattr(apply, "DSH_PLUGIN", tmp_path / "missing.mjs")
    monkeypatch.setattr(sys, "argv", ["apply.py", str(tmp_path), "--host", "dsh", "--enforce-dsh"])
    with pytest.raises(SystemExit) as ei:
        apply.main()
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "- AGENTS.md: created" in out
    assert "enforcement: skipped:" in out
    assert not dsh_patch.exists()
    assert (tmp_path / "AGENTS.md").is_file()


def test_cli_prints_enforcement_registered_or_skipped(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "apply.py"), str(tmp_path), "--host", "claude-code"],
        capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "enforcement: registered — takes effect next session" in r.stdout
    dest = tmp_path / "unknown-host"
    dest.mkdir()
    (dest / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "apply.py"), str(dest), "--host", "unknown"],
        capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "enforcement: skipped this invocation" in r.stdout
    assert "prose-only" not in r.stdout
    assert "disabled" not in r.stdout.lower()
    assert "settings.local.json" not in r.stdout


def test_cli_discloses_the_codex_recovery_limitation_only_when_it_actually_registers(tmp_path):
    """Opt-in-time disclosure. `--enforce-codex` buys a known unresolved limitation -- a session
    that already edited cannot clear a stage from inside itself -- and the operator should hear it
    when they opt in, not from the first denial. The paths that register nothing must stay silent,
    or the note stops carrying information."""
    def run(dest, *args, codex_home=None):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
        env = os.environ.copy()
        if codex_home is not None:
            env["CODEX_HOME"] = str(codex_home)
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "apply.py"), str(dest), *args],
                           capture_output=True, encoding="utf-8", env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout

    mark = "recovery is the operator hatch: LOADOUT_ENFORCE=0"
    home = tmp_path / "codex-home"
    out = run(tmp_path / "on", "--host", "codex", "--enforce-codex", codex_home=home)
    assert "enforcement: registered" in out
    assert mark in out, out
    assert (home / "hooks.json").is_file(), "the note must follow a registration that happened"

    # same host, no flag: registration is skipped this invocation, so there is nothing to disclose
    out = run(tmp_path / "off", "--host", "codex", codex_home=tmp_path / "codex-home-off")
    assert "skipped this invocation" in out and mark not in out, out
    assert not (tmp_path / "codex-home-off").exists()

    # and it is Codex-specific: a Claude Code registration must not carry it
    out = run(tmp_path / "cc", "--host", "claude-code")
    assert "enforcement: registered" in out and mark not in out, out


def test_cli_preserves_existing_dsh_registration_and_says_so(tmp_path, dsh_patch):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    dsh_patch.parent.mkdir(parents=True, exist_ok=True)
    dsh_patch.write_text(apply.dsh_entry(), encoding="utf-8")
    before = dsh_patch.read_bytes()
    env = os.environ.copy()
    env["DSH_HOME"] = str(dsh_patch.parent)
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "apply.py"), str(tmp_path), "--host", "dsh"],
        capture_output=True, encoding="utf-8", env=env)
    assert r.returncode == 0, r.stderr
    assert dsh_patch.read_bytes() == before
    assert "skipped this invocation" in r.stdout
    assert "existing registration preserved" in r.stdout
    assert "prose-only" not in r.stdout
    assert "disabled" not in r.stdout.lower()
