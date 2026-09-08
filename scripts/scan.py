#!/usr/bin/env python3
"""loadout scanner — inventory skills/plugins/hooks/commands/agents/MCP across agent harnesses.

Facts only: names, SKILL.md descriptions, registered hooks, enabled/disabled state.
Hook-command masking is best-effort for secret-shaped flags and env assignments;
positional secrets, unquoted edge cases and arbitrary names can still appear —
treat scan output as sensitive. Stdlib only; Python 3.9+; Windows/macOS/Linux.

Usage:
  python scan.py [--json] [--brief] [project_dir]
  python scan.py --check [--hosts a,b|all]          # compare installed copies to this source
  python scan.py --self-install [--hosts a,b|all]   # copy this skill into harness skills dirs
"""
import hashlib
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
SKILL_FILES = ["SKILL.md", "README.md", "LICENSE", "scripts/scan.py", "scripts/apply.py", "scripts/gate.py",
               "scripts/gate_codex.py", "scripts/gate_dsh.py", "scripts/gate_dsh.mjs", "scripts/check_notes.py",
               "scripts/skill_audit.py", "references/skill-notes.md"]
# references/skill-assessments.json is deliberately absent: it is audit state generated on one
# machine and its records name that machine's absolute paths, so copying it into another install
# would carry stale identities rather than knowledge. Each install regenerates it by auditing.


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


def read_json_strict(path):
    """(data, present), raising instead of collapsing a failed read into absence.

    `load_json` answers "what can the display show", so it returns None for a file that is
    missing, unreadable and malformed alike. A caller that must not turn a failure into an empty
    inventory needs those told apart: only a genuinely absent file is (None, False), and anything
    else raises. Absence is established here, never inferred from a failed read."""
    p = Path(path).expanduser()
    try:
        raw = p.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None, False          # established absence: the path is not there
    except OSError as e:
        raise OSError(f"cannot read {p}: {e}") from e
    try:
        # Strict, unlike load_json: errors="replace" turns an undecodable byte into U+FFFD and
        # the file then PARSES. The damaged key no longer matches the one it was written to
        # disable, so a corrupt settings file resolves to "enabled" - the exact default this
        # reader exists to refuse. An encoding failure is a read failure.
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{p} is not valid UTF-8: {e}") from e
    try:
        return json.loads(text), True
    except ValueError as e:
        raise ValueError(f"malformed JSON in {p}: {e}") from e


def stat_strict(p, what="path"):
    """(stat_result, present), raising instead of collapsing a failed stat into absence.

    `Path.is_dir()` cannot carry this distinction. Since 3.14 it delegates to os.path.isdir,
    which is the C accelerator nt._path_isdir on Windows: it asks the OS and answers False for
    an inaccessible path exactly as it does for a missing one, and never raises. A caller that
    publishes state reads that False as "this root is gone" and deletes the records it held.
    Only FileNotFoundError/NotADirectoryError establish absence here; every other OSError is
    the caller's to handle."""
    try:
        return os.stat(p), True
    except (FileNotFoundError, NotADirectoryError):
        return None, False
    except OSError as e:
        raise OSError(f"cannot stat {what} {p}: {e}") from e


def is_dir_strict(p, what="directory"):
    """True/False for "is a directory", where False means established absence or a non-directory
    and an inaccessible path raises instead of answering."""
    return dir_role_strict(p, what) == ROOT_PRESENT


# what a path whose ROLE is "a directory" turned out to be
ROOT_PRESENT, ROOT_ABSENT, ROOT_NOT_DIR = "present", "absent", "not-a-directory"


def dir_role_strict(p, what="root"):
    """PRESENT / ABSENT / NOT_A_DIRECTORY for a path whose ROLE is to be a directory.

    `is_dir_strict` answers one bit, and a caller that publishes state reads its False as absence.
    For an ordinary entry met inside a directory that is right - a stray file is not an error. For
    a root someone CONFIGURED it is not: "what you pointed me at is not a directory" and "it is
    not there" are different facts, and only the second is the normal absence of an optional
    harness. Reporting the first as an empty inventory deletes every record the root held, at
    exit 0.

    The exception class cannot carry the difference. Under a regular file Windows raises
    FileNotFoundError with winerror 3 (ERROR_PATH_NOT_FOUND), never the POSIX NotADirectoryError,
    and winerror 3 is also what a merely missing intermediate DIRECTORY gives. So the role is
    decided by stat-ing THIS path, and callers name the anchor they configured rather than
    walking ancestry - which would refuse a project that merely holds a file called `.agents`."""
    st, present = stat_strict(p, what)
    if not present:
        return ROOT_ABSENT
    return ROOT_PRESENT if stat.S_ISDIR(st.st_mode) else ROOT_NOT_DIR


def anchor_role_strict(p, what="anchor"):
    """dir_role_strict for a path someone explicitly NAMED, where apparent absence may be a
    non-directory ANCESTOR rather than a missing anchor.

    Stat-ing this path alone cannot tell "you pointed me underneath a file" from "it is not
    there": below a regular file Windows raises FileNotFoundError with winerror 3, the same class
    and the same winerror a merely missing intermediate directory gives, so substituting an
    exception class decides nothing - the OS never produces NotADirectoryError there. The only
    evidence is the ancestry. On apparent absence, walk up to the nearest component that EXISTS
    and ask what it is: a file means the anchor's structure is wrong, while a directory - or
    nothing existing at all - is the ordinary absence of an optional anchor.

    Only anchors get this. `dir_role_strict` stays ancestry-blind for the convention directories
    underneath them, so a project that merely holds a file called `.agents` is still absent."""
    role = dir_role_strict(p, what)
    if role != ROOT_ABSENT:
        return role
    for parent in Path(p).parents:
        st, present = stat_strict(parent, what)
        if not present:
            continue
        return ROOT_ABSENT if stat.S_ISDIR(st.st_mode) else ROOT_NOT_DIR
    return ROOT_ABSENT


def is_file_strict(p, what="file"):
    st, present = stat_strict(p, what)
    return present and stat.S_ISREG(st.st_mode)


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


def description_from(fm):
    """Description value out of an already-extracted frontmatter block, or "" when there is none.

    Handles plain multi-line scalars and YAML block scalars (>, |). Split out from desc_of so a
    caller that has already decoded the bytes itself - strictly, rather than with the
    errors="replace" desc_of uses - reads the description through this same grammar instead of a
    second, subtly different one."""
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


def desc_of(path):
    """Full description from a SKILL.md dir or a bare .md file's frontmatter."""
    md = path / "SKILL.md" if path.is_dir() else path
    if not md.is_file() or md.suffix.lower() != ".md":
        return ""
    try:
        with open(md, "rb") as f:
            head = f.read(MAX_HEAD).decode("utf-8", errors="replace")
    except OSError:
        return ""
    return description_from(frontmatter(head))


def bundle_skills(p, strict=False):
    """A plugin bundle dropped into an asset dir carries no SKILL.md of its own; its skills sit
    one level down. Returns [(name, dir)] as <bundle>:<skill>, matching plugin-skill naming, or
    [] when this is an ordinary skill dir. Listing the bundle name instead would give the audit
    a name it cannot classify and the user one they cannot invoke.

    Returns None when this is not a bundle at all, so the caller lists it normally. A bundle
    that cannot be read returns [], which drops it from the listing: one unreadable directory
    must not abort an inventory spanning dozens of roots, and emitting the bare bundle name
    would put back the unclassifiable, uninvocable entry this function exists to remove.
    `strict=True` raises instead: for the audit, a dropped bundle is not a cosmetic gap in a
    listing, it is an installed identity silently missing from published state.

    Runs for every entry of every asset dir, so it stays two stat calls on the common path.
    A bundle sharing its name with an installed plugin would produce the same <bundle>:<skill>
    string in both `skills` and `plugin-skills`; left unguarded, as it needs a name collision
    at both levels to occur."""
    # Under strict, every one of these stats is a place an inaccessible entry could otherwise
    # answer "not a bundle" / "no SKILL.md" and drop an installed identity out of the audit
    # without a word. A strict wrapper one level up does not help if the child helper it calls
    # is the tolerant one.
    isdir = (lambda q: is_dir_strict(q, "bundle entry")) if strict else Path.is_dir
    isfile = (lambda q: is_file_strict(q, "bundle body")) if strict else Path.is_file
    if not isdir(p) or isfile(p / "SKILL.md"):
        return None
    nested = p / "skills"
    if not isdir(nested):
        return None
    try:
        children = sorted(nested.iterdir())
    except OSError as e:
        if strict:
            raise OSError(f"cannot list bundle {nested}: {e}") from e
        return []
    return [(f"{p.name}:{c.name}", c) for c in children
            if isdir(c) and isfile(c / "SKILL.md")]


def scan_dir(d):
    """List entries in an asset dir: [{name, desc[, link]}]. Skips dotfiles."""
    if not d.is_dir():
        return None
    out = []
    for p in sorted(d.iterdir()):
        if p.name.startswith(".") or p.name in INFRA_NAMES:
            continue
        inner = bundle_skills(p)
        if inner is not None:
            for n, c in inner:
                e = {"name": n, "desc": desc_of(c)}
                # The nested skill, or the bundle around it, can be a symlink into a shared
                # pool; every other entry reports its target, so these must too.
                tgt = link_target(c) or link_target(p)
                if tgt:
                    e["link"] = tgt
                out.append(e)
            continue
        e = {"name": p.stem if p.is_file() else p.name, "desc": desc_of(p)}
        tgt = link_target(p)
        if tgt:
            e["link"] = tgt
        out.append(e)
    return out


# Best-effort only: secret-shaped flag/env values, not positional or arbitrary names.
# Unquoted values stop at whitespace or ;|& — not a shell parser. TOKENIZER is not TOKEN.
_SECRET_WORD = r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|AUTH[_-]?KEY|CREDENTIAL|KEY)"
_SECRET_VAL = r"(?:'[^']*'|\"[^\"]*\"|[^\s;|&]+)"
_SECRET_FLAG = re.compile(
    r"(?i)(--(?:[A-Za-z0-9]+[-_])*(?:token|secret|password|passwd|api[-_]?key|"
    r"auth(?:[-_]?key)?|credential|key)s?)"
    r"(?:(=)(" + _SECRET_VAL + r")|(\s+)(" + _SECRET_VAL + r"))")
_SECRET_ENV = re.compile(
    r"(?i)(?<!\S)(" + _SECRET_WORD + r"|[A-Za-z_][A-Za-z0-9_]*_" + _SECRET_WORD + r")=("
    + _SECRET_VAL + r")")


def _redact_value(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[0] + "***" + v[-1]
    return "***"


def short_cmd(cmd):
    s = re.sub(r"\s+", " ", str(cmd)).strip()
    s = _SECRET_FLAG.sub(lambda m: m.group(1) + (m.group(2) or m.group(4)) + _redact_value(m.group(3) or m.group(5)), s)
    s = _SECRET_ENV.sub(lambda m: m.group(1) + "=" + _redact_value(m.group(2)), s)
    return s[:80]


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


def _check_settings_maps(data, where):
    """Validate the containers and values a strict reader is about to consume.

    Checking only that the document is an object leaves two shapes through. An empty wrong-type
    value collapses silently: `data.get("enabledPlugins") or {}` reads `[]` as "no plugins are
    enabled", which is also what a correct empty map looks like. A NONEMPTY wrong-type value
    escapes as a traceback out of `.update()` or `.items()` instead of the deterministic refusal
    the boundary promises. Both are decided here, once, before any consumer indexes them.

    `enabledPlugins` values are booleans: `bool(v)` would read the string "false" as enabled,
    which is the same fail-open direction as an undecodable byte. An unsupported value is
    refused rather than given a meaning the host never wrote."""
    for key in ("enabledPlugins", "skillOverrides"):
        v = data.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            raise ValueError(f"settings {key!r} is not an object: {where}")
        for name in v:
            if not isinstance(name, str) or not name:
                raise ValueError(f"settings {key!r} has an empty key: {where}")
    for name, on in (data.get("enabledPlugins") or {}).items():
        if not isinstance(on, bool):
            raise ValueError(f"settings enabledPlugins[{name!r}] must be true or false, "
                             f"got {on!r}: {where}")


def settings_stack(rootp, proj, strict=False):
    """[(label, data)] for each Claude Code settings file that exists, in precedence order.

    Split out of claude_layer so another reader can resolve the same enabled/disabled facts from
    the same four files rather than inventing a second, divergent notion of "enabled".

    `strict=True` tells an absent settings file - normal, most machines have two of the four -
    apart from one that exists and could not be read or parsed. The display can treat both as
    "no settings"; a reader deciding whether a skill is enabled cannot, because doing so silently
    resolves an unreadable disable to the default, which is enabled."""
    out = []
    for label, p in (("~/.claude/settings.json", rootp / "settings.json"),
                     ("~/.claude/settings.local.json", rootp / "settings.local.json"),
                     (".claude/settings.json", proj / ".claude" / "settings.json"),
                     (".claude/settings.local.json", proj / ".claude" / "settings.local.json")):
        if strict:
            data, present = read_json_strict(p)
            if present and not isinstance(data, dict):
                raise ValueError(f"settings file is not an object: {Path(p).expanduser()}")
            if present:
                _check_settings_maps(data, Path(p).expanduser())
        else:
            data = load_json(p)
        if isinstance(data, dict):
            out.append((label, data))
    return out


def installed_plugins(rootp, enabled, strict=False):
    """[(name, installPath or None, on, version)] from the Claude Code plugin manifest.

    `enabled` is the merged enabledPlugins map from settings_stack. Split out so the skill audit
    reaches plugin-provided skills through the scanner's own manifest reading, canonical
    `plugin:skill` naming and enabled state rather than a second registry.

    `strict=True` refuses a manifest that exists but does not parse. Reading it as `{}` is the
    same byte sequence as "no plugins are installed", and an audit cannot tell those apart after
    the fact - it just publishes an inventory with every plugin skill missing."""
    mf = rootp / "plugins" / "installed_plugins.json"
    if strict:
        data, present = read_json_strict(mf)
        if not present:
            data = {}
        elif not isinstance(data, dict):
            raise ValueError(f"plugin manifest is not an object: {mf}")
        plugins = data.get("plugins")
        if plugins is None:
            plugins = {}
        # `plugins` as a list is the shape that reached `.items()` and escaped as an
        # AttributeError; as an EMPTY list it is worse, because `or {}` reads it as "no
        # plugins installed" and the audit publishes an inventory with every plugin skill
        # deleted out of it. Neither is an installed-plugin manifest.
        if not isinstance(plugins, dict):
            raise ValueError(f"plugin manifest 'plugins' is not an object: {mf}")
        for key, recs in plugins.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"plugin manifest has an empty plugin key: {mf}")
            # An entry that is not a nonempty list of objects currently degrades to `{}`,
            # which is an installed identity with no install path - indistinguishable from a
            # plugin that legitimately provides no directory.
            if not isinstance(recs, list) or not recs or not isinstance(recs[0], dict):
                raise ValueError(f"plugin manifest entry {key!r} must be a non-empty list of "
                                 f"objects: {mf}")
            ip = recs[0].get("installPath")
            if ip is not None and (not isinstance(ip, str) or not ip):
                raise ValueError(f"plugin manifest entry {key!r} has an unusable "
                                 f"installPath: {mf}")
    else:
        data = load_json(mf) or {}
        plugins = data.get("plugins") or {}
    out = []
    for key, recs in sorted(plugins.items()):
        rec = recs[0] if isinstance(recs, list) and recs and isinstance(recs[0], dict) else {}
        out.append((key.split("@")[0], rec.get("installPath"),
                    bool(enabled.get(key, True)), rec.get("version", "")))
    return out


def claude_layer(rootp, proj):
    """Plugins (+ their skills/agents/commands/hooks/MCP), registered hooks, and
    enabled/disabled state from the settings stack: user, user-local, project, project-local."""
    stack = settings_stack(rootp, proj)
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
    for name, ip, on, version in installed_plugins(rootp, enabled):
        ent = {"name": name, "desc": version}
        if not on:
            ent["status"] = "off"
        layer["plugins"].append(ent)
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


def discovery_key(p):
    """The comparison key for a discovery root: one lexical string, no filesystem read.

    `norm` cannot serve here. It rewrites a literal backslash into a separator, so on a POSIX
    filesystem - where a backslash is an ordinary filename character - `~/.config/opencode` and a
    genuinely distinct `~/.config\\opencode` reduce to the same string. A known set holding either
    one then excludes BOTH, and the installed root nobody ever declared known is dropped before
    anything inspects it; the audit reads that as those skills having been uninstalled. It also
    strips trailing separators, so a filesystem root stops naming a directory at all.

    The key is `Path(path).as_posix()` over an already-expanded path. It preserves exactly the
    distinctions `norm` erased, not every raw character of the spelling handed in: `Path` has
    already dropped a trailing separator and a redundant `.` before `as_posix` spells the
    separators one way. That is what a comparison of already-expanded paths needs and all it
    needs. Nothing here resolves, absolutises, case-folds or touches disk, so two spellings of one
    physical directory are still two keys - the same lexical limit the labels below carry. `norm`
    itself is unchanged: the settings/project comparison it serves is a different question about a
    different domain, and the three known-set producers agree on this REPRESENTATION without
    their deliberately different populations being unified."""
    return Path(p).as_posix()


def _dynamic_candidates(home, strict):
    """<parent>/<entry>/skills for each of the two parents dynamic discovery walks INTO by name.

    Replaces `home.glob(".*/skills")`, which cannot report what it could not read: pathlib's
    globber swallows every OSError its own scandir raises, so an unreadable home and an empty one
    are one answer, and a ~/.config present as a REGULAR FILE reports "no dynamic roots" exactly
    as an absent one does. Neither is an empty inventory, and a strict caller publishes over one.
    Catching it here is catching it at the real parent/list boundary; a wrapper around glob cannot
    see the errors glob already swallowed.

    The two parents carry a directory ROLE; the entries inside them do not, and that distinction
    is the whole of the rule. ~/.gitconfig is a file and is simply not a harness root, so it is
    skipped exactly as the glob skipped it, while a ~/.config that is a file disables discovery of
    every harness underneath it. Only an INACCESSIBLE entry raises - that one is genuinely
    unknown - and the terminal <entry>/skills is left to the caller's own strict check rather than
    being decided twice.

    The tolerant display keeps the old suppression: it only has to show what it can see. The two
    modes differ in what they RAISE, never in what they find."""
    out = []
    for base, dotted in ((home, True), (home / ".config", False)):
        if strict:
            role = dir_role_strict(base, "discovery parent")
            if role == ROOT_NOT_DIR:
                raise NotADirectoryError(f"discovery parent {base} is not a directory")
            if role == ROOT_ABSENT:
                continue  # an absent ~/.config is normal
        try:
            with os.scandir(base) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            if strict:
                raise OSError(f"cannot enumerate discovery parent {base}: {e}") from e
            continue
        for e in entries:
            if dotted and not e.name.startswith("."):
                continue
            try:
                if not e.is_dir():
                    continue
            except OSError as err:
                if strict:
                    raise OSError(f"cannot stat discovery entry {e.path}: {err}") from err
                continue
            out.append(Path(e.path) / "skills")
    return out


def _display_base(root, home):
    """The legacy public name for a discovered root: home-relative when it can be, else the path.

    Kept exactly as it was, lossy `norm` included, because this is presentation and its ordinary
    output - `.someagent`, `.pi/agent`, `.config/agents` - is what a user types into `--hosts`.
    It is only a CANDIDATE now; `_labelled` decides whether it survives. The one repair is the
    anchor, where the home-relative branch misses and `norm` then strips the whole path down to
    the empty string, naming nothing at all. Only an EMPTY base is repaired. A nonempty legacy
    label is kept exactly as it is, including the ambiguous `C:` a Windows drive root produces -
    it names its own stored root through the mapping below, and renaming it would change a name
    users already type."""
    try:
        base = norm(root.relative_to(home))
    except ValueError:
        base = norm(root)
    return base or discovery_key(root)


def _collision_label(base, key, occupied):
    """A deterministic label for one member of a group whose display base is already taken.

    Derived from the root key alone, so the same inventory always maps the same way and arrival
    order never picks a winner. The digest is SHORT because this is a name someone types, and a
    short prefix is exactly what can already be occupied - by another key that agrees on it, or
    by an ordinary label that merely looks generated. So it widens, and past the whole digest it
    counts: the candidate sequence is unbounded and `occupied` is finite, so this terminates even
    if two distinct keys were to agree on all 64 hex digits. No comma appears in the suffix, so
    suffixing never introduces a split of its own in the comma-delimited selector list. That is
    not a guarantee the whole label is one token: the suffix is never selected on its own, and a
    base that already carries a comma is split by the unchanged grammar either way."""
    digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
    n = 8
    cand = f"{base}#{digest[:n]}"
    while cand in occupied:
        n += 1
        cand = f"{base}#{digest[:n]}" if n <= 64 else f"{base}#{digest}~{n - 64}"
    return cand


def _labelled(records, home):
    """{public label: record} from {discovery key: record}, losing no key.

    Two passes, because doing it in one is what lost roots. A label used to be assigned the moment
    its root was found, so two distinct roots sharing a display base overwrote each other and the
    later one silently replaced the earlier - in the JSON, in the rendering, in `--hosts` and in
    the audit's own candidate list. Collecting first makes a collision VISIBLE before any name is
    handed out. `as_posix` on the root does not remove the collision: an `XDG_CONFIG_HOME` given
    as the relative `.config` contributes the extra root `.config/agents` while dynamic discovery
    contributes `<home>/.config/agents`, two distinct keys with one display base.

    The whole occupied namespace is reserved before allocation - the fixed host keys and the
    shared pool, which a discovered label would otherwise overwrite through `inv["hosts"].update`
    and `install_targets`, and every base that is already unique, which must stay byte-identical
    to what it is today. Only what actually collides is renamed, and the result is ordered by
    label so the mapping does not depend on enumeration order either."""
    bases = {}
    for key in sorted(records):
        bases.setdefault(_display_base(Path(records[key]["root"]), home), []).append(key)

    occupied = set(HOSTS) | {SHARED}
    keep = {keys[0]: base for base, keys in bases.items()
            if len(keys) == 1 and base not in occupied}
    occupied.update(keep.values())

    out = {}
    for base, keys in sorted(bases.items()):
        for key in keys:
            label = keep.get(key) or _collision_label(base, key, occupied)
            occupied.add(label)
            out[label] = records[key]
    return dict(sorted(out.items()))


def discover_roots(known, strict=False):
    """Any other <root>/skills dir holding SKILL.md children, e.g. the ~60 agents the skills CLI
    installs into. Names only; never a self-install target unless --hosts all.

    `strict=True` raises on a root that exists but cannot be enumerated, rather than recording
    it as holding nothing and therefore dropping it from the result entirely. That now covers the
    PARENTS the walk descends through as well as the roots themselves."""
    home = Path.home()
    cands = _dynamic_candidates(home, strict)
    cands += [Path(p).expanduser() for p in EXTRA_SKILL_ROOTS]
    found = {}

    def terminal_is_root(q):
        """The caller's own strict check on the terminal <entry>/skills, which
        `_dynamic_candidates` deliberately leaves undecided.

        `is_dir_strict` answers one bit and collapses the two non-directory answers into it. The
        terminal carries a directory ROLE - it is the thing being discovered as a root - so a
        regular file sitting where a harness's skills directory belongs is a structural mistake
        about that root, not the ordinary absence of an optional one. Answering False for it
        drops the root silently and the audit reads that as those skills having been uninstalled.
        Only the terminal is judged this way: the ENTRIES walked over to reach it carry no such
        role, so ~/.gitconfig and every other file beneath home is still simply skipped."""
        role = dir_role_strict(q, "discovered root")
        if role == ROOT_NOT_DIR:
            raise NotADirectoryError(f"discovered root {q} is not a directory")
        return role == ROOT_PRESENT

    isdir = terminal_is_root if strict else Path.is_dir
    isfile = (lambda q: is_file_strict(q, "discovered body")) if strict else Path.is_file
    for d in cands:
        root = d.parent
        # Same rule as the enumeration below, one call earlier: a dynamic root that cannot be
        # stat-ed is not a root that holds nothing. Answering False here drops it from the
        # result entirely, which the audit then reads as those skills having been uninstalled.
        if not isdir(d) or discovery_key(root) in known:
            continue
        try:
            entries = [{"name": p.name, "desc": ""} for p in sorted(d.iterdir())
                       if not p.name.startswith(".") and isfile(p / "SKILL.md")]
        except OSError as e:
            if strict:
                raise OSError(f"cannot list discovered root {d}: {e}") from e
            entries = []
        if not entries:
            continue
        found[discovery_key(root)] = {"root": str(root), "assets": {"skills": entries},
                                      "discovered": True}
    return _labelled(found, home)


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
            # A valid discovered label was already accepted two lines below; this message was
            # never what rejected one. What it omitted was any sign those alternatives existed,
            # so an operator who MISTYPED a discovered name saw only the fixed table.
            print(f"unknown host(s): {', '.join(unknown)}; "
                  f"known: {', '.join(sorted(set(table) | set(discovered)))}", file=sys.stderr)
            return None
        return {w: (table.get(w) or Path(discovered[w]["root"]) / "skills") for w in wanted}
    return present


def self_install(hosts_arg, check_only):
    src = Path(__file__).resolve().parent.parent
    if not (src / "SKILL.md").is_file():
        print(f"self-install: no SKILL.md next to {src}", file=sys.stderr)
        return 1
    known = {discovery_key(Path(r).expanduser()) for r, _ in HOSTS.values()} \
        | {discovery_key(Path(SHARED_ROOT).expanduser())}
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
        for rel in SKILL_FILES:
            if (src / rel).is_file():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
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
        known.add(discovery_key(rootp))
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
    known.add(discovery_key(shared))
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
    if "--help" in argv:
        print(__doc__)
        return
    known = {"--json", "--brief", "--check", "--self-install", "--hosts"}
    hosts_arg = None
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-"):  # any dash token, so -x cannot slip through as the project path
            if a not in known:
                print(f"scan: unknown option {a}", file=sys.stderr)
                sys.exit(2)
            if a == "--hosts":
                if hosts_arg is not None:
                    print("scan: --hosts given more than once", file=sys.stderr)
                    sys.exit(2)
                if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                    print("scan: --hosts needs a value", file=sys.stderr)
                    sys.exit(2)
                hosts_arg = argv[i + 1]
                # an empty/blank list would fall through install_targets as "no --hosts given"
                # and silently install into every present default host
                if not [w for w in hosts_arg.split(",") if w.strip()]:
                    print("scan: --hosts needs at least one host name", file=sys.stderr)
                    sys.exit(2)
                i += 2
                continue
            i += 1
            continue
        args.append(a)
        i += 1
    # Both checks run before either dispatch. Documented usage is one optional [project_dir] for a
    # scan and none at all for the two install modes, and neither mode has anywhere to put a second
    # path: taking args[0] and dropping the rest reads a mistyped command as a narrower one that
    # was never asked for.
    mode = "--check" if "--check" in argv else "--self-install" if "--self-install" in argv else None
    if mode and args:
        print(f"scan: {mode} takes no project directory (got {args[0]}); "
              f"it works on installed copies -- choose hosts with --hosts a,b|all", file=sys.stderr)
        sys.exit(2)
    if len(args) > 1:  # apply.py already rejects its extras; don't silently drop these
        print(f"scan: expected one project directory, got {len(args)}", file=sys.stderr)
        sys.exit(2)
    if mode:
        sys.exit(self_install(hosts_arg, mode == "--check"))
    flags = {a for a in argv if a.startswith("--")}
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
