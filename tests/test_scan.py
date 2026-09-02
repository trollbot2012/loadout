"""Tests for scripts/scan.py against a synthetic fixture HOME.

Runs the scanner as a subprocess with HOME/USERPROFILE pointed at a temp dir and every
host marker / home override scrubbed, so no real machine state is read or written.
Assertions verify behaviour, not machine-specific counts.
"""
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN = REPO / "scripts" / "scan.py"
sys.path.insert(0, str(REPO / "scripts"))
import scan  # noqa: E402  (constants only; the scanner itself runs as a subprocess)

SCRUB = ["LOADOUT_HOST", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME", "VIBE_HOME",
         "HERMES_HOME", "DSH_HOME", "XDG_CONFIG_HOME"] + [v for v, _ in scan.ENV_MARKERS]


def run_scan(home, args, env=None, host="claude-code"):
    e = {k: v for k, v in os.environ.items() if k not in SCRUB}
    e.update(USERPROFILE=str(home), HOME=str(home))
    if host:
        e["LOADOUT_HOST"] = host
    e.update(env or {})
    return subprocess.run([sys.executable, str(SCAN), *args], capture_output=True,
                          encoding="utf-8", env=e)


def scan_json(home, proj, extra=()):
    r = run_scan(home, ["--json", *extra, str(proj)])
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_fixture(tmp_path):
    h = tmp_path / "home"
    proj = tmp_path / "proj"
    write(proj / "pyproject.toml", "[project]\nname='x'\n")
    write(proj / "AGENTS.md", "# agents\n")
    write(proj / ".mcp.json", json.dumps({"mcpServers": {"proj-srv": {}}}))

    # --- claude-code: user skills with every description shape, plus disabled state
    write(h / ".claude/skills/plainskill/SKILL.md",
          "---\nname: plainskill\ndescription: A plain description\n---\n")
    write(h / ".claude/skills/foldedskill/SKILL.md",
          "---\nname: foldedskill\ndescription: >-\n  Folded description text\n  second folded line\n---\n")
    write(h / ".claude/skills/multiline/SKILL.md",
          "---\nname: multiline\ndescription: First line of plain scalar\n"
          "  continues with the Use when trigger\nlicense: MIT\n---\n")
    write(h / ".claude/skills/longskill/SKILL.md",
          "---\ndescription: Does one thing well. Use when the user asks for the thing. "
          "Not for the other thing.\n---\n")
    write(h / ".claude/skills/unicodeskill/SKILL.md",
          "---\ndescription: arrows → and dashes — survive\n---\n")
    write(h / ".claude/skills/bigmeta/SKILL.md",
          "---\nname: bigmeta\nmetadata:\n" + "".join(f"  k{i}: {'x' * 60}\n" for i in range(40))
          + "description: after a big metadata block\n---\n")
    write(h / ".claude/skills/offskill/SKILL.md", "---\ndescription: turned off\n---\n")
    # plugins: alpha enabled (skills+agents+hooks+mcp), beta disabled
    cache_a = h / ".claude/plugins/cache/mkt/alpha/1.0.0"
    write(cache_a / "skills/askill/SKILL.md", "---\ndescription: plugin skill alpha\n---\n")
    write(cache_a / "agents/aagent.md", "---\ndescription: plugin agent\n---\n")
    write(cache_a / "hooks/hooks.json", json.dumps({"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": "run-hook.cmd session-start"}]}]}}))
    write(cache_a / ".mcp.json", json.dumps({"mcpServers": {"alpha-srv": {}}}))
    cache_b = h / ".claude/plugins/cache/mkt/beta/2.0.0"
    write(cache_b / "skills/bskill/SKILL.md", "---\ndescription: plugin skill beta\n---\n")
    write(cache_b / ".mcp.json", json.dumps({"beta-bare": {"command": "x"}}))  # bare map, no wrapper
    write(cache_b / ".claude-plugin/plugin.json", json.dumps({"name": "beta", "mcpServers": {"beta-inline": {}}}))
    write(h / ".claude/plugins/installed_plugins.json", json.dumps({"version": 2, "plugins": {
        "alpha@mkt": [{"installPath": str(cache_a), "version": "1.0.0"}],
        "beta@mkt": [{"installPath": str(cache_b), "version": "2.0.0"}]}}))
    write(h / ".claude/settings.json", json.dumps({
        "enabledPlugins": {"alpha@mkt": True, "beta@mkt": False},
        "skillOverrides": {"offskill": "off", "plainskill": "user-invocable-only"},
        "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python trim.py"}]}],
                  "SessionStart": [{"hooks": [{"type": "command", "command": "gh-axi"},
                                              {"type": "command", "command": "lavish-axi"}]}]},
        "disabledMcpServers": ["dead-srv"]}))
    write(h / ".claude/hooks/trim.py", "")  # script storage, not a registration
    write(h / ".claude.json", json.dumps({
        "mcpServers": {"srv-a": {}, "srv-b": {}, "dead-srv": {}},
        "projects": {"/some/other": {"mcpServers": {"nested-srv": {}}},
                     str(proj.resolve()): {"mcpServers": {"proj-nested": {}},
                                           "disabledMcpjsonServers": ["proj-nested"]}}}))
    # --- codex: legacy skills dir, hooks.json, toml MCP
    write(h / ".codex/skills/plainskill/SKILL.md", "---\ndescription: on codex too\n---\n")
    write(h / ".codex/config.toml", '[mcp_servers.toml-srv]\ncommand = "x"\n')
    write(h / ".codex/hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "node stop.mjs"}]}]}}))
    # --- cursor: flat hooks.json, marketplace plugins
    write(h / ".cursor/hooks.json", json.dumps({"version": 1, "hooks": {
        "beforeShellExecution": [{"command": "cursor-hook.sh"}]}}))
    (h / ".cursor/plugins/marketplaces/mkt/owner/cplug").mkdir(parents=True)
    # --- gemini: hooks + MCP in settings.json, infra file in extensions
    write(h / ".gemini/settings.json", json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "gemini-hook.sh"}]}]},
        "mcpServers": {"gem-srv": {}}}))
    write(h / ".gemini/extensions/realext/gemini-extension.json", "{}")
    write(h / ".gemini/extensions/extension-enablement.json", "{}")
    # --- grok: registry plugins + toml MCP
    write(h / ".grok/installed-plugins/registry.json",
          json.dumps({"version": 1, "repos": {"r": {"plugins": {"gplug": {"version": "1"}}}}}))
    write(h / ".grok/config.toml", '[mcp_servers.grok-srv]\ncommand = "x"\n')
    # --- shared pool and roots outside the table
    write(h / ".agents/skills/plainskill/SKILL.md", "---\ndescription: shared copy\n---\n")
    write(h / ".agents/skills/poolonly/SKILL.md", "---\ndescription: only in the shared pool\n---\n")
    write(h / ".dsh/skills/dshskill/SKILL.md", "---\ndescription: a deepseek harness skill\n---\n")
    write(h / ".dsh/skills/plainskill/SKILL.md", "---\ndescription: on deepseek too\n---\n")
    write(h / ".zcode/skills/plainskill/SKILL.md", "---\ndescription: on zcode too\n---\n")
    write(h / ".someagent/skills/plainskill/SKILL.md", "---\ndescription: elsewhere\n---\n")
    write(h / ".pi/agent/skills/plainskill/SKILL.md", "---\ndescription: nested root\n---\n")
    return h, proj


# ---------------------------------------------------------------- layout & parsing

def test_project_section_first_and_detection_note(tmp_path):
    h, proj = make_fixture(tmp_path)
    r = run_scan(h, [str(proj)])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.index("## Project:") < out.index("Running inside")
    assert "Running inside: **claude-code** (LOADOUT_HOST override)" in out
    assert "manifests: pyproject.toml" in out and "AGENTS.md" in out and "proj-srv" in out
    assert "- git: NOT a git repository" in out
    (proj / ".git").mkdir()
    assert "- git: repository present" in run_scan(h, [str(proj)]).stdout


def test_multiline_descriptions_are_complete(tmp_path):
    h, proj = make_fixture(tmp_path)
    out = run_scan(h, [str(proj)]).stdout
    assert "**foldedskill** — Folded description text second folded line" in out
    assert "— >-" not in out
    assert "First line of plain scalar continues with the Use when trigger" in out
    assert "**bigmeta** — after a big metadata block" in out
    assert "arrows → and dashes" in out


def test_plugin_skills_discovered_with_plugin_state(tmp_path):
    h, proj = make_fixture(tmp_path)
    inv = scan_json(h, proj)
    assets = inv["hosts"]["claude-code"]["assets"]
    pskills = {e["name"]: e for e in assets["plugin-skills"]}
    assert all(":" in n for n in pskills), "plugin skills are namespaced plugin:skill"
    assert pskills["alpha:askill"]["desc"] == "plugin skill alpha" and "status" not in pskills["alpha:askill"]
    assert pskills["beta:bskill"]["status"].startswith("off")
    plugins = {e["name"]: e for e in assets["plugins"]}
    assert "status" not in plugins["alpha"] and plugins["beta"]["status"] == "off"
    assert plugins["alpha"]["desc"] == "1.0.0"
    assert any(e["name"] == "alpha:aagent" for e in assets["agents"])
    out = run_scan(h, [str(proj)]).stdout
    assert "**beta:bskill** (off (plugin disabled))" in out


def test_registered_hooks_come_from_settings_not_script_dirs(tmp_path):
    h, proj = make_fixture(tmp_path)
    inv = scan_json(h, proj)
    hooks = inv["hosts"]["claude-code"]["assets"]["hooks"]
    names = [x["name"] for x in hooks]
    assert names.count("SessionStart") == 3  # two from settings, one from plugin alpha
    post = next(x for x in hooks if x["name"] == "PostToolUse")
    assert "[Bash] python trim.py" in post["desc"] and "settings.json" in post["desc"]
    assert any("(plugin alpha)" in x["desc"] for x in hooks)
    assert not any(x["name"] == "trim" for x in hooks), "hooks/ dir scripts are not registrations"
    assert inv["hosts"]["codex"]["assets"]["hooks"][0]["name"] == "Stop"
    assert "node stop.mjs" in inv["hosts"]["codex"]["assets"]["hooks"][0]["desc"]
    assert inv["hosts"]["cursor"]["assets"]["hooks"][0]["name"] == "beforeShellExecution"
    assert inv["hosts"]["gemini"]["assets"]["hooks"][0]["name"] == "SessionStart"


def test_disabled_state_marked(tmp_path):
    h, proj = make_fixture(tmp_path)
    out = run_scan(h, [str(proj)]).stdout
    assert "**offskill** (off)" in out
    assert "**plainskill** (user-invocable-only)" in out
    assert "dead-srv (off)" in out
    assert "proj-nested (off)" in out
    inv = scan_json(h, proj)
    skills = {e["name"]: e for e in inv["hosts"]["claude-code"]["assets"]["skills"]}
    assert skills["offskill"]["status"] == "off" and "status" not in skills["foldedskill"]


def test_mcp_mixed_top_level_and_project(tmp_path):
    h, proj = make_fixture(tmp_path)
    inv = scan_json(h, proj)
    mcp = {e["name"]: e for e in inv["mcp"]["~/.claude.json"]}
    assert mcp["srv-a"]["scope"] == "user"
    assert mcp["nested-srv"]["scope"].startswith("project "), "nested servers survive a top-level block"
    assert mcp["alpha:alpha-srv"]["scope"] == "plugin"
    assert mcp["beta:beta-bare"]["status"].startswith("off"), "bare-map .mcp.json is read"
    assert mcp["beta:beta-inline"]["scope"] == "plugin", "inline plugin.json mcpServers is read"
    assert "toml-srv" in [e["name"] for e in inv["mcp"]["~/.codex/config.toml"]]
    assert "grok-srv" in [e["name"] for e in inv["mcp"]["~/.grok/config.toml"]]
    assert "gem-srv" in [e["name"] for e in inv["mcp"]["~/.gemini/settings.json"]]


def test_cursor_marketplace_traversal(tmp_path):
    h, proj = make_fixture(tmp_path)
    inv = scan_json(h, proj)
    assert inv["hosts"]["cursor"]["assets"]["plugins"] == [{"name": "cplug", "desc": ""}]
    out = run_scan(h, [str(proj)]).stdout
    assert "plugins: cplug" in out and "plugins: gplug" in out
    assert "realext" in out and "extension-enablement" not in out


# ---------------------------------------------------------------- discovery & cross-host

def test_shared_pool_credited_to_readers_not_claude(tmp_path):
    h, proj = make_fixture(tmp_path)
    inv = scan_json(h, proj)
    ch = inv["cross_host"]
    assert ch["universal"] == ["plainskill"]
    assert "poolonly" in ch["missing_here"], "pool skill counts as present in codex/cursor/gemini/grok"
    assert ch["shared_pool"] == 2 and "codex" in ch["shared_readers"]
    assert inv["hosts"]["agents-shared"]["shared_pool"] is True
    assert "claude-code" not in inv["hosts"]["agents-shared"]["readers"]
    assert "shared ~/.agents pool of 2 credited to" in run_scan(h, [str(proj)]).stdout


def test_discovered_roots_and_brief(tmp_path):
    h, proj = make_fixture(tmp_path)
    inv = scan_json(h, proj)
    assert inv["hosts"][".someagent"]["discovered"] is True
    assert inv["hosts"][".pi/agent"]["discovered"] is True
    full = run_scan(h, [str(proj)]).stdout
    assert "## Other harness roots with skills (2)" in full and "## codex" in full
    brief = run_scan(h, ["--brief", str(proj)]).stdout
    assert "## claude-code" in brief and "### skills" in brief
    assert "## codex" not in brief
    assert ".someagent (1)" in brief


def test_deepseek_harness_is_a_first_class_host(tmp_path):
    h, proj = make_fixture(tmp_path)
    inv = scan_json(h, proj)
    ds = inv["hosts"]["deepseek"]
    assert [e["name"] for e in ds["assets"]["skills"]] == ["dshskill", "plainskill"]
    assert not ds.get("discovered"), "deepseek is in the host table, not merely discovered"
    assert "deepseek" not in (inv["cross_host"]["shared_readers"] or []), "no evidence it reads ~/.agents"
    assert "reads project AGENTS.md" in run_scan(h, [str(proj)]).stdout
    # self-install reaches it, and DSH_HOME relocates it
    assert run_scan(h, ["--self-install"]).returncode == 0
    assert (h / ".dsh/skills/loadout/SKILL.md").is_file()
    alt = tmp_path / "altdsh"
    (alt / "skills").mkdir(parents=True)
    r = run_scan(h, ["--json", str(proj)], env={"DSH_HOME": str(alt)})
    assert Path(json.loads(r.stdout)["hosts"]["deepseek"]["root"]) == alt


def test_codex_legacy_note_and_configurable_home(tmp_path):
    h, proj = make_fixture(tmp_path)
    assert "~/.codex/skills is legacy" in run_scan(h, [str(proj)]).stdout
    alt = tmp_path / "altclaude"
    write(alt / "skills/altskill/SKILL.md", "---\ndescription: from CLAUDE_CONFIG_DIR\n---\n")
    inv = scan_json(h, proj) if False else json.loads(
        run_scan(h, ["--json", str(proj)], env={"CLAUDE_CONFIG_DIR": str(alt)}).stdout)
    assert Path(inv["hosts"]["claude-code"]["root"]) == alt
    assert [e["name"] for e in inv["hosts"]["claude-code"]["assets"]["skills"]] == ["altskill"]


def test_junction_or_symlink_detected(tmp_path):
    h, proj = make_fixture(tmp_path)
    src = h / ".agents/skills/poolonly"
    dest = h / ".claude/skills/linked"
    if platform.system() == "Windows":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dest), str(src)], capture_output=True)
        assert r.returncode == 0, r.stderr
    else:
        os.symlink(src, dest, target_is_directory=True)
    inv = scan_json(h, proj)
    linked = next(e for e in inv["hosts"]["claude-code"]["assets"]["skills"] if e["name"] == "linked")
    assert linked["link"].replace("\\", "/").endswith("/.agents/skills/poolonly")
    assert "1 linked" in run_scan(h, [str(proj)]).stdout


def test_host_detection_from_child_shell_signals(tmp_path):
    h, proj = make_fixture(tmp_path)
    def running(env):
        r = run_scan(h, [str(proj)], env=env, host=None)
        assert r.returncode == 0, r.stderr
        return r.stdout.split("Running inside: **")[1].split("**")[0]
    assert running({}) == "unknown"
    assert running({"CODEX_HOME": str(h / ".codex")}) == "unknown", "CODEX_HOME is config, not a marker"
    assert running({"COPILOT_CLI": "1"}) == "copilot"
    assert running({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}) == "codex"
    assert running({"GEMINI_CLI": "1"}) == "gemini"
    assert "no reliable signal" in run_scan(h, [str(proj)], host=None).stdout


def test_nonexistent_project_dir_fails(tmp_path):
    h, _ = make_fixture(tmp_path)
    r = run_scan(h, [str(tmp_path / "nope")])
    assert r.returncode == 2 and "project dir not found" in r.stderr


def test_prior_loadout_detected_for_reaudit(tmp_path):
    h, proj = make_fixture(tmp_path)
    write(proj / "LOADOUT.md", "# Loadout: x\nHarness: claude-code\nDate: 2026-08-31\n\n## Accepted\n- plan: `p`\n")
    write(proj / "AGENTS.md", "# agents\n\n## Loadout\n- plan: invoke `p`\n")
    write(proj / "CLAUDE.md", "@AGENTS.md\n")
    out = run_scan(h, [str(proj)]).stdout
    assert "**prior loadout (re-audit)**: LOADOUT.md exists (dated 2026-08-31); ## Loadout section in AGENTS.md; CLAUDE.md imports AGENTS.md" in out
    lo = scan_json(h, proj)["project"]["loadout"]
    assert lo["LOADOUT.md"]["date"] == "2026-08-31"
    assert lo["AGENTS.md"]["loadout_section"] and lo["CLAUDE.md"]["imports_agents_md"]
    assert not lo["CLAUDE.md"]["loadout_section"]


# ---------------------------------------------------------------- self-install / check

def test_self_install_check_and_host_opt_in(tmp_path):
    h, _ = make_fixture(tmp_path)
    r = run_scan(h, ["--check"])
    assert r.returncode == 1 and "claude-code: not installed" in r.stdout
    r = run_scan(h, ["--self-install"])
    assert r.returncode == 0, r.stderr
    for host_dir in (".claude", ".codex", ".agents", ".gemini", ".grok", ".zcode"):
        dest = h / host_dir / "skills/loadout"
        assert (dest / "SKILL.md").is_file() and (dest / "scripts/scan.py").is_file(), host_dir
        assert (dest / "scripts/apply.py").is_file() and (dest / "LICENSE").is_file(), host_dir
    assert not (h / ".someagent/skills/loadout").exists(), "discovered roots need --hosts all"
    assert not (h / ".qwen").exists(), "absent hosts are skipped, not created"
    r = run_scan(h, ["--check"])
    assert r.returncode == 0 and "stale" not in r.stdout
    (h / ".codex/skills/loadout/SKILL.md").write_text("old", encoding="utf-8")
    r = run_scan(h, ["--check"])
    assert r.returncode == 1 and "codex: stale -> SKILL.md stale" in r.stdout
    r = run_scan(h, ["--self-install", "--hosts", "codex"])
    assert r.returncode == 0 and "codex: updated" in r.stdout
    assert (h / ".codex/skills/loadout/SKILL.md").read_bytes() == (REPO / "SKILL.md").read_bytes()
    r = run_scan(h, ["--self-install", "--hosts", "all"])
    assert r.returncode == 0 and (h / ".someagent/skills/loadout/SKILL.md").is_file()
    r = run_scan(h, ["--self-install", "--hosts", "bogus"])
    assert r.returncode == 2 and "unknown host(s): bogus" in r.stderr


def test_loadout_host_override_is_normalised_to_a_host_key(tmp_path):
    h, proj = make_fixture(tmp_path)
    def running(value, *args):
        r = run_scan(h, [*args, str(proj)], host=value)
        assert r.returncode == 0, r.stderr
        return r.stdout
    # the obvious short names map onto the table keys, so the current-host listing survives
    out = running("claude", "--brief")
    assert "Running inside: **claude-code**" in out
    assert "## claude-code" in out and "### skills" in out
    assert "Running inside: **deepseek**" in running("dsh")
    assert "Running inside: **Claude-Code**" not in running("Claude-Code")
    assert "Running inside: **claude-code**" in running("Claude-Code")
    # an unknown value is not silently taken as a host: fall back and name the valid keys
    out = running("notahost")
    assert "Running inside: **unknown**" in out
    assert "notahost" in out and "claude-code" in out.split("Running inside")[1].split("\n")[0]


def test_brief_keeps_only_the_first_sentence_of_a_description(tmp_path):
    h, proj = make_fixture(tmp_path)
    full = run_scan(h, [str(proj)]).stdout
    assert "**longskill** — Does one thing well. Use when the user asks for the thing." in full
    brief = run_scan(h, ["--brief", str(proj)]).stdout
    assert "- **longskill** — Does one thing well.\n" in brief
    assert "Use when the user asks" not in brief


def test_registered_gate_is_reported_for_reaudit(tmp_path):
    h, proj = make_fixture(tmp_path)
    write(proj / "LOADOUT.md", "# Loadout: x\nDate: 2026-09-02\n\n## Accepted\n- planning: `p`\n")
    write(proj / ".claude/settings.local.json", json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": 'python "/x/gate.py" stop'}]}]}}))
    inv = scan_json(h, proj)
    assert inv["project"]["loadout"]["gate"] == "claude-code"
    out = run_scan(h, [str(proj)]).stdout
    assert "enforcement gate registered (claude-code)" in out
    assert "scripts/gate.py" in scan.SKILL_FILES


def test_foreign_host_hooks_are_collapsed_with_counts(tmp_path):
    h, proj = make_fixture(tmp_path)
    out = run_scan(h, [str(proj)], host="codex").stdout  # claude-code is now a foreign host
    assert "- hooks: PostToolUse, SessionStart ×3\n" in out
    assert "SessionStart, SessionStart" not in out
