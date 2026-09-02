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
    assert gate.write_shaped("cd x && cp a b")
    assert gate.write_shaped("python x/apply.py ."), "names the enforcement surface"
    assert gate.write_shaped("cat CLAUDE.md"), "conservative: naming an activation file counts"
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


# ---------------------------------------------------------------- decisions

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


def denied(hook):
    return run_gate("pre", hook)["hookSpecificOutput"]["permissionDecision"] == "deny"


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
    assert denied(pre_hook(proj, t, "Bash", command="cat > a.py <<'EOF'"))
    assert run_gate("pre", pre_hook(proj, t, "Read", file_path="a.py")) is None


def test_bootstrap_boundary_is_exact_not_blanket(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
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
