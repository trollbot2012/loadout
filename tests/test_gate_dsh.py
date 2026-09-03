"""Tests for the DeepSeek Harness ledger: facts come from the plugin's live event stream.

dsh writes its session log as multi-frame zstd, which stdlib Python cannot read, so the in-process
plugin passes the events it observed on the hook payload instead. Everything else (binding stages,
surface rules, bootstrap boundary, write-shaped detection) stays in gate.py.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate.py"
sys.path.insert(0, str(REPO / "scripts"))
import gate  # noqa: E402
import gate_dsh  # noqa: E402

LOADOUT = "# Loadout: x\n\n## Accepted\n- planning: `planner`\n- review: `reviewer`\n- situational, x: `unlazy`\n"


def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "LOADOUT.md").write_bytes(LOADOUT.encode("utf-8"))
    return proj


def run_gate(mode, hook):
    e = {k: v for k, v in os.environ.items() if k != "LOADOUT_ENFORCE"}
    r = subprocess.run([sys.executable, str(GATE), mode, "--host", "dsh"], input=json.dumps(hook),
                       capture_output=True, encoding="utf-8", env=e)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def hook(proj, events, **extra):
    base = {"session_id": "s1", "cwd": str(proj), "events": events}
    base.update(extra)
    return base


def skill_event(name):
    return {"t": "skill", "name": name}


def tool_event(name, **args):
    return dict({"t": "tool", "name": name}, **args)


# ---------------------------------------------------------------- ledger

def test_facts_from_events():
    f = gate_dsh.facts_from_events([skill_event("planner"), tool_event("write", file="a.py")], cwd="C:/p")
    assert f.invoked == {"planner"} and f.edited is True and f.cwd == "C:/p"

    f = gate_dsh.facts_from_events([tool_event("read", file="a.py"), tool_event("pwsh", cmd="git status --short")])
    assert f.invoked == set() and f.edited is False, "reads and read-only shells are not edits"

    f = gate_dsh.facts_from_events([tool_event("pwsh", cmd="echo hi > a.txt")])
    assert f.edited is True, "write-shaped shell is an edit"

    f = gate_dsh.facts_from_events([tool_event("edit", file="a.py")])
    assert f.edited is True


def test_blocks_count_consecutively_and_reset_on_a_skill():
    ev = [tool_event("write", file="a.py"), {"t": "block"}, {"t": "block"}]
    assert gate_dsh.facts_from_events(ev).blocks == 2
    assert gate_dsh.facts_from_events(ev + [skill_event("planner"), {"t": "block"}]).blocks == 1


def test_garbage_events_are_tolerated():
    f = gate_dsh.facts_from_events(["nonsense", None, 5, {"t": "tool"}, {"no": "t"}, skill_event("planner")])
    assert f.invoked == {"planner"} and f.edited is False
    assert gate_dsh.facts_from_events(None) == gate.Facts(set(), False, None, 0)


# ---------------------------------------------------------------- decisions through gate.py

def test_pre_denies_an_edit_before_stage_one_and_allows_after(tmp_path):
    proj = project(tmp_path)
    h = hook(proj, [], tool_name="write", tool_input={"file_path": str(proj / "a.py")})
    out = run_gate("pre", h)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "`planner`" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert run_gate("pre", hook(proj, [skill_event("planner")], tool_name="write",
                                tool_input={"file_path": str(proj / "a.py")})) is None


def test_pre_gates_shell_by_shape_and_never_gates_the_skill_tool(tmp_path):
    proj = project(tmp_path)
    denied = run_gate("pre", hook(proj, [], tool_name="pwsh", tool_input={"command": "echo hi > a.txt"}))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run_gate("pre", hook(proj, [], tool_name="pwsh", tool_input={"command": "git status --short"})) is None
    # the stage-1 skill must stay reachable, or the gate could never be satisfied
    assert run_gate("pre", hook(proj, [], tool_name="skill", tool_input={"name": "planner"})) is None
    for read_only in ("read", "glob", "grep", "todo_write"):
        assert run_gate("pre", hook(proj, [], tool_name=read_only, tool_input={"file_path": "a.py"})) is None, read_only


def test_the_enforcement_surface_is_operator_owned_on_dsh(tmp_path):
    proj = project(tmp_path)
    done = [skill_event("planner")]
    for target in ("LOADOUT.md", "AGENTS.md", str(Path.home() / ".dsh" / "settings.yaml")):
        out = run_gate("pre", hook(proj, done, tool_name="write", tool_input={"file_path": target}))
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", target
    for cmd in ("rm ~/.dsh/profiles/headless/cordis.patch.yml", "echo x > cordis.patch.yml"):
        out = run_gate("pre", hook(proj, done, tool_name="pwsh", tool_input={"command": cmd}))
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


def test_stop_blocks_until_every_binding_stage_ran(tmp_path):
    proj = project(tmp_path)
    edited = [skill_event("planner"), tool_event("write", file="a.py")]
    out = run_gate("stop", hook(proj, edited))
    assert out["decision"] == "block" and "review (`reviewer`)" in out["reason"] and "unlazy" not in out["reason"]
    assert run_gate("stop", hook(proj, edited + [skill_event("reviewer")])) is None
    assert run_gate("stop", hook(proj, [tool_event("pwsh", cmd="git status")])) is None, "no edit: never trapped"


def test_stop_never_yields_on_dsh_however_many_blocks(tmp_path):
    proj = project(tmp_path)
    edited = [skill_event("planner"), tool_event("write", file="a.py")]
    for n in (gate.STOP_BLOCK_CAP, gate.STOP_BLOCK_CAP + 25):
        out = run_gate("stop", hook(proj, edited + [{"t": "block"}] * n))
        assert out and out["decision"] == "block", f"dsh has no host cap; the gate must not yield after {n}"


def test_operator_hatch_still_disables_everything(tmp_path):
    proj = project(tmp_path)
    e = {k: v for k, v in os.environ.items()}
    e["LOADOUT_ENFORCE"] = "0"
    r = subprocess.run([sys.executable, str(GATE), "pre", "--host", "dsh"],
                       input=json.dumps(hook(proj, [], tool_name="write", tool_input={"file_path": "a.py"})),
                       capture_output=True, encoding="utf-8", env=e)
    assert r.returncode == 0 and r.stdout.strip() == ""
