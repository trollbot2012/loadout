# Enforcement Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the accepted loadout binding in Claude Code: no edits before the stage-1 skill has run, no stopping while a binding stage was never run.

**Architecture:** One new stdlib script `scripts/gate.py` runs as a PreToolUse hook (`pre`) and a Stop hook (`stop`). It reads LOADOUT.md's Accepted lines for the binding set and the session transcript JSONL (handed to every hook as `transcript_path`) as the ledger of invoked skills. `scripts/apply.py` registers both hooks into the project's `.claude/settings.local.json` when the host is claude-code.

**Tech Stack:** Python 3.9+ stdlib only, pytest, Claude Code hooks (PreToolUse `permissionDecision`, Stop `decision: block`).

**Spec:** `docs/superpowers/specs/2026-09-02-enforcement-gate-design.md`

## Global Constraints

- Python 3.9+, stdlib only, Windows/macOS/Linux (CI matrix: ubuntu + windows × 3.9/3.11/3.13).
- gate.py never raises and never exits non-zero: any parse problem allows (exit 0, no output).
- Exit 0 with no stdout means "allow" for both hook modes.
- Tests run the scripts as subprocesses with `HOME`/`USERPROFILE` pointed at a temp dir, following `tests/test_scan.py`.
- Write files with `newline="\n"` in tests and scripts; the repo has `.gitattributes` text normalisation.
- Commit after each task. Do not push; the release push is a separate operator step.
- Version bump to `1.4.0` happens in the last task only.

---

## File structure

| File | Responsibility |
|---|---|
| `scripts/gate.py` (create) | Hook entry point. Parse hook JSON, find LOADOUT.md, read the transcript, decide allow/deny/block. |
| `scripts/apply.py` (modify) | Existing `## Loadout` upsert plus new `register_gate()` writing `.claude/settings.local.json`; `--no-enforce` flag. |
| `scripts/scan.py` (modify) | `SKILL_FILES` gains `scripts/gate.py`; project section reports a registered gate. |
| `tests/test_gate.py` (create) | Subprocess tests for gate.py with synthetic transcripts. |
| `tests/test_apply.py` (modify) | Registration tests. |
| `tests/test_scan.py` (modify) | Gate-detection test. |
| `SKILL.md`, `README.md` (modify) | Step 5 registration, report Enforcement line, hatch, ceilings, version. |

`gate.py` imports `parse_accepted` from `apply.py` (same directory; `sys.path[0]` is the script dir when run as a script), so the Accepted-line grammar has one owner.

---

### Task 1: gate.py primitives — write-shaped Bash detection and transcript facts

**Files:**
- Create: `scripts/gate.py`
- Create: `tests/test_gate.py`

**Interfaces:**
- Produces: `write_shaped(cmd: str) -> bool`; `transcript_facts(path) -> tuple[set[str], bool]` returning `(invoked_skills, edited)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for scripts/gate.py: the Claude Code enforcement gate, driven as a subprocess."""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate.py"
sys.path.insert(0, str(REPO / "scripts"))
import gate  # noqa: E402


def test_write_shaped_bash_commands():
    assert gate.write_shaped("cat > f.txt <<'EOF'")
    assert gate.write_shaped("echo hi >> log")
    assert gate.write_shaped("python - <<'EOF'")
    assert gate.write_shaped("sed -i 's/a/b/' f")
    assert gate.write_shaped("ls | tee out.txt")
    assert gate.write_shaped("git checkout -- f.py")
    assert not gate.write_shaped("python -m pytest -q")
    assert not gate.write_shaped("cmd 2>&1 | tail -3")
    assert not gate.write_shaped("cmd > /dev/null")
    assert not gate.write_shaped("cmd >/dev/null 2>&1")
    assert not gate.write_shaped("git status --short")


def transcript(tmp_path, blocks, name="t.jsonl"):
    """Write a JSONL transcript. Each item is a list of content blocks for one assistant turn,
    or a plain string for a user turn."""
    p = tmp_path / name
    lines = []
    for item in blocks:
        if isinstance(item, str):
            lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": item}}))
        else:
            lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": item}}))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return p


def skill(name):
    return {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": name}}


def tool(name, **inp):
    return {"type": "tool_use", "id": "t2", "name": name, "input": inp}


def test_transcript_facts_collects_skills_and_edits(tmp_path):
    t = transcript(tmp_path, [
        "<command-message>loadout</command-message><command-name>/loadout</command-name>",
        [skill("superpowers:brainstorming"), {"type": "text", "text": "ok"}],
        [tool("Bash", command="git status")],
    ])
    invoked, edited = gate.transcript_facts(t)
    assert invoked == {"loadout", "superpowers:brainstorming"}
    assert edited is False
    t = transcript(tmp_path, [[tool("Edit", file_path="a.py", old_string="x", new_string="y")]])
    assert gate.transcript_facts(t) == (set(), True)
    t = transcript(tmp_path, [[tool("Bash", command="cat > a.py <<'EOF'")]])
    assert gate.transcript_facts(t) == (set(), True)


def test_transcript_facts_tolerates_garbage(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('not json\n{"message": 5}\n' + json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "x"}}]}}) + "\n",
                 encoding="utf-8", newline="\n")
    assert gate.transcript_facts(p) == ({"x"}, False)
    assert gate.transcript_facts(tmp_path / "missing.jsonl") == (set(), False)
    assert gate.transcript_facts(None) == (set(), False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gate.py -q`
Expected: FAIL at import with `ModuleNotFoundError: No module named 'gate'`

- [ ] **Step 3: Write the minimal implementation**

```python
#!/usr/bin/env python3
"""loadout gate — make the accepted loadout binding via Claude Code hooks.

Registered by apply.py into <project>/.claude/settings.local.json:
  python gate.py pre    # PreToolUse for Edit|Write|MultiEdit|NotebookEdit|Bash
  python gate.py stop   # Stop
Reads the hook JSON on stdin. Exit 0 with no output = allow. Never raises and never
exits non-zero: enforcement must not break the harness, so any parse problem allows.
Ledger = the session transcript every hook receives as transcript_path; binding set =
LOADOUT.md Accepted lines whose stage label does not start with "situational".
Operator hatch: LOADOUT_ENFORCE=0 (or remove LOADOUT.md). There is no agent-side override.
"""
import json
import os
import re
import shlex
import sys
from pathlib import Path

import apply  # same directory; owns the Accepted-line grammar

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
COMMAND_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
HATCH = "Operator hatch: LOADOUT_ENFORCE=0."

# a redirect that is not a 2>&1-style fd dup and not to /dev/null or NUL
_REDIRECT = re.compile(r"(?<![<>0-9&])>{1,2}(?!&)(?!\s*(?:/dev/null|NUL\b))")
_WRITE_WORDS = re.compile(
    r"(?:^|[|;&]\s*)(?:tee|sed\s+-i|perl\s+-i|mv|cp|install|patch|git\s+apply|git\s+checkout\s+--|git\s+restore|touch)\b")


def write_shaped(cmd):
    """True when a shell command looks like it writes a file. False positives are accepted."""
    return bool("<<" in cmd or _REDIRECT.search(cmd) or _WRITE_WORDS.search(cmd))


def transcript_facts(path):
    """(invoked skill names, edited?) from a JSONL transcript. Bad lines are skipped."""
    invoked, edited = set(), False
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return invoked, edited
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            invoked.update(COMMAND_RE.findall(content))
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                invoked.update(COMMAND_RE.findall(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                name, inp = block.get("name"), block.get("input") or {}
                if name == "Skill" and inp.get("skill"):
                    invoked.add(str(inp["skill"]))
                elif name in EDIT_TOOLS:
                    edited = True
                elif name == "Bash" and write_shaped(str(inp.get("command") or "")):
                    edited = True
    return invoked, edited
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gate.py -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/gate.py tests/test_gate.py
git commit -m "gate: write-shaped command detection and transcript facts"
```

---

### Task 2: gate.py decisions — pre and stop modes, CLI

**Files:**
- Modify: `scripts/gate.py` (append below `transcript_facts`)
- Modify: `tests/test_gate.py` (append)

**Interfaces:**
- Consumes: `apply.parse_accepted(text) -> list[tuple[stage, skill]]` (exists in apply.py), `write_shaped`, `transcript_facts` from Task 1.
- Produces: `find_loadout(cwd) -> Path | None`; `binding_stages(text) -> list[tuple[stage, skill]]`; `bootstrap_invocation(cmd: str) -> bool`; `decide(mode, hook, env) -> dict | None`; CLI `python gate.py pre|stop` reading stdin JSON, printing the JSON decision or nothing, exit 0 always.

- [ ] **Step 1: Write the failing tests**

```python
LOADOUT = ("# Loadout: x\nHarness: claude-code | Project type: cli\nDate: 2026-09-02\n\n"
           "## Accepted\n- planning: `planner`\n- review: `reviewer`\n"
           "- situational, gated work: `unlazy`\n")


def project(tmp_path, loadout=LOADOUT):
    proj = tmp_path / "proj" / "sub"
    proj.mkdir(parents=True)
    if loadout is not None:
        (tmp_path / "proj" / "LOADOUT.md").write_text(loadout, encoding="utf-8", newline="\n")
    return proj


def run_gate(mode, hook, env=None):
    e = {k: v for k, v in os.environ.items() if k != "LOADOUT_ENFORCE"}
    e.update(env or {})
    r = subprocess.run([sys.executable, str(GATE), mode], input=json.dumps(hook),
                       capture_output=True, encoding="utf-8", env=e)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def pre_hook(proj, t, tool_name="Edit", **inp):
    return {"cwd": str(proj), "transcript_path": str(t), "tool_name": tool_name,
            "tool_input": inp or {"file_path": str(proj / "a.py")}}


def stop_hook(proj, t, active=False):
    return {"cwd": str(proj), "transcript_path": str(t), "stop_hook_active": active}


def test_binding_stages_skip_situational():
    assert gate.binding_stages(LOADOUT) == [("planning", "planner"), ("review", "reviewer")]


def test_find_loadout_walks_up(tmp_path):
    proj = project(tmp_path)
    assert gate.find_loadout(proj) == tmp_path / "proj" / "LOADOUT.md"
    assert gate.find_loadout(tmp_path) is None


def test_pre_denies_edit_before_stage_one_and_allows_after(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[tool("Bash", command="git status")]])
    out = run_gate("pre", pre_hook(proj, t))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "`planner`" in reason and "planning" in reason and "LOADOUT_ENFORCE=0" in reason
    t = transcript(tmp_path, [[skill("planner")]])
    assert run_gate("pre", pre_hook(proj, t)) is None


def test_pre_gates_only_write_shaped_bash(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
    assert run_gate("pre", pre_hook(proj, t, "Bash", command="python -m pytest -q")) is None
    out = run_gate("pre", pre_hook(proj, t, "Bash", command="cat > a.py <<'EOF'"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run_gate("pre", pre_hook(proj, t, "Read", file_path="a.py")) is None


def test_bootstrap_boundary_is_exact_not_blanket(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
    denied = lambda hook: run_gate("pre", hook)["hookSpecificOutput"]["permissionDecision"] == "deny"  # noqa: E731
    # activation files stay gated like any other mutation
    assert denied(pre_hook(proj, t, "Write", file_path=str(proj.parent / "LOADOUT.md"), content="x"))
    assert denied(pre_hook(proj, t, "Edit", file_path=str(proj.parent / "AGENTS.md"), old_string="a", new_string="b"))
    assert denied(pre_hook(proj, t, "Bash", command="cat > LOADOUT.md <<'EOF'"))
    assert denied(pre_hook(proj, t, "Bash", command="echo x >> CLAUDE.md"))
    # only the exact validated apply.py invocation is the bootstrap exception
    ok = ['python "x/apply.py" . --host claude-code', "python3 scripts/apply.py /p --loadout L.md --no-enforce",
          f'"{sys.executable}" "C:\\a b\\apply.py" "C:\\proj"']
    for cmd in ok:
        assert run_gate("pre", pre_hook(proj, t, "Bash", command=cmd)) is None, cmd
    bad = ["python x/apply.py . --host claude-code && cat > LOADOUT.md", "python x/apply.py . ; rm -rf x",
           "python x/apply.py . > out", "python x/apply.py . --host claude-code --evil", "python x/apply.py . extra",
           "python x/notapply.py .", "bash apply.py .", "python x/apply.py"]
    for cmd in bad:
        assert denied(pre_hook(proj, t, "Bash", command=cmd)), cmd


def test_bootstrap_invocation_predicate():
    assert gate.bootstrap_invocation('python "x/apply.py" . --host claude-code')
    assert not gate.bootstrap_invocation("python x/apply.py . | tee log")
    assert not gate.bootstrap_invocation("python x/apply.py . $(id)")
    assert not gate.bootstrap_invocation("python x/apply.py . `id`")


def test_slash_command_counts_as_invoked(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, ["<command-name>/planner</command-name>"])
    assert run_gate("pre", pre_hook(proj, t)) is None


def test_stop_blocks_only_after_edits_and_names_missing_stages(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[skill("planner")], [tool("Bash", command="ls")]])
    assert run_gate("stop", stop_hook(proj, t)) is None, "no edits: never trap a question-only session"
    t = transcript(tmp_path, [[skill("planner")], [tool("Edit", file_path="a.py")]])
    out = run_gate("stop", stop_hook(proj, t))
    assert out["decision"] == "block"
    assert "review (`reviewer`)" in out["reason"] and "planner" not in out["reason"]
    assert "unlazy" not in out["reason"], "situational stages are not binding"
    t = transcript(tmp_path, [[skill("planner")], [skill("reviewer")], [tool("Edit", file_path="a.py")]])
    assert run_gate("stop", stop_hook(proj, t)) is None


def test_stop_respects_loop_guard(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[tool("Edit", file_path="a.py")]])
    assert run_gate("stop", stop_hook(proj, t, active=True)) is None


def test_silent_allow_without_loadout_or_with_hatch(tmp_path):
    proj = project(tmp_path, loadout=None)
    t = transcript(tmp_path, [[tool("Edit", file_path="a.py")]])
    assert run_gate("pre", pre_hook(proj, t)) is None
    assert run_gate("stop", stop_hook(proj, t)) is None
    proj = project(tmp_path / "two")
    assert run_gate("pre", pre_hook(proj, t), env={"LOADOUT_ENFORCE": "0"}) is None
    proj = project(tmp_path / "three", loadout="# Loadout\n\n## Accepted\n- situational, x: `unlazy`\n")
    assert run_gate("stop", stop_hook(proj, t)) is None, "no binding stages"


def test_never_breaks_the_harness(tmp_path):
    for mode in ("pre", "stop", "bogus"):
        r = subprocess.run([sys.executable, str(GATE), mode], input="not json",
                           capture_output=True, encoding="utf-8")
        assert r.returncode == 0 and r.stdout == ""
    r = subprocess.run([sys.executable, str(GATE)], input="{}", capture_output=True, encoding="utf-8")
    assert r.returncode == 0 and r.stdout == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gate.py -q`
Expected: FAIL with `AttributeError: module 'gate' has no attribute 'binding_stages'` and the subprocess tests failing on empty stdout where a decision is expected.

- [ ] **Step 3: Write the minimal implementation** (append to `scripts/gate.py`)

```python
def find_loadout(cwd):
    """Nearest LOADOUT.md walking up from cwd, or None."""
    try:
        p = Path(cwd or os.getcwd()).resolve()
    except (OSError, TypeError):
        return None
    for d in (p, *p.parents):
        f = d / "LOADOUT.md"
        if f.is_file():
            return f
    return None


def binding_stages(text):
    """Accepted lines minus those whose stage label starts with 'situational'."""
    return [(s, k) for s, k in apply.parse_accepted(text) if not s.lower().startswith("situational")]


_INTERPRETERS = {"python", "python3", "python.exe", "py", "py.exe"}
_BOOTSTRAP_VALUE_FLAGS = {"--host", "--loadout"}


def bootstrap_invocation(cmd):
    """True only for an exact, validated `apply.py` run: the one-time bootstrap that may
    re-run on an enforced project. No shell operators, no extra tokens. Everything else that
    touches LOADOUT.md / AGENTS.md / CLAUDE.md stays gated like any other mutation."""
    if re.search(r"[|;&<>`\n]|\$\(", cmd):
        return False
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return False
    if len(toks) < 3:
        return False
    exe, script, rest = toks[0], toks[1], toks[2:]
    if Path(exe).name.lower() not in _INTERPRETERS and exe != sys.executable:
        return False
    if Path(script).name != "apply.py":
        return False
    positional, i = 0, 0
    while i < len(rest):
        tok = rest[i]
        if tok in _BOOTSTRAP_VALUE_FLAGS:
            if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
                return False
            i += 2
        elif tok == "--no-enforce":
            i += 1
        elif tok.startswith("-"):
            return False
        else:
            positional += 1
            i += 1
    return positional == 1


def decide(mode, hook, env=None):
    """The hook decision dict, or None to allow."""
    env = os.environ if env is None else env
    if env.get("LOADOUT_ENFORCE") == "0":
        return None
    loadout = find_loadout(hook.get("cwd"))
    if not loadout:
        return None
    try:
        stages = binding_stages(loadout.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not stages:
        return None
    if mode == "pre":
        tool, inp = hook.get("tool_name"), hook.get("tool_input") or {}
        if tool == "Bash":
            cmd = str(inp.get("command") or "")
            if bootstrap_invocation(cmd) or not write_shaped(cmd):
                return None
        elif tool not in EDIT_TOOLS:
            return None
        stage, skill = stages[0]
        invoked, _ = transcript_facts(hook.get("transcript_path"))
        if skill in invoked:
            return None
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason":
                f"Loadout gate: invoke `{skill}` ({stage}) before editing. Details in LOADOUT.md. {HATCH}"}}
    if mode == "stop":
        if hook.get("stop_hook_active"):
            return None
        invoked, edited = transcript_facts(hook.get("transcript_path"))
        if not edited:
            return None
        missing = [(s, k) for s, k in stages if k not in invoked]
        if not missing:
            return None
        return {"decision": "block",
                "reason": "Loadout gate: stages not run this session: "
                          + ", ".join(f"{s} (`{k}`)" for s, k in missing)
                          + f". Invoke them, then stop. {HATCH}"}
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        out = decide(mode, json.load(sys.stdin))
    except Exception:  # deliberate: a broken gate must allow, never wedge the harness
        out = None
    if out:
        sys.stdout.write(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gate.py -q`
Expected: `14 passed`

- [ ] **Step 5: Try it against this session's real transcript** (manual check, not a test)

```bash
python - <<'EOF'
import json, subprocess, sys, glob, os
t = max(glob.glob(os.path.expanduser("~/.claude/projects/C--/*.jsonl")), key=os.path.getmtime)
hook = {"cwd": os.getcwd(), "transcript_path": t, "tool_name": "Edit", "tool_input": {"file_path": "x.py"}}
print(subprocess.run([sys.executable, "scripts/gate.py", "pre"], input=json.dumps(hook), capture_output=True, text=True).stdout or "allow")
EOF
```

Expected in the loadout repo (LOADOUT.md present, stage 1 = planning-with-files): a deny naming `planning-with-files` unless that skill was invoked in the current session.

- [ ] **Step 6: Commit**

```bash
git add scripts/gate.py tests/test_gate.py
git commit -m "gate: pre and stop decisions from LOADOUT.md and the session transcript"
```

---

### Task 3: apply.py registers the gate into .claude/settings.local.json

**Files:**
- Modify: `scripts/apply.py` (imports, new `register_gate`, `apply()` signature, `main()` arg parsing)
- Modify: `tests/test_apply.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `register_gate(project: Path) -> str` returning `created | updated | unchanged`; `apply(project, host, loadout="LOADOUT.md", enforce=True)`; CLI flag `--no-enforce`; result key `".claude/settings.local.json"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_claude_registers_gate_hooks_idempotently(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    res = apply.apply(tmp_path, "claude-code")
    assert res[".claude/settings.local.json"].startswith("created")
    data = json.loads((tmp_path / ".claude/settings.local.json").read_text(encoding="utf-8"))
    pre, stop = data["hooks"]["PreToolUse"], data["hooks"]["Stop"]
    assert pre[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit|Bash"
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
    assert [h["hooks"][0]["command"] for h in data["hooks"]["Stop"]][0] == "other-stop"
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


def test_invalid_settings_json_is_an_error(tmp_path):
    (tmp_path / "LOADOUT.md").write_text(LOADOUT, encoding="utf-8")
    p = tmp_path / ".claude/settings.local.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        apply.apply(tmp_path, "claude-code")
```

Add `import json` to the test file's imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_apply.py -q`
Expected: FAIL with `KeyError: '.claude/settings.local.json'` and `AttributeError: module 'apply' has no attribute 'register_gate'`.

- [ ] **Step 3: Write the minimal implementation**

Add to imports in `scripts/apply.py`:

```python
import json
```

Add after `NATIVE = ...`:

```python
GATE = Path(__file__).resolve().parent / "gate.py"
GATE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
SETTINGS_LOCAL = ".claude/settings.local.json"


def gate_hooks():
    """Hook registrations for this machine's copy of gate.py (hence settings.local.json)."""
    cmd = lambda mode: f'"{sys.executable}" "{GATE}" {mode}'  # noqa: E731
    return {"PreToolUse": [{"matcher": GATE_MATCHER, "hooks": [{"type": "command", "command": cmd("pre")}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("stop")}]}]}


def register_gate(project):
    """Upsert the gate hooks into <project>/.claude/settings.local.json. Returns the action."""
    path = Path(project) / SETTINGS_LOCAL
    data, existed = {}, path.is_file()
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError(f"{path} is not valid JSON; fix it or pass --no-enforce")
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, entries in gate_hooks().items():
        current = hooks.get(event, [])
        kept = [e for e in current if not any("gate.py" in h.get("command", "") for h in e.get("hooks", []))]
        new = kept + entries
        if new != current:
            hooks[event] = new
            changed = True
    if existed and not changed:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "updated" if existed else "created"
```

Change `apply()`:

```python
def apply(project, host, loadout="LOADOUT.md", enforce=True):
    project = Path(project)
    text = (project / loadout).read_text(encoding="utf-8", errors="replace")
    accepted = parse_accepted(text)
    if not accepted:
        raise ValueError(f"no '- <stage>: `<skill>`' lines under '## Accepted' in {loadout}")
    blk = block(accepted)
    results = {"AGENTS.md": upsert(project / "AGENTS.md", blk)}
    native = NATIVE.get(host)
    if native:
        path = project / native
        if host == "claude-code" and not path.is_file():
            path.write_text("@AGENTS.md\n", encoding="utf-8")
            results[native] = "created with @AGENTS.md import"
        else:
            results[native] = upsert(path, blk)
    for other in NATIVE.values():
        if other != native and (project / other).is_file():
            results[other] = upsert(project / other, blk)
    if enforce and host == "claude-code":
        results[SETTINGS_LOCAL] = register_gate(project) + " (gate hooks take effect from the next Claude Code session)"
    return results
```

Replace `main()`'s argument parsing so a value-less flag never swallows the positional:

```python
def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    host = argv[argv.index("--host") + 1] if "--host" in argv else "unknown"
    loadout = argv[argv.index("--loadout") + 1] if "--loadout" in argv else "LOADOUT.md"
    enforce = "--no-enforce" not in argv
    value_flags = {"--host", "--loadout"}
    args = [a for i, a in enumerate(argv)
            if not a.startswith("--") and (i == 0 or argv[i - 1] not in value_flags)]
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
```

Update the module docstring's Usage line to `python apply.py <project_dir> --host <host> [--loadout LOADOUT.md] [--no-enforce]` and add a bullet: `- on claude-code, the enforcement gate (scripts/gate.py) into .claude/settings.local.json unless --no-enforce`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests -q`
Expected: all pass (previous 26 + 14 gate + 4 apply = 44).

- [ ] **Step 5: Commit**

```bash
git add scripts/apply.py tests/test_apply.py
git commit -m "apply: register the enforcement gate hooks in .claude/settings.local.json"
```

---

### Task 4: scanner ships gate.py and reports a registered gate

**Files:**
- Modify: `scripts/scan.py` (`SKILL_FILES` near line 22; project loadout detection; markdown prior-loadout line)
- Modify: `tests/test_scan.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `inv["project"]["loadout"]["gate"] == "claude-code"` when `.claude/settings.local.json` contains a `gate.py` hook; markdown prior-loadout line gains `; enforcement gate registered (claude-code)`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scan.py -q -k reaudit`
Expected: FAIL with `KeyError: 'gate'`.

- [ ] **Step 3: Write the minimal implementation**

In `scripts/scan.py`, change `SKILL_FILES`:

```python
SKILL_FILES = ["SKILL.md", "README.md", "LICENSE", "scripts/scan.py", "scripts/apply.py", "scripts/gate.py"]
```

Find the function that builds `p["loadout"]` (search for `"LOADOUT.md" in` / `loadout_section`). After the existing detection, add:

```python
    local = proj / ".claude" / "settings.local.json"
    if local.is_file():
        try:
            if "gate.py" in local.read_text(encoding="utf-8", errors="replace"):
                lo["gate"] = "claude-code"
        except OSError:
            pass
```

where `lo` is the dict assigned to the project's `loadout` key (adapt the variable name to the existing code; if the loadout dict is only created when LOADOUT.md or a section exists, create it when the gate is found too).

In `markdown()`, inside the `if lo:` block after the CLAUDE.md-import bit:

```python
        if lo.get("gate"):
            bits.append(f"enforcement gate registered ({lo['gate']})")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/scan.py tests/test_scan.py
git commit -m "scan: ship gate.py with self-install; report a registered gate on re-audit"
```

---

### Task 5: docs, version 1.4.0, self-install, live registration on this repo

**Files:**
- Modify: `SKILL.md` (frontmatter version; step 5 action 2; report template; a Notes bullet)
- Modify: `README.md` (component list; new Enforcement section)
- Modify: `docs/superpowers/specs/2026-09-02-enforcement-gate-design.md` (one line: activation-file exemption)

- [ ] **Step 1: SKILL.md edits**

Frontmatter: `version: "1.4.0"`.

Step 5, action 2, after the sentence ending "they never add a second one.", add:

```markdown
   On Claude Code this also registers the enforcement gate (`scripts/gate.py`) into
   `.claude/settings.local.json`: the agent cannot edit before the stage-1 skill has run and
   cannot stop while a binding stage was never run. Say that the gate takes effect from the
   next Claude Code session. Pass `--no-enforce` only if the user asks for prose-only wiring.
```

Report template: add under the `Date:` line:

```markdown
Enforcement: claude-code gate registered | prose only
```

and under `## Accepted` in the template add the comment line:

```markdown
- situational, <when>: `<skill>`   <- stages labelled "situational" are accepted but not binding
```

Notes for specific hosts, Claude Code bullet, append:

```markdown
  The enforcement gate is Claude Code only in this version. Operator hatch:
  `LOADOUT_ENFORCE=0` or remove LOADOUT.md; there is no agent-side override. Ceilings: Claude
  Code overrides a Stop hook after 8 consecutive blocks without progress, and the Bash write
  check is a heuristic (redirects, heredocs, tee/sed -i/mv/cp/patch) that can misfire on an
  innocent command, which the hatch covers.
```

- [ ] **Step 2: README edits**

Component list: add `- scripts/gate.py — Claude Code hook: enforces the accepted loadout (PreToolUse deny before stage 1, Stop block while binding stages are missing)`.

New section before "Self-install, check and update":

```markdown
## Enforcement (Claude Code)

`apply.py --host claude-code` also registers `scripts/gate.py` as a PreToolUse and Stop hook
in the project's `.claude/settings.local.json` (local: the command embeds this machine's
path). From the next session the agent cannot edit files, or run a write-shaped shell
command, before the stage-1 skill has been invoked, and cannot stop while any binding stage
(every Accepted line not labelled `situational`) was never invoked. The ledger is the
session transcript; there is no state file and no agent-side override.

Operator hatch: `LOADOUT_ENFORCE=0` in the environment, or remove LOADOUT.md. Skip
registration with `--no-enforce`. Ceilings: Claude Code overrides a Stop hook after 8
consecutive blocks without progress; the shell write check is a heuristic and can misfire.
Writes to LOADOUT.md, AGENTS.md and CLAUDE.md are gated like any other edit once the gate
is active; only an exact `apply.py` invocation is allowed as a re-bootstrap, so a re-audit on
an enforced project needs the stage-1 skill first or the hatch. Other hosts keep prose wiring
in this version.
```

- [ ] **Step 3: Spec check**

The spec already carries the "Bootstrap boundary" section (no blanket exemption; exact
`apply.py` invocation only). Confirm the README Enforcement section states it.

- [ ] **Step 4: Verify, re-sync copies, register on this repo**

```bash
python -m pytest tests -q
python scripts/scan.py --self-install | grep -c updated
python scripts/scan.py --check; echo "check exit=$?"
python scripts/apply.py . --host claude-code
cat .claude/settings.local.json
git status --short
```

Expected: all tests pass; 15 copies updated; check exit 0; apply prints `- .claude/settings.local.json: created (gate hooks take effect from the next Claude Code session)`; `.claude/settings.local.json` is untracked (add it to `.gitignore` if git shows it, since it embeds a machine path).

- [ ] **Step 5: Commit**

```bash
git add SKILL.md README.md docs/superpowers/specs/2026-09-02-enforcement-gate-design.md .gitignore
git commit -m "loadout v1.4.0: enforcement gate for Claude Code"
```

- [ ] **Step 6: Live proof in a fresh session** (operator step, reported not assumed)

Open a new Claude Code session in `Projects/loadout`, ask for a one-line edit to README.md without invoking any skill. Expected: the Edit is denied with `Loadout gate: invoke \`planning-with-files\` (planning) before editing.` Then invoke `/planning-with-files`, retry the edit (allowed), and try to end the turn: expected Stop block naming the remaining binding stages. The release push and `v1.4.0` tag follow only after this proof.
