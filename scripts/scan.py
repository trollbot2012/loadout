#!/usr/bin/env python
"""loadout scanner — inventory skills/plugins/hooks/commands/agents/MCP across agent harnesses.

Facts only: enumerates names + SKILL.md descriptions. Never reads credential files.
Stdlib only. Usage: python scan.py [--json] [project_dir]
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

HOME = Path.home()
MAX_DESC = 160
MAX_LIST = 200

# host -> (global root, {asset kind: subdir})
HOSTS = {
    "claude-code": ("~/.claude", {"skills": "skills", "plugins": "plugins", "hooks": "hooks",
                                  "commands": "commands", "agents": "agents", "rules": "rules"}),
    # codex ~/.codex/plugins is runtime infra (openai-bundled etc.), not user plugins
    "codex":       ("~/.codex", {"skills": "skills", "hooks": "hooks", "prompts": "prompts"}),
    "cursor":      ("~/.cursor", {"skills": "skills", "plugins": "plugins", "rules": "rules",
                                  "agents": "agents"}),
    "gemini":      ("~/.gemini", {"skills": "skills", "extensions": "extensions"}),
    "opencode":    ("~/.config/opencode", {"skills": "skills", "plugins": "plugins",
                                           "agents": "agents", "commands": "commands"}),
    "crush":       ("~/.config/crush", {"skills": "skills"}),
    "qwen":        ("~/.qwen", {"skills": "skills", "extensions": "extensions", "hooks": "hooks"}),
    "continue":    ("~/.continue", {"skills": "skills"}),
    "copilot":     ("~/.copilot", {"hooks": "hooks"}),
    "grok":        ("~/.grok", {"skills": "skills", "plugins": "installed-plugins", "hooks": "hooks"}),
    # shared cross-agent standard dir; used by DeepSeek Harness among others
    "deepseek-harness (~/.agents)": ("~/.agents", {"skills": "skills", "commands": "commands"}),
}

# project-relative asset dirs, tagged by host family
PROJECT_ASSETS = [
    ("claude-code", ".claude/skills"), ("claude-code", ".claude/commands"),
    ("claude-code", ".claude/agents"), ("claude-code", ".claude/hooks"),
    ("cursor", ".cursor/rules"), ("cursor", ".cursor/skills"),
    ("opencode", ".opencode/skills"), ("opencode", ".opencode/commands"),
    ("generic", ".agents/skills"), ("codex", ".codex/skills"),
]

PROJECT_FILES = ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".mcp.json",
                 "opencode.json", ".windsurfrules", ".github/copilot-instructions.md"]

MANIFESTS = ["package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod",
             "pom.xml", "build.gradle", "Gemfile", "composer.json", "mix.exs",
             "CMakeLists.txt", "Makefile", "Dockerfile", "docker-compose.yml"]

# env markers -> host actually running this process
ENV_MARKERS = [
    ("CLAUDECODE", "claude-code"), ("CLAUDE_CODE_ENTRYPOINT", "claude-code"),
    ("CURSOR_AGENT", "cursor"), ("CURSOR_TRACE_ID", "cursor"),
    ("CODEX_SANDBOX", "codex"), ("CODEX_HOME", "codex"),
    ("GEMINI_CLI", "gemini"), ("OPENCODE", "opencode"),
    ("QWEN_CODE", "qwen"), ("GROK_CLI", "grok"), ("COPILOT_AGENT", "copilot"),
]

# (path, json key) files that may declare MCP servers
MCP_JSON = [
    ("~/.claude.json", "mcpServers"), ("~/.cursor/mcp.json", "mcpServers"),
    ("~/.gemini/settings.json", "mcpServers"), ("~/.qwen/settings.json", "mcpServers"),
    ("~/.config/opencode/opencode.json", "mcp"),
]
MCP_PROJECT_JSON = [(".mcp.json", "mcpServers"), (".cursor/mcp.json", "mcpServers"),
                    (".vscode/mcp.json", "servers"), ("opencode.json", "mcp")]

DESC_RE = re.compile(r"^description:\s*(.+)$", re.M)

# non-plugin infrastructure entries inside plugins/extensions dirs
INFRA_NAMES = {"cache", "data", "local", "config", "marketplaces",
               "known_marketplaces.json", "extension-enablement.json"}


def desc_of(path):
    """Description from a SKILL.md dir or a bare .md file's frontmatter."""
    md = path / "SKILL.md" if path.is_dir() else path
    if not md.is_file() or md.suffix.lower() != ".md":
        return ""
    try:
        head = md.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return ""
    m = DESC_RE.search(head)
    if not m:
        return ""
    val = m.group(1).strip().strip("\"'")
    if val in (">", ">-", ">+", "|", "|-", "|+"):  # YAML block scalar: take first indented line
        m2 = re.search(r"\n[ \t]+(\S.*)", head[m.end():])
        val = m2.group(1).strip() if m2 else ""
    return val[:MAX_DESC]


def scan_dir(d):
    """List entries in an asset dir: [{name, desc}]. Skips dotfiles."""
    if not d.is_dir():
        return None
    out = []
    for p in sorted(d.iterdir()):
        if p.name.startswith(".") or p.name in INFRA_NAMES:
            continue
        out.append({"name": p.stem if p.is_file() else p.name, "desc": desc_of(p)})
    return out


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def plugins_manifest(host, rootp, sub):
    """Installed-plugin names from a host's own manifest, where one exists."""
    if host == "claude-code":
        data = load_json(rootp / sub / "installed_plugins.json")
        if data and isinstance(data.get("plugins"), dict):
            return [{"name": k.split("@")[0], "desc": ""} for k in sorted(data["plugins"])]
    if host == "grok":
        data = load_json(rootp / sub / "registry.json")
        if data and isinstance(data.get("repos"), dict):
            names = set()
            for repo in data["repos"].values():
                names.update(repo.get("plugins", {}).keys())
            return [{"name": n, "desc": ""} for n in sorted(names)]
    if host == "cursor":
        base = rootp / sub / "marketplaces"
        if base.is_dir():
            found = sorted({p.name for p in base.glob("*/*/*") if p.is_dir()})
            return [{"name": n, "desc": ""} for n in found]
    return None  # no manifest convention known: caller falls back to dir scan


def mcp_from_json(path, key):
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    servers = data.get(key)
    if not isinstance(servers, dict):
        # claude: mcpServers may nest under per-project entries
        found = {}
        for v in data.get("projects", {}).values() if isinstance(data.get("projects"), dict) else []:
            if isinstance(v, dict) and isinstance(v.get("mcpServers"), dict):
                found.update(v["mcpServers"])
        servers = found
    return sorted(servers.keys()) if isinstance(servers, dict) else []


def mcp_from_codex_toml():
    p = HOME / ".codex" / "config.toml"
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sorted(set(re.findall(r"^\[mcp_servers\.([^\].]+)", text, re.M)))


def detect_host():
    override = os.environ.get("LOADOUT_HOST")
    if override:
        return override
    for var, host in ENV_MARKERS:
        if os.environ.get(var):
            return host
    return "unknown"


def cross_host(inv):
    """Skill-name coverage across hosts: universal, missing-here, only-here."""
    sets = {h: {e["name"] for e in d["assets"].get("skills", [])}
            for h, d in inv["hosts"].items() if d["assets"].get("skills")}
    if len(sets) < 2:
        return None
    every = set().union(*sets.values())
    cur = inv["running_in"]
    out = {"hosts_with_skills": len(sets),
           "universal": sorted(n for n in every if all(n in s for s in sets.values()))}
    if cur in sets:
        others = [s for h, s in sets.items() if h != cur]
        out["missing_here"] = sorted(n for n in every if n not in sets[cur]
                                     and sum(n in s for s in others) >= 2)
        out["only_here"] = sorted(sets[cur] - set().union(*others))
    return out


def self_install():
    """Copy this skill (SKILL.md + scripts) into every detected harness's skills dir."""
    src = Path(__file__).resolve().parent.parent
    if not (src / "SKILL.md").is_file():
        print(f"self-install: no SKILL.md next to {src}", file=sys.stderr)
        return 1
    for host, (root, kinds) in HOSTS.items():
        rootp = Path(root).expanduser()
        if not rootp.is_dir():
            print(f"- {host}: skipped (harness not present)")
            continue
        if "skills" not in kinds:
            print(f"- {host}: skipped (no skills dir convention)")
            continue
        dest = rootp / kinds["skills"] / "loadout"
        if dest.resolve() == src.resolve():
            print(f"- {host}: source copy (already here)")
            continue
        (dest / "scripts").mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "SKILL.md", dest / "SKILL.md")
        if (src / "README.md").is_file():
            shutil.copy2(src / "README.md", dest / "README.md")
        shutil.copy2(src / "scripts" / "scan.py", dest / "scripts" / "scan.py")
        print(f"- {host}: installed -> {dest}")
    return 0


def build_inventory(project_dir):
    inv = {"running_in": detect_host(), "hosts": {}, "mcp": {}, "project": {}}

    for host, (root, kinds) in HOSTS.items():
        rootp = Path(root).expanduser()
        if not rootp.is_dir():
            continue
        assets = {}
        for kind, sub in kinds.items():
            entries = plugins_manifest(host, rootp, sub) if kind == "plugins" else None
            if entries is None:
                entries = scan_dir(rootp / sub)
            if entries:
                assets[kind] = entries
        inv["hosts"][host] = {"root": str(rootp), "assets": assets}

    for path, key in MCP_JSON:
        names = mcp_from_json(path, key)
        if names:
            inv["mcp"][path] = names
    codex_mcp = mcp_from_codex_toml()
    if codex_mcp:
        inv["mcp"]["~/.codex/config.toml"] = codex_mcp

    proj = Path(project_dir).resolve()
    inv["project"]["dir"] = str(proj)
    for host, rel in PROJECT_ASSETS:
        entries = scan_dir(proj / rel)
        if entries:
            inv["project"].setdefault("assets", {})[rel] = entries
    inv["project"]["files"] = [f for f in PROJECT_FILES if (proj / f).is_file()]
    inv["project"]["manifests"] = [f for f in MANIFESTS if (proj / f).is_file()]
    for rel, key in MCP_PROJECT_JSON:
        names = mcp_from_json(proj / rel, key)
        if names:
            inv["project"].setdefault("mcp", {})[rel] = names
    inv["cross_host"] = cross_host(inv)
    return inv


def markdown(inv):
    lines = ["# Harness Inventory", "", f"Running inside: **{inv['running_in']}**", ""]
    cur = inv["running_in"]
    for host, data in inv["hosts"].items():
        counts = ", ".join(f"{k}: {len(v)}" for k, v in data["assets"].items()) or "nothing found"
        lines.append(f"## {host} ({data['root']}) — {counts}")
        # full listing only for the host we're running in; names-only elsewhere
        for kind, entries in data["assets"].items():
            if host == cur or cur == "unknown":
                lines.append(f"### {kind}")
                for e in entries[:MAX_LIST]:
                    lines.append(f"- **{e['name']}**" + (f" — {e['desc']}" if e["desc"] else ""))
                if len(entries) > MAX_LIST:
                    lines.append(f"- …and {len(entries) - MAX_LIST} more")
            else:
                names = ", ".join(e["name"] for e in entries[:40])
                more = f", …+{len(entries) - 40}" if len(entries) > 40 else ""
                lines.append(f"- {kind}: {names}{more}")
        lines.append("")
    ch = inv.get("cross_host")
    if ch:
        lines.append("## Cross-host skill coverage")
        lines.append(f"- hosts with skills installed: {ch['hosts_with_skills']}")
        lines.append(f"- universal (in every host): {len(ch['universal'])}")
        for key, label in (("missing_here", "missing in this host but in >=2 others"),
                           ("only_here", "only in this host")):
            names = ch.get(key)
            if names is not None:
                shown = ", ".join(names[:30]) + (f", …+{len(names) - 30}" if len(names) > 30 else "")
                lines.append(f"- {label} ({len(names)}): {shown or 'none'}")
        lines.append("")
    if inv["mcp"]:
        lines.append("## MCP servers (global)")
        for src, names in inv["mcp"].items():
            lines.append(f"- {src}: {', '.join(names)}")
        lines.append("")
    lines.append(f"## Project: {inv['project']['dir']}")
    if inv["project"]["files"]:
        lines.append(f"- config files: {', '.join(inv['project']['files'])}")
    if inv["project"]["manifests"]:
        lines.append(f"- manifests: {', '.join(inv['project']['manifests'])}")
    for rel, entries in inv["project"].get("assets", {}).items():
        lines.append(f"- {rel}: {', '.join(e['name'] for e in entries)}")
    for rel, names in inv["project"].get("mcp", {}).items():
        lines.append(f"- MCP ({rel}): {', '.join(names)}")
    if not inv["project"]["files"] and not inv["project"].get("assets"):
        lines.append("- no project-level agent config found")
    return "\n".join(lines)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--self-install" in sys.argv:
        sys.exit(self_install())
    args = [a for a in sys.argv[1:] if a != "--json"]
    inv = build_inventory(args[0] if args else os.getcwd())
    if "--json" in sys.argv:
        json.dump(inv, sys.stdout, indent=1)
    else:
        print(markdown(inv))


if __name__ == "__main__":
    main()
