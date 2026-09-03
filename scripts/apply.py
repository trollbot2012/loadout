#!/usr/bin/env python3
"""loadout apply — persist the accepted loadout into the project's agent instruction files, idempotently.

Usage: python apply.py <project_dir> --host <host> [--loadout LOADOUT.md] [--no-enforce]

Reads the '## Accepted' section of LOADOUT.md (lines like '- <stage>: `<skill>`'), builds the
'## Loadout' block and upserts it (replace if present, else append, else create) into:
  - AGENTS.md (read natively by Codex, Cursor, OpenCode, Copilot, ...)
  - the running host's native file: CLAUDE.md / GEMINI.md / QWEN.md. Claude Code does not read
    AGENTS.md, so a missing CLAUDE.md is created with an '@AGENTS.md' import line.
  - any other native file that already exists in the project (keeps every harness consistent).
  - on claude-code, the enforcement gate (scripts/gate.py) as PreToolUse + Stop hooks in
    .claude/settings.local.json, unless --no-enforce. Hooks load at the next session.
  - on codex, the same gate in the user-level ~/.codex/hooks.json ($CODEX_HOME honoured):
    project-level Codex hooks need a trusted project, and the gate is a no-op without LOADOUT.md.
  - on deepseek/dsh, the gate as a loader patch entry in the user-level $DSH_HOME/cordis.patch.yml
    (default ~/.dsh): dsh has no per-repo plugin config, and that patch layer is applied after every
    profile's own, so the one entry covers headless, tui and web alike.
Re-runs replace the existing section; content before/after it is preserved. Stdlib only.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

NATIVE = {"claude-code": "CLAUDE.md", "gemini": "GEMINI.md", "qwen": "QWEN.md"}
GATE = Path(__file__).resolve().parent / "gate.py"
DSH_PLUGIN = Path(__file__).resolve().parent / "gate_dsh.mjs"
GATE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash|EnterWorktree|mcp__.*"
SETTINGS_LOCAL = ".claude/settings.local.json"
CODEX_HOOKS = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "hooks.json"
DSH_PATCH = Path(os.environ.get("DSH_HOME") or "~/.dsh").expanduser() / "cordis.patch.yml"
VALUE_FLAGS = {"--host", "--loadout"}  # CLI flags that consume the next token; gate.py validates against this
SECTION_RE = re.compile(r"^## Loadout\b.*?(?=^## |\Z)", re.M | re.S)
ACCEPTED_RE = re.compile(r"^## Accepted\b.*?(?=^## |\Z)", re.M | re.S)
IMPORT_RE = re.compile(r"^@AGENTS\.md\s*$", re.M)
LINE_RE = re.compile(r"^\s*[-*]\s*([^:`]+?)\s*:\s*`?([^`\s]+)`?", re.M)


def gate_hooks():
    """Hook registrations for this machine's copy of gate.py (hence settings.local.json)."""
    def cmd(mode):
        return f'"{sys.executable}" "{GATE}" {mode}'
    return {"PreToolUse": [{"matcher": GATE_MATCHER, "hooks": [{"type": "command", "command": cmd("pre")}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("stop")}]}]}


def codex_gate_hooks():
    """Same gate for Codex CLI, in its Claude-shaped hooks.json. No PreToolUse matcher: Codex tool names
    differ from Claude's, so the gate decides by name itself. Codex runs hooks through PowerShell on
    Windows, where a quoted path at command position needs the call operator; bash takes the plain form.
    Event names are keys of the map returned here; the caller nests it under the file's root "hooks"
    object -- Codex's schema accepts only "description"/"hooks" at the root. commandWindows (camelCase)
    is the canonical field Codex itself writes; codex_hook_hash() still tolerates the old snake_case
    form when reading, but we only ever write the canonical one."""
    def entry(mode):
        posix = f'"{sys.executable}" "{GATE}" {mode} --host codex'
        return {"hooks": [{"type": "command", "command": posix, "commandWindows": "& " + posix, "timeout": 20}]}
    return {"PreToolUse": [entry("pre")], "Stop": [entry("stop")]}


def _all_gate_entries(entries):
    """True when every handler across every group in `entries` is one of this repo's own gate hooks
    (identified the same way upsert_hooks does: the "gate.py" command substring). An entries list with
    no handlers at all (junk/empty) counts as all-gate too -- there is nothing foreign to protect."""
    for group in entries:
        if not isinstance(group, dict):
            return False
        for h in group.get("hooks", []):
            if not isinstance(h, dict) or "gate.py" not in h.get("command", ""):
                return False
    return True


def _migrate_stray_root_events(data):
    """Codex's hooks.json root accepts ONLY "description"/"hooks" -- any other root key fails to load
    every hook in the file, not just an invalid one. An earlier version of this writer put the gate's
    event entries straight at the root; move any such stray root key that holds ONLY our own gate
    entries under "hooks", deleting the now-empty root key. A stray root key holding any entry that is
    NOT ours is left completely untouched (it is not our data to move or delete) and its name is
    returned so the caller can report it. Mutates `data` in place; returns (foreign_keys, moved_any)."""
    foreign = []
    moved = False
    for key in [k for k in data if k not in ("description", "hooks")]:
        entries = data[key]
        if not isinstance(entries, list):
            continue
        if _all_gate_entries(entries):
            if entries:
                data.setdefault("hooks", {}).setdefault(key, []).extend(entries)
            del data[key]
            moved = True
        else:
            foreign.append(key)
    return foreign, moved


def load_settings(path):
    """Parsed hooks JSON, or {} when absent. Validated up front so a bad file fails before any write."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ValueError(f"{path} is not valid JSON; fix it or pass --no-enforce")


def upsert_hooks(hooks, wanted):
    """Replace our own gate hooks under each event in `hooks` (mutated) with `wanted`; every other hook
    survives, including siblings inside the same entry. Returns whether anything changed."""
    changed = False
    for event, entries in wanted.items():
        current = hooks.get(event, [])
        kept = []
        for e in current:
            inner = [h for h in e.get("hooks", []) if "gate.py" not in h.get("command", "")]
            if inner:
                kept.append({**e, "hooks": inner} if len(inner) != len(e.get("hooks", [])) else e)
        new = kept + entries
        if new != current:
            hooks[event] = new
            changed = True
    return changed


def write_hooks(path, data, existed, changed):
    if existed and not changed:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_lf(path, json.dumps(data, indent=2) + "\n")
    return "updated" if existed else "created"


def register_gate(project, data=None):
    """Upsert the gate hooks into <project>/.claude/settings.local.json. Returns the action."""
    path = Path(project) / SETTINGS_LOCAL
    existed = path.is_file()
    data = load_settings(path) if data is None else data
    return write_hooks(path, data, existed, upsert_hooks(data.setdefault("hooks", {}), gate_hooks()))


def register_codex_gate(path=None, data=None):
    """Upsert the gate hooks into the user-level Codex hooks.json, nested under the root "hooks" object
    (Codex's schema; see _migrate_stray_root_events for why root-level event keys are never written)."""
    path = Path(path or CODEX_HOOKS)
    existed = path.is_file()
    data = load_settings(path) if data is None else data
    foreign, moved = _migrate_stray_root_events(data)
    changed = upsert_hooks(data.setdefault("hooks", {}), codex_gate_hooks()) or moved
    action = write_hooks(path, data, existed, changed)
    if foreign:
        action += f"; WARNING: root-level {', '.join(sorted(foreign))} left as-is (holds non-gate entries)"
    return action


# Codex skips hooks it has not been told to trust, silently. Trust is a per-handler hash in
# config.toml ([hooks.state.'<file>:<event>:<group>:<handler>'] trusted_hash = "sha256:..."),
# reproduced here from codex-rs/hooks/src/engine/discovery.rs hook_hash and verified against
# every entry on a real machine, so apply can grant it without the operator opening /hooks.
CODEX_CONFIG = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"
_CODEX_EVENT_LABEL = {"PreToolUse": "pre_tool_use", "PermissionRequest": "permission_request",
                      "PostToolUse": "post_tool_use", "PreCompact": "pre_compact", "PostCompact": "post_compact",
                      "SessionStart": "session_start", "SessionEnd": "session_end",
                      "UserPromptSubmit": "user_prompt_submit", "SubagentStart": "subagent_start",
                      "SubagentStop": "subagent_stop", "Stop": "stop", "Interrupt": "interrupt"}
_CODEX_NO_MATCHER = {"user_prompt_submit", "stop", "interrupt"}
_CODEX_CTX_LIMIT_EVENTS = {"pre_tool_use", "post_tool_use", "session_start", "user_prompt_submit", "subagent_start"}


def codex_hook_hash(event, group, handler, windows=None):
    """Codex's trusted_hash for one command handler: canonical JSON of the normalised identity, sha256."""
    windows = sys.platform == "win32" if windows is None else windows
    cmd = (handler.get("commandWindows") or handler.get("command_windows")) if windows else None
    cmd = cmd or handler.get("command", "")
    t = handler.get("timeout")
    if event in ("session_end", "interrupt"):
        t = min(max(1 if t is None else t, 1), 3)
    else:
        t = max(600 if t is None else t, 1)
    hook = {"type": "command", "command": cmd, "timeout": t, "async": bool(handler.get("async", False))}
    if handler.get("statusMessage") is not None:
        hook["statusMessage"] = handler["statusMessage"]
    acl = handler.get("additionalContextLimit")
    if event in _CODEX_CTX_LIMIT_EVENTS and acl is not None and acl != 2500:
        hook["additionalContextLimit"] = acl
    ident = {"event_name": event, "hooks": [hook]}
    if group.get("matcher") is not None and event not in _CODEX_NO_MATCHER:
        ident["matcher"] = group["matcher"]
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def trust_codex_gate(hooks_path=None, config_path=None):
    """Write trusted_hash entries for our gate handlers into config.toml. Returns trusted|unchanged.
    The file is edited textually (stdlib has no TOML writer): only our own [hooks.state.'<key>']
    sections are replaced or appended; every other line stays as it is."""
    hooks_path = Path(hooks_path or CODEX_HOOKS)
    config_path = Path(config_path or CODEX_CONFIG)
    data = load_settings(hooks_path)
    events = data.get("hooks", {}) if isinstance(data.get("hooks"), dict) else {}
    wanted = {}
    for json_key, label in _CODEX_EVENT_LABEL.items():
        for gi, group in enumerate(events.get(json_key, [])):
            for hi, handler in enumerate(group.get("hooks", [])):
                if "gate.py" in handler.get("command", ""):
                    wanted[f"{hooks_path}:{label}:{gi}:{hi}"] = codex_hook_hash(label, group, handler)
    text = config_path.read_text(encoding="utf-8") if config_path.is_file() else "[hooks.state]\n"
    nl = "\r\n" if "\r\n" in text else "\n"
    changed = False
    for key, digest in wanted.items():
        header = f"[hooks.state.'{key}']"
        # the header at line start, then every following non-blank line that is not a table header:
        # the whole section is replaced, so a stray comment can never leave two trusted_hash keys
        section = re.compile(r"^" + re.escape(header) + r"[ \t]*(?:\r?\n(?!\[)(?![ \t]*\r?$)[^\r\n]*)*", re.M)
        replacement = f"{header}{nl}trusted_hash = \"{digest}\""
        m = section.search(text)
        if m:
            if m.group(0) != replacement:
                text = text[:m.start()] + replacement + text[m.end():]
                changed = True
        else:
            text = text.rstrip("\r\n") + nl + nl + replacement + nl
            changed = True
    if not changed and config_path.is_file():
        return "unchanged"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
    return "trusted"


# dsh registration is user-level: there is no per-repo plugin config, and the loader applies
# cordis.patch.yml after every profile's own patch layer, so one entry covers every profile. The
# stdlib has no YAML writer, so the file is edited line by line exactly like config.toml above:
# only our own entry is replaced or appended; every other entry and comment keeps its bytes and order.
DSH_HEADER = "# dsh loader patches, applied after every profile's own patch layer (written by loadout apply).\n"
_DSH_TOP = re.compile(r"-[ \t]")


def _dsh_meaningful(line):
    return bool(line.strip()) and not line.lstrip().startswith("#")


def _dsh_append(lines, entry):
    while lines and not lines[-1].strip():
        lines.pop()
    return lines + entry + [""]


def dsh_entry(plugin=None):
    """The loader patch entry that loads our gate. `name` must be a file:// URL: Node ESM rejects a
    bare Windows path with ERR_UNSUPPORTED_ESM_URL_SCHEME. lstrip keeps POSIX at three slashes too."""
    url = "file:///" + str(Path(plugin or DSH_PLUGIN)).replace("\\", "/").lstrip("/")
    return "- insert:\n    - id: loadout-gate\n      name: " + url + "\n"


def register_dsh_gate(path=None, plugin=None):
    """Upsert our entry into the user-level $DSH_HOME/cordis.patch.yml. Returns the action."""
    path = Path(path or DSH_PATCH)
    entry = dsh_entry(plugin).rstrip("\n").split("\n")
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_lf(path, DSH_HEADER + "\n".join(entry) + "\n")
        return "created"
    with path.open(encoding="utf-8", newline="") as f:  # newline="" so CRLF survives to be detected
        raw = f.read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.replace("\r\n", "\n").split("\n")
    starts = [i for i, line in enumerate(lines) if _DSH_TOP.match(line)]
    if starts:
        for start, stop in zip(starts, starts[1:] + [len(lines)]):
            while stop > start + 1 and not _dsh_meaningful(lines[stop - 1]):
                stop -= 1  # trailing blank and comment lines belong to the file, not to the entry
            if any("gate_dsh.mjs" in line for line in lines[start:stop]):
                if lines[start:stop] == entry:
                    return "unchanged"
                lines[start:stop] = entry
                break
        else:
            lines = _dsh_append(lines, entry)
    else:
        # a fresh profile file is a comment block followed by an empty array
        meaningful = [i for i, line in enumerate(lines) if _dsh_meaningful(line)]
        if len(meaningful) == 1 and lines[meaningful[0]].strip() == "[]":
            lines[meaningful[0]:meaningful[0] + 1] = entry
        else:
            lines = _dsh_append(lines, entry)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(nl.join(lines))
    return "updated"


def parse_accepted(text):
    m = ACCEPTED_RE.search(text)
    if not m:
        return []
    return [(stage.strip(), skill.strip()) for stage, skill in LINE_RE.findall(m.group(0))]


def block(accepted):
    lines = ["## Loadout", "Accepted skill workflow for this project (details in LOADOUT.md):"]
    lines += [f"- {stage}: invoke `{skill}`" for stage, skill in accepted]
    lines += ["Invoke these at their stage without being asked. Do not use skills",
              'listed under "Skip" in LOADOUT.md for this project.']
    return "\n".join(lines) + "\n"


def imports_agents(path):
    try:
        return bool(IMPORT_RE.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def write_lf(path, text):
    # Path.write_text(newline=) is 3.10+; the project floor is 3.9
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def upsert_native(path, blk):
    """A CLAUDE.md that imports AGENTS.md stays import-only: the section lives in AGENTS.md,
    and Claude Code would otherwise read it twice. Any duplicate left by an older apply is removed."""
    if path.name == "CLAUDE.md" and imports_agents(path):
        text = path.read_text(encoding="utf-8", errors="replace")
        m = SECTION_RE.search(text)
        if not m:
            return "imports AGENTS.md (unchanged)"
        write_lf(path, (text[:m.start()] + text[m.end():]).rstrip("\n") + "\n")
        return "duplicate ## Loadout removed (imports AGENTS.md)"
    return upsert(path, blk)


def upsert(path, blk, create_with=None):
    """Replace the ## Loadout section, else append it, else create the file. Returns the action."""
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        m = SECTION_RE.search(text)
        if m:
            sep = "\n" if m.end() < len(text) else ""
            new = text[:m.start()] + blk + sep + text[m.end():]
            action = "replaced"
        else:
            new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + blk
            action = "appended"
    else:
        new = blk if create_with is None else create_with
        action = "created"
    path.write_text(new, encoding="utf-8")
    return action


def apply(project, host, loadout="LOADOUT.md", enforce=True):
    project = Path(project)
    text = (project / loadout).read_text(encoding="utf-8", errors="replace")
    accepted = parse_accepted(text)
    if not accepted:
        raise ValueError(f"no '- <stage>: `<skill>`' lines under '## Accepted' in {loadout}")
    blk = block(accepted)
    gate = enforce and host == "claude-code"
    codex = enforce and host == "codex"
    dsh = enforce and host in ("deepseek", "dsh")
    settings = load_settings(project / SETTINGS_LOCAL) if gate else None  # validate before touching anything
    codex_settings = load_settings(CODEX_HOOKS) if codex else None
    results = {"AGENTS.md": upsert(project / "AGENTS.md", blk)}
    native = NATIVE.get(host)
    if native:
        path = project / native
        if host == "claude-code" and not path.is_file():
            write_lf(path, "@AGENTS.md\n")
            results[native] = "created with @AGENTS.md import"
        else:
            results[native] = upsert_native(path, blk)
    for other in NATIVE.values():
        if other != native and (project / other).is_file():
            results[other] = upsert_native(project / other, blk)
    if gate:
        results[SETTINGS_LOCAL] = register_gate(project, settings) + " (gate hooks take effect from the next Claude Code session)"
    if codex:
        reg = register_codex_gate(CODEX_HOOKS, codex_settings)
        trust = trust_codex_gate(CODEX_HOOKS, CODEX_CONFIG)
        results["~/.codex/hooks.json"] = (reg + "; trust " + ("granted" if trust == "trusted" else "already present")
                                          + " in config.toml (Codex loads hooks at the next session)")
    if dsh:
        results["~/.dsh/cordis.patch.yml"] = register_dsh_gate() + (
            " (dsh loads the plugin at the next session; there is no per-repo config,"
            " so this registration covers every profile)")
    return results


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    host = argv[argv.index("--host") + 1] if "--host" in argv else "unknown"
    loadout = argv[argv.index("--loadout") + 1] if "--loadout" in argv else "LOADOUT.md"
    enforce = "--no-enforce" not in argv
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] not in VALUE_FLAGS)]
    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    try:
        results = apply(args[0], host, loadout, enforce)
    except (OSError, ValueError) as e:
        print(f"apply: {e}", file=sys.stderr)
        sys.exit(2)
    for f, action in results.items():
        print(f"- {f}: {action}")


if __name__ == "__main__":
    main()
