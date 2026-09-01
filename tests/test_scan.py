"""Tests for scripts/scan.py against a synthetic fixture HOME.

Runs the scanner as a subprocess with HOME/USERPROFILE pointed at a temp dir,
so no real machine state is read or written.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN = REPO / "scripts" / "scan.py"


def run_scan(home, args):
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home),
               LOADOUT_HOST="claude-code")
    return subprocess.run([sys.executable, str(SCAN), *args],
                          capture_output=True, encoding="utf-8", env=env)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_fixture(tmp_path):
    h = tmp_path / "home"
    write(h / ".claude/skills/plainskill/SKILL.md",
          "---\nname: plainskill\ndescription: A plain description\n---\n")
    write(h / ".claude/skills/foldedskill/SKILL.md",
          "---\nname: foldedskill\ndescription: >-\n  Folded description text\n---\n")
    write(h / ".claude/skills/unicodeskill/SKILL.md",
          "---\ndescription: arrows → and dashes — survive\n---\n")
    write(h / ".claude/plugins/installed_plugins.json",
          json.dumps({"version": 2, "plugins": {"alpha@mkt": [{}], "beta@mkt": [{}]}}))
    write(h / ".claude.json", json.dumps({"mcpServers": {"srv-a": {}, "srv-b": {}}}))
    write(h / ".codex/skills/plainskill/SKILL.md", "---\ndescription: on codex too\n---\n")
    write(h / ".codex/config.toml", '[mcp_servers.toml-srv]\ncommand = "x"\n')
    write(h / ".agents/skills/plainskill/SKILL.md", "---\ndescription: on deepseek too\n---\n")
    write(h / ".grok/installed-plugins/registry.json",
          json.dumps({"version": 1, "repos": {"r": {"plugins": {"gplug": {"version": "1"}}}}}))
    write(h / ".gemini/extensions/realext/gemini-extension.json", "{}")
    write(h / ".gemini/extensions/extension-enablement.json", "{}")
    proj = tmp_path / "proj"
    write(proj / "pyproject.toml", "[project]\nname='x'\n")
    write(proj / "AGENTS.md", "# agents\n")
    write(proj / ".mcp.json", json.dumps({"mcpServers": {"proj-srv": {}}}))
    return h, proj


def test_markdown_inventory(tmp_path):
    h, proj = make_fixture(tmp_path)
    r = run_scan(h, [str(proj)])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "Running inside: **claude-code**" in out
    # descriptions: plain, YAML block scalar resolved, unicode survives
    assert "**plainskill** — A plain description" in out
    assert "Folded description text" in out
    assert "— >-" not in out
    assert "arrows → and dashes" in out
    # plugins from manifests, not dir listings
    assert "**alpha**" in out and "**beta**" in out
    # non-current hosts render names-only lines
    assert "plugins: gplug" in out
    # infra files filtered from generic extension scan
    assert "realext" in out
    assert "extension-enablement" not in out
    # MCP from json and codex toml
    assert "srv-a" in out and "toml-srv" in out
    # cross-host: plainskill is in claude, codex, and .agents
    assert "universal (in every host): 1" in out
    # project scope
    assert "manifests: pyproject.toml" in out
    assert "AGENTS.md" in out
    assert "proj-srv" in out


def test_json_mode(tmp_path):
    h, proj = make_fixture(tmp_path)
    r = run_scan(h, ["--json", str(proj)])
    assert r.returncode == 0, r.stderr
    inv = json.loads(r.stdout)
    assert inv["running_in"] == "claude-code"
    claude = inv["hosts"]["claude-code"]
    assert [p["name"] for p in claude["assets"]["plugins"]] == ["alpha", "beta"]
    assert inv["cross_host"]["universal"] == ["plainskill"]
    assert sorted(inv["cross_host"]["only_here"]) == ["foldedskill", "unicodeskill"]
    assert inv["project"]["manifests"] == ["pyproject.toml"]


def test_self_install_idempotent(tmp_path):
    h, _ = make_fixture(tmp_path)
    for _ in range(2):  # second run must not fail
        r = run_scan(h, ["--self-install"])
        assert r.returncode == 0, r.stderr
    for host_dir in (".claude", ".codex", ".agents", ".gemini", ".grok"):
        dest = h / host_dir / "skills/loadout"
        assert (dest / "SKILL.md").is_file(), host_dir
        assert (dest / "scripts/scan.py").is_file(), host_dir
    # hosts absent from the fixture are skipped, not created
    assert not (h / ".qwen").exists()
    assert "skipped" in r.stdout
