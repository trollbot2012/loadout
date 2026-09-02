"""Hardening tests for scripts/gate.py: the bypasses an adversarial review found, closed."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate.py"
APPLY = REPO / "scripts" / "apply.py"
sys.path.insert(0, str(REPO / "scripts"))
import gate  # noqa: E402
from test_gate import LOADOUT, project, run_gate, pre_hook, stop_hook, denied, transcript, skill, tool  # noqa: E402


def test_write_shaped_covers_non_shell_writers_and_prefixes():
    yes = ['python -c "open(\'a\',\'w\').write(1)"', "node -e \"require('fs').writeFileSync('a','')\"",
           "powershell -Command Set-Content a b", "pwsh -c 'x'", "curl -o a.txt http://x", "wget http://x",
           "dd if=/dev/zero of=a", "sed --in-place s/a/b/ f", "sed -i.bak s/a/b/ f", "perl -pi -e s/a/b/ f",
           "git commit -am x", "git stash", "git reset --hard", "git clean -fd", "git checkout HEAD -- f",
           "sudo cp a b", "env cp a b", "FOO=1 cp a b", "/bin/cp a b", "(cp a b)", "sh -c 'cp a b'",
           "echo x ＞ f"]
    for cmd in yes:
        assert gate.write_shaped(cmd), cmd
    no = ["git status", "git log --oneline", "git diff main...HEAD", "python -m pytest -q", "node --version",
          "curl http://x", "grep -rn foo .", "python x.py"]
    for cmd in no:
        assert not gate.write_shaped(cmd), cmd


def test_transcript_facts_drops_failed_skill_calls_and_counts_delegation(tmp_path):
    ok = {"type": "tool_use", "id": "ok1", "name": "Skill", "input": {"skill": "planner"}}
    bad = {"type": "tool_use", "id": "bad1", "name": "Skill", "input": {"skill": "reviewer"}}
    err = {"type": "tool_result", "tool_use_id": "bad1", "is_error": True, "content": "Unknown skill"}
    t = transcript(tmp_path, [[ok, bad], [err], [tool("Agent", prompt="edit stuff")]])
    facts = gate.transcript_facts(t)
    assert facts.invoked == {"planner"}, "an errored Skill call is not an invocation"
    assert facts.edited is True, "delegating to a subagent is a mutation"


def test_transcript_first_cwd_is_recorded(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes((json.dumps({"type": "user", "cwd": str(tmp_path / "proj"), "message": {"role": "user", "content": "hi"}}) + "\n"
                   + json.dumps({"type": "assistant", "cwd": "C:/", "message": {"content": []}}) + "\n").encode("utf-8"))
    assert gate.transcript_facts(p).cwd == str(tmp_path / "proj")


def test_stop_gate_is_not_a_one_shot_nag_but_is_capped(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[skill("planner")], [tool("Edit", file_path="a.py")]])
    sid = f"pytest-{os.getpid()}-{tmp_path.name}"
    counter = Path(tempfile.gettempdir()) / f"loadout-gate-{sid}.blocks"
    if counter.exists():
        counter.unlink()
    try:
        hook = dict(stop_hook(proj, t, active=True), session_id=sid)
        for i in range(gate.STOP_BLOCK_CAP):
            out = run_gate("stop", hook)
            assert out and out["decision"] == "block", f"block {i + 1} must still fire with stop_hook_active"
        assert run_gate("stop", hook) is None, "own cap: allow after STOP_BLOCK_CAP blocks"
    finally:
        if counter.exists():
            counter.unlink()


def test_surface_is_operator_owned_at_every_stage(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[skill("planner")]])  # stage 1 satisfied
    assert run_gate("pre", pre_hook(proj, t)) is None, "ordinary edit allowed after stage 1"
    for name in ("LOADOUT.md", "AGENTS.md", "CLAUDE.md", "settings.json", "settings.local.json", "gate.py", "apply.py"):
        assert denied(pre_hook(proj, t, "Edit", file_path=str(proj.parent / name), old_string="a", new_string="b")), name
        assert denied(pre_hook(proj, t, "Write", file_path=str(proj.parent / ".claude" / name), content="x")), name
    assert denied(pre_hook(proj, t, "Bash", command="echo x >> LOADOUT.md"))
    assert denied(pre_hook(proj, t, "Bash", command="cat > .claude/settings.json <<'EOF'"))
    assert denied(pre_hook(proj, t, "Bash", command='python -c "open(\'CLAUDE.md\',\'w\')"'))
    assert run_gate("pre", pre_hook(proj, t, "Bash", command="cat LOADOUT.md")) is None, "reading is fine"
    assert run_gate("pre", pre_hook(proj, t, "Bash", command=f'python "{APPLY}" . --host claude-code')) is None


def test_bootstrap_must_be_this_skills_apply_py(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
    evil = tmp_path / "evil" / "apply.py"
    evil.parent.mkdir()
    evil.write_bytes(b"print('evil')\n")
    assert denied(pre_hook(proj, t, "Bash", command=f'python "{evil}" . --host claude-code'))
    assert denied(pre_hook(proj, t, "Bash", command="python x/apply.py . --host claude-code"))
    assert run_gate("pre", pre_hook(proj, t, "Bash", command=f'python "{APPLY}" . --host claude-code')) is None
    rel = os.path.relpath(APPLY, proj)
    assert gate.bootstrap_invocation(f'python "{rel}" .', cwd=str(proj))


def test_cd_above_the_project_does_not_blind_the_gate(tmp_path):
    proj = project(tmp_path)
    # transcript says the session started in the project; the hook cwd has drifted to the drive root
    p = tmp_path / "t.jsonl"
    lines = [json.dumps({"type": "user", "cwd": str(proj), "message": {"role": "user", "content": "go"}}),
             json.dumps({"type": "assistant", "message": {"content": [tool("Edit", file_path="a.py")]}})]
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    outside = str(Path(tmp_path.anchor))
    hook = {"cwd": outside, "transcript_path": str(p), "tool_name": "Write",
            "tool_input": {"file_path": str(proj / "a.py"), "content": "x"}}
    assert denied(hook), "file path is inside the project"
    hook["tool_input"] = {"file_path": str(tmp_path / "elsewhere.py"), "content": "x"}
    assert denied(hook), "session started inside the project"
    assert run_gate("stop", {"cwd": outside, "transcript_path": str(p), "stop_hook_active": False})["decision"] == "block"


def test_subagent_is_gated_against_the_parent_session(tmp_path):
    proj = project(tmp_path)
    sess = tmp_path / "sessions" / "abc.jsonl"
    sub = tmp_path / "sessions" / "abc" / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_bytes((json.dumps({"type": "assistant", "message": {"content": []}}) + "\n").encode("utf-8"))
    hook = {"cwd": str(proj), "transcript_path": str(sub), "agent_id": "agent-1", "tool_name": "Edit",
            "tool_input": {"file_path": str(proj / "a.py")}}
    sess.write_bytes((json.dumps({"type": "assistant", "message": {"content": []}}) + "\n").encode("utf-8"))
    assert denied(hook), "parent never ran stage 1"
    sess.write_bytes((json.dumps({"type": "assistant", "message": {"content": [skill("planner")]}}) + "\n").encode("utf-8"))
    assert run_gate("pre", hook) is None, "parent ran stage 1, so its subagent may edit"


def test_mcp_and_worktree_tools_are_gated_by_name(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
    assert denied(pre_hook(proj, t, "EnterWorktree", name="x"))
    assert denied(pre_hook(proj, t, "mcp__fs__write_file", path="a", content="x"))
    assert denied(pre_hook(proj, t, "mcp__composio__COMPOSIO_REMOTE_BASH_TOOL", command="ls"))
    assert run_gate("pre", pre_hook(proj, t, "mcp__context7__query-docs", q="x")) is None
    assert run_gate("pre", pre_hook(proj, t, "Read", file_path="a")) is None
