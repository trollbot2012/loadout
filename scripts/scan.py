#!/usr/bin/env python3
"""loadout scanner — inventory skills/plugins/hooks/commands/agents/MCP across agent harnesses.

Facts only: names, SKILL.md descriptions, registered hooks, enabled/disabled state.
Never prints credential values. Stdlib only; Python 3.9+; Windows/macOS/Linux.

Usage:
  python scan.py [--json] [--brief] [project_dir]
  python scan.py --check [--hosts a,b|all]          # compare installed copies to this source
  python scan.py --self-install [--hosts a,b|all]   # copy this skill into harness skills dirs
"""
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

MAX_DESC = 400      # chars of description kept (trigger text usually sits past 160)
MAX_LIST = 200
MAX_HEAD = 16384    # bytes of SKILL.md read for frontmatter
SKILL_FILES = ["SKILL.md", "README.md", "LICENSE", "scripts/scan.py", "scripts/apply.py", "scripts/gate.py"]


def _root(env_var, default):
    """Harness root, honouring the documented home override when set."""
    v = os.environ.get(env_var) if env_var else None
    return str(Path(v).expanduser()) if v else default


XDG = os.environ.get("XDG_CONFIG_HOME") or "~/.config"

# host -> (global root, {asset kind: subdir})
HOSTS = {
    "claude-code": (_root("CLAUDE_CONFIG_DIR", "~/.claude"),
                    {"skills": "skills", "commands": "commands", "agents": "agents", "rules": "rules"}),
    # ~/.codex/skills is deprecated by Codex in favour of ~/.agents/skills but still read
    "codex":       (_root("CODEX_HOME", "~/.codex"), {"skills": "skills", "prompts": "prompts", "rules": "rules"}),
    "cursor":      ("~/.cursor", {"skills": "skills", "plugins": "plugins", "rules": "rules", "agents": "agents"}),
    "gemini":      ("~/.gemini", {"skills": "skills", "extensions": "extensions", "commands": "commands"}),
    "opencode":    (XDG + "/opencode", {"skills": "skills", "plugins": "plugins",
                                        "agents": "agents", "commands": "commands"}),
    "crush":       (XDG + "/crush", {"skills": "skills"}),
    "qwen":        ("~/.qwen", {"skills": "skills", "extensions": "extensions"}),
    "continue":    ("~/.continue", {"skills": "skills"}),
    "copilot":     ("~/.copilot", {"skills": "skills", "agents": "agents", "hooks": "hooks"}),
    "grok":        (_root("GROK_HOME", "~/.grok"),
                    {"skills": "skills", "plugins": "installed-plugins", "hooks": "hooks"}),
    "vibe":        (_root("VIBE_HOME", "~/.vibe"), {"skills": "skills"}),
    # DeepSeek Harness: own skills dir; reads project AGENTS.md/CLAUDE.md + $DSH_HOME/AGENTS.md
    "deepseek":    (_root("DSH_HOME", "~/.dsh"), {"skills": "skills"}),
    "hermes":      (_root("HERMES_HOME", "~/.hermes"), {"skills": "skills"}),
    "zcode":       ("~/.zcode", {"skills": "skills"}),
}
# ~/.agents/skills: the cross-agent shared pool. Not a harness of its own.
SHARED = "agents-shared"
SHARED_ROOT = "~/.agents"
# hosts whose official docs say they read ~/.agents/skills natively (verified 2026-09-01)
SHARED_READERS = {"codex", "gemini", "cursor", "opencode", "copilot", "grok", "crush"}
HOST_NOTES = {"codex": "~/.codex/skills is legacy (still read); Codex prefers ~/.agents/skills",
              "deepseek": "DeepSeek Harness; reads project AGENTS.md/CLAUDE.md natively"}

# hosts whose hook registrations live in a JSON file (Claude: settings stack, others: one file)
HOOK_JSON = {
    "codex": ["hooks.json"], "cursor": ["hooks.json"],
    "gemini": ["settings.json"], "qwen": ["settings.json"],
}
NATIVE_FILES = {"claude-code": "CLAUDE.md", "gemini": "GEMINI.md", "qwen": "QWEN.md"}

# extra global skills roots used by harnesses that nest them (from the skills CLI agent table)
EXTRA_SKILL_ROOTS = [
    "~/.gemini/antigravity/skills", "~/.gemini/antigravity-cli/skills", "~/.pi/agent/skills",
    "~/.codeium/windsurf/skills", "~/.tabnine/agent/skills", "~/.deepagents/agent/skills",
    "~/.snowflake/cortex/skills", "~/.posit/assistant/skills", XDG + "/agents/skills",
    XDG + "/kimchi/harness/skills",
]

# project-relative asset dirs, tagged by host family
PROJECT_ASSETS = [
    ("claude-code", ".claude/skills"), ("claude-code", ".claude/commands"),
    ("claude-code", ".claude/agents"),
    ("cursor", ".cursor/rules"), ("cursor", ".cursor/skills"),
    ("opencode", ".opencode/skills"), ("opencode", ".opencode/commands"),
    ("generic", ".agents/skills"), ("codex", ".codex/skills"),
]

PROJECT_FILES = ["AGENTS.md", "CLAUDE.md", "GEMINI.md", "QWEN.md", "LOADOUT.md", ".cursorrules",
                 ".mcp.json", "opencode.json", ".windsurfrules", ".github/copilot-instructions.md",
                 ".claude/settings.json", ".claude/settings.local.json"]

MANIFESTS = ["package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod",
             "pom.xml", "build.gradle", "Gemfile", "composer.json", "mix.exs",
             "CMakeLists.txt", "Makefile", "Dockerfile", "docker-compose.yml"]

# env markers -> host. These are CHILD-SHELL signals the harness sets for commands it runs,
# not a top-level identity; verified against official docs/source 2026-09-01. Codex sets its
# markers only when sandboxed; Grok sets none outside hooks. Unknown stays unknown.
# short names people actually type for LOADOUT_HOST -> table key
HOST_ALIASES = {"claude": "claude-code", "claude_code": "claude-code", "claudecode": "claude-code",
                "dsh": "deepseek", "deepseek-harness": "deepseek", "copilot-cli": "copilot"}

ENV_MARKERS = [
    ("CLAUDECODE", "claude-code"), ("CLAUDE_CODE_CHILD_SESSION", "claude-code"),
    ("CURSOR_AGENT", "cursor"),
    ("CODEX_SANDBOX", "codex"), ("CODEX_SANDBOX_NETWORK_DISABLED", "codex"),
    ("GEMINI_CLI", "gemini"), ("OPENCODE", "opencode"), ("QWEN_CODE", "qwen"),
    ("COPILOT_CLI", "copilot"), ("COPILOT_AGENT_SESSION_ID", "copilot"),
]

# (path, json key) files that may declare MCP servers
MCP_JSON = [
    ("~/.cursor/mcp.json", "mcpServers"), ("~/.gemini/settings.json", "mcpServers"),
    ("~/.qwen/settings.json", "mcpServers"), (XDG + "/opencode/opencode.json", "mcp"),
    ("~/.copilot/mcp-config.json", "mcpServers"),
]
MCP_PROJECT_JSON = [(".mcp.json", "mcpServers"), (".cursor/mcp.json", "mcpServers"),
                    (".vscode/mcp.json", "servers"), ("opencode.json", "mcp")]

DESC_RE = re.compile(r"^description:[ \t]*(.*)$", re.M)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SECTION_RE = re.compile(r"^## Loadout\b", re.M)

# non-plugin infrastructure entries inside plugins/extensions dirs
INFRA_NAMES = {"cache", "data", "local", "config", "marketplaces",
               "known_marketplaces.json", "extension-enablement.json"}


# ---------------------------------------------------------------- small helpers

def load_json(path):
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def link_target(p):
    """Resolved target if p is a symlink or a Windows junction, else None.
    os.path.isjunction is 3.12+, so fall back to the reparse-point attribute."""
    try:
        st = os.lstat(p)
    except OSError:
        return None
    is_link = stat.S_ISLNK(st.st_mode)
    isj = getattr(os.path, "isjunction", None)
    if isj is not None:
        is_link = is_link or isj(p)
    else:
        attrs = getattr(st, "st_file_attributes", 0)
        is_link = is_link or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if not is_link:
        return None
    try:
        return str(Path(p).resolve())
    except OSError:
        return None


def frontmatter(text):
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else text[3:]


def desc_of(path):
    """Full description from a SKILL.md dir or a bare .md file's frontmatter.
    Handles plain multi-line scalars and YAML block scalars (>, |)."""
    md = path / "SKILL.md" if path.is_dir() else path
    if not md.is_file() or md.suffix.lower() != ".md":
        return ""
    try:
        with open(md, "rb") as f:
            head = f.read(MAX_HEAD).decode("utf-8", errors="replace")
    except OSError:
        return ""
    fm = frontmatter(head)
    m = DESC_RE.search(fm)
    if not m:
        return ""
    first = m.group(1).strip()
    cont = []
    for line in fm[m.end():].split("\n"):
        if line.strip() == "":
            continue
        if line[0] in " \t":
            cont.append(line.strip())
        else:
            break
    if first in (">", ">-", ">+", "|", "|-", "|+"):
        val = " ".join(cont)
    else:
        val = " ".join([first] + cont if first else cont)
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return val[:MAX_DESC]


def scan_dir(d):
    """List entries in an asset dir: [{name, desc[, link]}]. Skips dotfiles."""
    if not d.is_dir():
        return None
    out = []
    for p in sorted(d.iterdir()):
        if p.name.startswith(".") or p.name in INFRA_NAMES:
            continue
        e = {"name": p.stem if p.is_file() else p.name, "desc": desc_of(p)}
        tgt = link_target(p)
        if tgt:
            e["link"] = tgt
        out.append(e)
    return out


def short_cmd(cmd):
    return re.sub(r"\s+", " ", str(cmd)).strip()[:80]


def hooks_from_data(data, source):
    """Registered hooks from a Claude/Codex/Gemini/Qwen/Cursor-shaped 'hooks' mapping."""
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return []
    off = bool(data.get("disableAllHooks"))
    out = []
    for event, items in hooks.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            matcher = item.get("matcher") or ""
            inner = item.get("hooks") if isinstance(item.get("hooks"), list) else [item]
            for h in inner:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command") or h.get("commandWindows") or h.get("type") or ""
                e = {"name": event, "desc": (f"[{matcher}] " if matcher else "") + short_cmd(cmd) + f" ({source})"}
                if off:
                    e["status"] = "off (disableAllHooks)"
                out.append(e)
    return out


def hooks_from_file(path, source):
    data = load_json(path)
    return hooks_from_data(data, source) if isinstance(data, dict) else []


def mcp_names(path, key):
    data = load_json(path)
    servers = data.get(key) if isinstance(data, dict) else None
    return sorted(servers) if isinstance(servers, dict) else []


def mcp_from_toml(path):
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sorted(set(re.findall(r"^\[mcp_servers\.([^\].]+)", text, re.M)))


def plugin_mcp(ip):
    """MCP server names a Claude plugin declares: .mcp.json (wrapped in mcpServers or a bare
    name->config map) and/or an inline or referenced mcpServers field in plugin.json."""
    names = set()
    data = load_json(ip / ".mcp.json")
    if isinstance(data, dict):
        servers = data["mcpServers"] if isinstance(data.get("mcpServers"), dict) else data
        names |= {k for k, v in servers.items() if isinstance(v, dict)}
    pj = load_json(ip / ".claude-plugin" / "plugin.json")
    ms = pj.get("mcpServers") if isinstance(pj, dict) else None
    if isinstance(ms, dict):
        names |= set(ms)
    elif isinstance(ms, str):
        names |= set(mcp_names(ip / ms, "mcpServers"))
    return sorted(names)


def plugins_manifest(host, rootp, sub):
    """Installed-plugin names from a host's own manifest, where one exists (non-Claude hosts)."""
    if host == "grok":
        data = load_json(rootp / sub / "registry.json")
        if data and isinstance(data.get("repos"), dict):
            names = set()
            for repo in data["repos"].values():
                names.update((repo.get("plugins") or {}).keys())
            return [{"name": n, "desc": ""} for n in sorted(names)]
    if host == "cursor":
        base = rootp / sub / "marketplaces"
        if base.is_dir():
            found = sorted({p.name for p in base.glob("*/*/*") if p.is_dir()})
            return [{"name": n, "desc": ""} for n in found]
    return None  # no manifest convention known: caller falls back to dir scan


# ---------------------------------------------------------------- Claude Code settings layer

def norm(p):
    return str(p).replace("\\", "/").rstrip("/")


def claude_layer(rootp, proj):
    """Plugins (+ their skills/agents/commands/hooks/MCP), registered hooks, and
    enabled/disabled state from the settings stack: user, user-local, project, project-local."""
    stack = []
    for label, p in (("~/.claude/settings.json", rootp / "settings.json"),
                     ("~/.claude/settings.local.json", rootp / "settings.local.json"),
                     (".claude/settings.json", proj / ".claude" / "settings.json"),
                     (".claude/settings.local.json", proj / ".claude" / "settings.local.json")):
        data = load_json(p)
        if isinstance(data, dict):
            stack.append((label, data))
    enabled, overrides, mcp_off = {}, {}, set()
    hooks = []
    for label, data in stack:
        enabled.update(data.get("enabledPlugins") or {})
        overrides.update(data.get("skillOverrides") or {})
        for key in ("disabledMcpServers", "disabledMcpjsonServers"):
            mcp_off.update(data.get(key) or [])
        hooks.extend(hooks_from_data(data, label))

    layer = {"plugins": [], "plugin-skills": [], "agents": [], "commands": [],
             "hooks": hooks, "mcp": [], "skill_status": overrides, "mcp_off": mcp_off}
    data = load_json(rootp / "plugins" / "installed_plugins.json") or {}
    for key, recs in sorted((data.get("plugins") or {}).items()):
        name = key.split("@")[0]
        rec = recs[0] if isinstance(recs, list) and recs and isinstance(recs[0], dict) else {}
        on = bool(enabled.get(key, True))
        ent = {"name": name, "desc": rec.get("version", "")}
        if not on:
            ent["status"] = "off"
        layer["plugins"].append(ent)
        ip = rec.get("installPath")
        if not ip:
            continue
        ip = Path(ip)
        for kind, sub in (("plugin-skills", "skills"), ("agents", "agents"), ("commands", "commands")):
            for e in scan_dir(ip / sub) or []:
                e["name"] = f"{name}:{e['name']}"
                if not on:
                    e["status"] = "off (plugin disabled)"
                layer[kind].append(e)
        for h in hooks_from_file(ip / "hooks" / "hooks.json", f"plugin {name}"):
            if not on:
                h["status"] = "off (plugin disabled)"
            layer["hooks"].append(h)
        for n in plugin_mcp(ip):
            e = {"name": f"{name}:{n}", "scope": "plugin"}
            if not on:
                e["status"] = "off (plugin disabled)"
            layer["mcp"].append(e)
    return layer


def mcp_claude(path, proj, mcp_off):
    """~/.claude.json: user-level servers AND every project's servers (a top-level block must not
    hide the nested ones). Disabled state from the settings stack and the project entry."""
    data = load_json(path)
    if not isinstance(data, dict):
        return []
    out = []
    seen = set()
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        for n in sorted(servers):
            out.append({"name": n, "scope": "user"})
            seen.add(n)
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    off = set(mcp_off)
    for pkey, v in projects.items():
        if not isinstance(v, dict):
            continue
        if norm(pkey) == norm(proj):
            off.update(v.get("disabledMcpjsonServers") or [])
        for n in sorted(v.get("mcpServers") or {}):
            if n not in seen:
                out.append({"name": n, "scope": f"project {pkey}"})
                seen.add(n)
    for e in out:
        if e["name"] in off:
            e["status"] = "off"
    return out


# ---------------------------------------------------------------- detection & discovery

def detect_host():
    """(host, how). Env markers are child-shell signals, so 'how' names the signal."""
    override = os.environ.get("LOADOUT_HOST")
    if override:
        key = override.strip().lower()
        key = HOST_ALIASES.get(key, key)
        if key in HOSTS:
            return key, "LOADOUT_HOST override"
        return "unknown", f"LOADOUT_HOST={override!r} is not a known host; use one of {', '.join(HOSTS)}"
    for var, host in ENV_MARKERS:
        if os.environ.get(var):
            return host, f"env {var}, a child-shell signal; set LOADOUT_HOST if wrong"
    return "unknown", "no reliable signal; set LOADOUT_HOST=<host>"


def discover_roots(known):
    """Any other <root>/skills dir holding SKILL.md children, e.g. the ~60 agents the skills CLI
    installs into. Names only; never a self-install target unless --hosts all."""
    home = Path.home()
    cands = list(home.glob(".*/skills")) + list((home / ".config").glob("*/skills"))
    cands += [Path(p).expanduser() for p in EXTRA_SKILL_ROOTS]
    found = {}
    for d in cands:
        root = d.parent
        if not d.is_dir() or norm(root) in known:
            continue
        try:
            entries = [{"name": p.name, "desc": ""} for p in sorted(d.iterdir())
                       if not p.name.startswith(".") and (p / "SKILL.md").is_file()]
        except OSError:
            entries = []
        if not entries:
            continue
        try:
            label = norm(root.relative_to(home))
        except ValueError:
            label = norm(root)
        found[label] = {"root": str(root), "assets": {"skills": entries}, "discovered": True}
    return found


def cross_host(inv):
    """Skill-name coverage across hosts. ~/.agents is a pool credited to its verified readers,
    never a host of its own; Claude Code sees only its own dir (junctions included)."""
    hosts = inv["hosts"]
    pool = {e["name"] for e in hosts.get(SHARED, {}).get("assets", {}).get("skills", [])}
    sets = {}
    for h, d in hosts.items():
        if h == SHARED:
            continue
        s = {e["name"] for e in d["assets"].get("skills", [])}
        if h in SHARED_READERS:
            s |= pool
        if s:
            sets[h] = s
    if len(sets) < 2:
        return None
    every = set().union(*sets.values())
    cur = inv["running_in"]
    out = {"hosts_with_skills": len(sets), "shared_pool": len(pool),
           "shared_readers": sorted(h for h in sets if h in SHARED_READERS),
           "universal": sorted(n for n in every if all(n in s for s in sets.values()))}
    if cur in sets:
        others = [s for h, s in sets.items() if h != cur]
        out["missing_here"] = sorted(n for n in every if n not in sets[cur]
                                     and sum(n in s for s in others) >= 2)
        out["only_here"] = sorted(sets[cur] - set().union(*others))
    return out


# ---------------------------------------------------------------- self-install / check

def install_targets(hosts_arg, discovered):
    """Table hosts present on disk by default; --hosts a,b for a subset; --hosts all adds
    every discovered root."""
    table = {h: Path(root).expanduser() / kinds["skills"]
             for h, (root, kinds) in HOSTS.items() if "skills" in kinds}
    table[SHARED] = Path(SHARED_ROOT).expanduser() / "skills"
    present = {h: p for h, p in table.items() if p.parent.is_dir()}
    if hosts_arg == "all":
        present.update({h: Path(d["root"]) / "skills" for h, d in discovered.items()})
        return present
    if hosts_arg:
        wanted = [w.strip() for w in hosts_arg.split(",") if w.strip()]
        unknown = [w for w in wanted if w not in table and w not in discovered]
        if unknown:
            print(f"unknown host(s): {', '.join(unknown)}; known: {', '.join(sorted(table))}",
                  file=sys.stderr)
            return None
        return {w: (table.get(w) or Path(discovered[w]["root"]) / "skills") for w in wanted}
    return present


def self_install(hosts_arg, check_only):
    src = Path(__file__).resolve().parent.parent
    if not (src / "SKILL.md").is_file():
        print(f"self-install: no SKILL.md next to {src}", file=sys.stderr)
        return 1
    known = {norm(Path(r).expanduser()) for r, _ in HOSTS.values()} | {norm(Path(SHARED_ROOT).expanduser())}
    targets = install_targets(hosts_arg, discover_roots(known))
    if targets is None:
        return 2
    stale = 0
    for host, skills_dir in sorted(targets.items()):
        dest = skills_dir / "loadout"
        if dest.exists() and dest.resolve() == src.resolve():
            print(f"- {host}: source copy (already here)")
            continue
        state = []
        for rel in SKILL_FILES:
            s, d = src / rel, dest / rel
            if not s.is_file():
                continue
            if not d.is_file():
                state.append(("missing", rel))
            elif s.read_bytes() != d.read_bytes():
                state.append(("stale", rel))
        if check_only:
            if not dest.is_dir():
                print(f"- {host}: not installed ({dest})")
                stale += 1
            elif state:
                print(f"- {host}: stale -> " + ", ".join(f"{rel} {why}" for why, rel in state))
                stale += 1
            else:
                print(f"- {host}: up to date")
            continue
        if not state and dest.is_dir():
            print(f"- {host}: up to date ({dest})")
            continue
        (dest / "scripts").mkdir(parents=True, exist_ok=True)
        for rel in SKILL_FILES:
            if (src / rel).is_file():
                shutil.copy2(src / rel, dest / rel)
        print(f"- {host}: {'updated' if dest.is_dir() and state else 'installed'} -> {dest}")
    return 1 if (check_only and stale) else 0


# ---------------------------------------------------------------- inventory

def project_info(proj):
    info = {"dir": str(proj), "exists": proj.is_dir()}
    if not proj.is_dir():
        return info
    for host, rel in PROJECT_ASSETS:
        entries = scan_dir(proj / rel)
        if entries:
            info.setdefault("assets", {})[rel] = entries
    info["files"] = [f for f in PROJECT_FILES if (proj / f).is_file()]
    info["manifests"] = [f for f in MANIFESTS if (proj / f).is_file()]
    info["git"] = (proj / ".git").exists()  # review/branch skills are blocked without it
    for rel, key in MCP_PROJECT_JSON:
        names = mcp_names(proj / rel, key)
        if names:
            info.setdefault("mcp", {})[rel] = names
    # re-audit signals: a prior LOADOUT.md and any instruction file carrying a ## Loadout section
    lo = {}
    lp = proj / "LOADOUT.md"
    if lp.is_file():
        try:
            head = "\n".join(lp.read_text(encoding="utf-8", errors="replace").splitlines()[:10])
        except OSError:
            head = ""
        m = DATE_RE.search(head)
        lo["LOADOUT.md"] = {"date": m.group(0) if m else None}
    for f in ["AGENTS.md"] + sorted(NATIVE_FILES.values()):
        p = proj / f
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            d = {"loadout_section": bool(SECTION_RE.search(text))}
            if f == "CLAUDE.md":
                d["imports_agents_md"] = bool(re.search(r"^@AGENTS\.md\s*$", text, re.M))
            lo[f] = d
    local = proj / ".claude" / "settings.local.json"  # apply.py registers the gate hooks here
    if local.is_file():
        try:
            if "gate.py" in local.read_text(encoding="utf-8", errors="replace"):
                lo["gate"] = "claude-code"
        except OSError:
            pass
    if lo:
        info["loadout"] = lo
    return info


def build_inventory(project_dir):
    host, how = detect_host()
    inv = {"running_in": host, "detection": how, "hosts": {}, "mcp": {}, "project": {}}
    proj = Path(project_dir).expanduser().resolve()

    known = set()
    for hname, (root, kinds) in HOSTS.items():
        rootp = Path(root).expanduser()
        known.add(norm(rootp))
        if not rootp.is_dir():
            continue
        assets = {}
        for kind, sub in kinds.items():
            entries = plugins_manifest(hname, rootp, sub) if kind == "plugins" else None
            if entries is None:
                entries = scan_dir(rootp / sub)
            if entries:
                assets[kind] = entries
        for fname in HOOK_JSON.get(hname, []):
            hooks = hooks_from_file(rootp / fname, f"{rootp.name}/{fname}")
            if hooks:
                assets.setdefault("hooks", []).extend(hooks)
        if hname == "claude-code":
            layer = claude_layer(rootp, proj)
            for kind in ("plugins", "plugin-skills", "agents", "commands", "hooks"):
                if layer[kind]:
                    assets.setdefault(kind, []).extend(layer[kind]) if kind in ("agents", "commands") \
                        else assets.__setitem__(kind, layer[kind])
            for e in assets.get("skills", []):
                st = layer["skill_status"].get(e["name"])
                if st and st != "on":
                    e["status"] = st
            claude_mcp = mcp_claude(Path("~/.claude.json").expanduser(), proj, layer["mcp_off"]) + layer["mcp"]
            if claude_mcp:
                inv["mcp"]["~/.claude.json"] = claude_mcp
        entry = {"root": str(rootp), "assets": assets}
        if hname in HOST_NOTES:
            entry["note"] = HOST_NOTES[hname]
        inv["hosts"][hname] = entry

    shared = Path(SHARED_ROOT).expanduser()
    known.add(norm(shared))
    if shared.is_dir():
        assets = {}
        for kind in ("skills", "commands"):
            entries = scan_dir(shared / kind)
            if entries:
                assets[kind] = entries
        inv["hosts"][SHARED] = {"root": str(shared), "assets": assets, "shared_pool": True,
                                "readers": sorted(SHARED_READERS)}

    inv["hosts"].update(discover_roots(known))

    for path, key in MCP_JSON:
        names = mcp_names(path, key)
        if names:
            inv["mcp"][path] = [{"name": n} for n in names]
    for label, path in (("~/.codex/config.toml", Path(HOSTS["codex"][0]).expanduser() / "config.toml"),
                        ("~/.grok/config.toml", Path(HOSTS["grok"][0]).expanduser() / "config.toml")):
        names = mcp_from_toml(path)
        if names:
            inv["mcp"][label] = [{"name": n} for n in names]

    inv["project"] = project_info(proj)
    inv["cross_host"] = cross_host(inv)
    return inv


# ---------------------------------------------------------------- output

def fmt_entry(e, desc_cap=MAX_DESC, brief=False):
    s = f"- **{e['name']}**"
    if e.get("status"):
        s += f" ({e['status']})"
    desc = e.get("desc")
    if desc:
        if brief:  # first sentence only: enough to classify, short enough for 100+ skills
            desc = re.split(r"(?<=[.!?])\s+", desc, maxsplit=1)[0]
            desc_cap = 160
        s += f" — {desc[:desc_cap]}"
    return s


def counts_line(assets):
    parts = []
    for k, v in assets.items():
        n = len(v)
        links = sum(1 for e in v if e.get("link"))
        off = sum(1 for e in v if e.get("status"))
        extra = []
        if links:
            extra.append(f"{links} linked")
        if off:
            extra.append(f"{off} off")
        parts.append(f"{k}: {n}" + (f" ({', '.join(extra)})" if extra else ""))
    return ", ".join(parts) or "nothing found"


def markdown(inv, brief=False):
    cur = inv["running_in"]
    p = inv["project"]
    lines = ["# Harness Inventory", "", f"## Project: {p['dir']}"]
    if p.get("files"):
        lines.append(f"- config files: {', '.join(p['files'])}")
    if p.get("manifests"):
        lines.append(f"- manifests: {', '.join(p['manifests'])}")
    if "git" in p:
        lines.append("- git: " + ("repository present" if p["git"] else "NOT a git repository (review/branch skills are blocked)"))
    for rel, entries in p.get("assets", {}).items():
        lines.append(f"- {rel}: {', '.join(e['name'] for e in entries)}")
    for rel, names in p.get("mcp", {}).items():
        lines.append(f"- MCP ({rel}): {', '.join(names)}")
    lo = p.get("loadout")
    if lo:
        bits = []
        if "LOADOUT.md" in lo:
            bits.append("LOADOUT.md exists" + (f" (dated {lo['LOADOUT.md']['date']})" if lo["LOADOUT.md"]["date"] else ""))
        secs = [f for f, d in lo.items() if f not in ("LOADOUT.md", "gate") and d.get("loadout_section")]
        if secs:
            bits.append("## Loadout section in " + ", ".join(secs))
        if "CLAUDE.md" in lo:
            bits.append("CLAUDE.md imports AGENTS.md" if lo["CLAUDE.md"].get("imports_agents_md")
                        else "CLAUDE.md does not import AGENTS.md")
        if lo.get("gate"):
            bits.append(f"enforcement gate registered ({lo['gate']})")
        lines.append("- **prior loadout (re-audit)**: " + "; ".join(bits))
    if not p.get("files") and not p.get("assets"):
        lines.append("- no project-level agent config found")
    lines += ["", f"Running inside: **{cur}** ({inv['detection']})", ""]

    hosts = inv["hosts"]
    order = [h for h in hosts if h == cur] + [h for h in hosts if h != cur]
    discovered = []
    for host in order:
        data = hosts[host]
        if data.get("discovered"):
            discovered.append(host)
            continue
        if brief and host != cur and cur != "unknown":
            continue
        head = f"## {host} ({data['root']}) — {counts_line(data['assets'])}"
        if data.get("shared_pool"):
            head += f"\n- shared pool read natively by: {', '.join(data['readers'])}"
        if data.get("note"):
            head += f"\n- note: {data['note']}"
        lines.append(head)
        for kind, entries in data["assets"].items():
            if host == cur or cur == "unknown":
                lines.append(f"### {kind}")
                lines.extend(fmt_entry(e, brief=brief) for e in entries[:MAX_LIST])
                if len(entries) > MAX_LIST:
                    lines.append(f"- …and {len(entries) - MAX_LIST} more")
            else:
                if kind == "hooks":  # foreign hosts: event names only, so collapse repeats
                    seen = {}
                    for e in entries:
                        seen[e["name"]] = seen.get(e["name"], 0) + 1
                    entries = [{"name": k + (f" ×{n}" if n > 1 else "")} for k, n in seen.items()]
                names = ", ".join(e["name"] + (" (off)" if e.get("status") else "") for e in entries[:40])
                more = f", …+{len(entries) - 40}" if len(entries) > 40 else ""
                lines.append(f"- {kind}: {names}{more}")
        lines.append("")
    if discovered:
        lines.append(f"## Other harness roots with skills ({len(discovered)})")
        if brief:
            lines.append("- " + ", ".join(f"{h} ({len(hosts[h]['assets']['skills'])})" for h in discovered))
        else:
            for h in discovered:
                sk = hosts[h]["assets"]["skills"]
                lines.append(f"- {h}: {len(sk)} skills")
        lines.append("")

    ch = inv.get("cross_host")
    if ch:
        lines.append("## Cross-host skill coverage")
        lines.append(f"- hosts with skills installed: {ch['hosts_with_skills']}"
                     + (f"; shared ~/.agents pool of {ch['shared_pool']} credited to {', '.join(ch['shared_readers'])}"
                        if ch.get("shared_pool") else ""))
        lines.append(f"- universal (in every host): {len(ch['universal'])}")
        for key, label in (("missing_here", "missing in this host but in >=2 others"),
                           ("only_here", "only in this host")):
            names = ch.get(key)
            if names is not None:
                shown = ", ".join(names[:30]) + (f", …+{len(names) - 30}" if len(names) > 30 else "")
                lines.append(f"- {label} ({len(names)}): {shown or 'none'}")
        lines.append("- plugin-provided skills are listed under plugin-skills, not here: check them before calling a missing name a gap")
        lines.append("")
    if inv["mcp"]:
        lines.append("## MCP servers (global)")
        for src, entries in inv["mcp"].items():
            lines.append(f"- {src}: " + ", ".join(
                e["name"] + (f" ({e['status']})" if e.get("status") else "")
                + (f" [{e['scope']}]" if e.get("scope") and e["scope"] != "user" else "") for e in entries))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    hosts_arg = None
    if "--hosts" in argv:
        i = argv.index("--hosts")
        hosts_arg = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
    if "--self-install" in argv or "--check" in argv:
        sys.exit(self_install(hosts_arg, "--check" in argv))
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    proj = Path(args[0] if args else os.getcwd()).expanduser()
    if not proj.is_dir():
        print(f"project dir not found: {proj}", file=sys.stderr)
        sys.exit(2)
    inv = build_inventory(proj)
    if "--json" in flags:
        json.dump(inv, sys.stdout, indent=1, default=sorted)
    else:
        print(markdown(inv, brief="--brief" in flags), end="")


if __name__ == "__main__":
    main()
