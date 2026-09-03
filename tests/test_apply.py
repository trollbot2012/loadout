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
CODEX_NOTE = "; trust granted in config.toml (Codex loads hooks at the next session"


@pytest.fixture(autouse=True)
def codex_hooks(tmp_path, monkeypatch):
    """Never touch the real ~/.codex/hooks.json from tests."""
    path = tmp_path / "codex-home" / "hooks.json"
    monkeypatch.setattr(apply, "CODEX_HOOKS", path, raising=False)
    monkeypatch.setattr(apply, "CODEX_CONFIG", tmp_path / "codex-home" / "config.toml", raising=False)
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
