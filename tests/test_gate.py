"""Tests for scripts/gate.py: the Claude Code enforcement gate, driven as a subprocess."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate.py"
sys.path.insert(0, str(REPO / "scripts"))
import gate  # noqa: E402


@pytest.fixture(autouse=True)
def _no_inherited_hatch(monkeypatch):
    """A test that calls gate.decide() in-process reads this process's environment, so an operator
    hatch set in the shell that launched pytest would silently turn enforcement off underneath it
    and the test would assert against a bypassed gate. run_gate() already strips it for the
    subprocess path; this covers the direct one. The hatch itself is still proved explicitly, by
    passing LOADOUT_ENFORCE=0 to a subprocess in test_silent_allow_without_loadout_or_with_hatch."""
    monkeypatch.delenv("LOADOUT_ENFORCE", raising=False)


def test_write_shaped_bash_commands():
    assert gate.write_shaped("cat > f.txt <<'EOF'")
    assert gate.write_shaped("echo hi >> log")
    assert gate.write_shaped("python - <<'EOF'")
    assert gate.write_shaped("sed -i 's/a/b/' f")
    assert gate.write_shaped("ls | tee out.txt")
    assert gate.write_shaped("git checkout -- f.py")
    assert gate.write_shaped("cd x && cp a b")
    assert gate.write_shaped("cmd 1> out") and gate.write_shaped("cmd &> out")
    assert not gate.write_shaped("python x/apply.py .") and gate.sensitive("python x/apply.py .")
    assert not gate.write_shaped("cat CLAUDE.md") and gate.sensitive("cat CLAUDE.md")
    assert not gate.write_shaped("python -m pytest -q")
    assert not gate.write_shaped("cmd 2>&1 | tail -3")
    assert not gate.write_shaped("cmd > /dev/null")
    assert not gate.write_shaped("cmd >/dev/null 2>&1")
    assert not gate.write_shaped("git status --short")


def test_mcp_action_tokens_not_substring_run():
    assert not gate.is_edit_tool("mcp__github__list_workflow_runs")
    assert not gate.is_edit_tool("mcp__x__runner_status")
    assert gate.is_edit_tool("mcp__github__run_workflow")
    assert gate.is_edit_tool("mcp__fs__write_file")
    assert gate.is_edit_tool("mcp__x__create_issue")
    assert not gate.is_edit_tool("mcp__fs__read_file")
    # the mutating word sits in the middle of both configured Composio executors, so no
    # first-/last-word rule can classify them
    assert gate.is_edit_tool("mcp__composio__COMPOSIO_MULTI_EXECUTE_TOOL")
    assert gate.is_edit_tool("mcp__composio__COMPOSIO_REMOTE_BASH_TOOL")
    assert not gate.is_edit_tool("mcp__composio__COMPOSIO_GET_TOOL_SCHEMAS")


def transcript(tmp_path, blocks, name="t.jsonl", cwd=None):
    """Write a JSONL transcript. Each item is a list of content blocks for one assistant turn,
    or a plain string for a user turn. `cwd` stamps the session's starting directory, which the
    gate reads from the first line that carries one."""
    p = tmp_path / name
    lines = []
    for item in blocks:
        if isinstance(item, str):
            line = {"type": "user", "message": {"role": "user", "content": item}}
        else:
            line = {"type": "assistant", "message": {"role": "assistant", "content": item}}
        if cwd and not lines:
            line["cwd"] = str(cwd)
        lines.append(json.dumps(line))
    if cwd and not lines:  # a transcript with no turns still records where the session began
        lines.append(json.dumps({"type": "user", "cwd": str(cwd),
                                 "message": {"role": "user", "content": "start"}}))
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))  # LF on every platform, 3.9-safe
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
    facts = gate.transcript_facts(t)
    assert facts.invoked == {"loadout", "superpowers:brainstorming"}
    assert facts.edited is False
    t = transcript(tmp_path, [[tool("Edit", file_path="a.py", old_string="x", new_string="y")]])
    assert gate.transcript_facts(t)[:2] == (set(), True)
    t = transcript(tmp_path, [[tool("Bash", command="cat > a.py <<'EOF'")]])
    assert gate.transcript_facts(t)[:2] == (set(), True)


def test_transcript_facts_tolerates_garbage(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_bytes(('not json\n{"message": 5}\n' + json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "x"}}]}}) + "\n").encode("utf-8"))
    assert gate.transcript_facts(p)[:2] == ({"x"}, False)
    assert gate.transcript_facts(tmp_path / "missing.jsonl")[:2] == (set(), False)
    assert gate.transcript_facts(None)[:2] == (set(), False)


# ---------------------------------------------------------------- decisions

LOADOUT = ("# Loadout: x\nHarness: claude-code | Project type: cli\nDate: 2026-09-02\n\n"
           "## Accepted\n- planning: `planner`\n- review: `reviewer`\n"
           "- situational, gated work: `unlazy`\n")


def assert_no_inherited_policy(root):
    """Fail loudly if a LOADOUT.md above the fixture would govern it.

    `find_loadout` walks to the filesystem root by design, and that is production behaviour worth
    keeping: a project's policy usually sits above the file being edited. The cost is that a
    policy anywhere above the basetemp -- this repo's own LOADOUT.md, if someone points
    `--basetemp` inside the tree -- silently governs every fixture built here, turning a "no
    policy, so allow" case into a deny for a reason nothing in the test names. The fixture
    asserts its own ground instead of narrowing the walk-up."""
    inherited = gate.find_loadout(root)
    assert inherited is None, (
        f"fixture root {root} inherits {inherited}; run pytest with --basetemp outside any tree "
        "carrying a LOADOUT.md. The walk-up is production behaviour, not the bug.")


def project(tmp_path, loadout=LOADOUT):
    assert_no_inherited_policy(tmp_path)  # before writing this fixture's own policy
    proj = tmp_path / "proj" / "sub"
    proj.mkdir(parents=True)
    if loadout is not None:
        (tmp_path / "proj" / "LOADOUT.md").write_bytes(loadout.encode("utf-8"))
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


def test_fixture_isolation_guard_is_not_vacuous(tmp_path):
    """The guard every fixture runs has to be able to fail, and the walk-up it guards against has
    to still work: same mechanism, one accidental and one deliberate."""
    root = tmp_path / "clean"
    root.mkdir()
    assert_no_inherited_policy(root)                    # ordinary fixture ground: nothing above it
    assert gate.find_loadout(root) is None

    outer = tmp_path / "inherited"
    inner = outer / "proj"
    inner.mkdir(parents=True)
    (outer / "LOADOUT.md").write_bytes(LOADOUT.encode("utf-8"))
    import pytest
    with pytest.raises(AssertionError, match="inherits"):
        assert_no_inherited_policy(inner)               # the same planted file the guard exists for
    assert gate.find_loadout(inner) == outer / "LOADOUT.md", "production walk-up is unchanged"


def test_nearest_policy_governs_and_the_walk_up_skips_directories_without_one(tmp_path):
    """Two policies on one path: the nearest governs, and its stage 1 -- not the outer one's -- is
    what the hook demands. Remove it and the outer policy governs from the same directory, so the
    walk-up is still crossing the intermediate directory that has no LOADOUT.md of its own."""
    assert_no_inherited_policy(tmp_path)
    outer = tmp_path / "outer"
    inner = outer / "inner"
    deep = inner / "a" / "b"                            # no LOADOUT.md of their own
    deep.mkdir(parents=True)
    (outer / "LOADOUT.md").write_bytes(
        b"# Loadout: outer\n\n## Accepted\n- planning: `outerplanner`\n")
    (inner / "LOADOUT.md").write_bytes(LOADOUT.encode("utf-8"))
    t = transcript(tmp_path, [])

    reason = run_gate("pre", pre_hook(deep, t))["hookSpecificOutput"]["permissionDecisionReason"]
    assert "`planner`" in reason and "outerplanner" not in reason
    assert gate.find_loadout(deep) == inner / "LOADOUT.md"

    (inner / "LOADOUT.md").unlink()
    reason = run_gate("pre", pre_hook(deep, t))["hookSpecificOutput"]["permissionDecisionReason"]
    assert "`outerplanner`" in reason
    assert gate.find_loadout(deep) == outer / "LOADOUT.md"

    # and the stage that governs is the one the governing file names: invoking it releases the edit
    after = transcript(tmp_path, [[skill("outerplanner")]], name="after.jsonl")
    assert run_gate("pre", pre_hook(deep, after)) is None


def test_pre_denies_edit_before_stage_one_and_allows_after(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[tool("Bash", command="git status")]])
    out = run_gate("pre", pre_hook(proj, t))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "`planner`" in reason and "planning" in reason and "LOADOUT_ENFORCE=0" in reason
    t = transcript(tmp_path, [[skill("planner")]])
    assert run_gate("pre", pre_hook(proj, t)) is None


def test_pre_gates_every_bash_command_until_stage_one(tmp_path):
    """No regex can tell what an arbitrary program writes, so the stage-1 prerequisite applies to
    every command; the write/read classification only decides things once stage 1 has run."""
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
    assert denied(pre_hook(proj, t, "Bash", command="python -m pytest -q"))
    assert denied(pre_hook(proj, t, "Bash", command="cat > a.py <<'EOF'"))
    after = transcript(tmp_path, [[skill("planner")]], name="after.jsonl")
    assert run_gate("pre", pre_hook(proj, after, "Bash", command="python -m pytest -q")) is None
    assert run_gate("pre", pre_hook(proj, after, "Bash", command="cat > a.py <<'EOF'")) is None
    # tools outside the gated set are never touched, at any stage
    assert run_gate("pre", pre_hook(proj, t, "Read", file_path="a.py")) is None


def test_session_start_dir_decides_the_policy_not_the_post_cd_cwd(tmp_path):
    """`cd` moves hook["cwd"], so a session could otherwise step into a directory holding a
    permissive LOADOUT.md and edit its own project by absolute path under those rules."""
    strict = project(tmp_path)                      # tmp_path/proj, binding: planner then reviewer
    lax = tmp_path / "lax"
    lax.mkdir()
    (lax / "LOADOUT.md").write_bytes(
        b"# Loadout: lax\n\n## Accepted\n- situational, anything: `nothing`\n")
    # the session began in the strict project; the agent has since cd'd into the permissive one
    t = transcript(tmp_path, [], cwd=strict)
    hook = {"cwd": str(lax), "transcript_path": str(t), "tool_name": "Edit",
            "tool_input": {"file_path": str(strict / "a.py"), "old_string": "a", "new_string": "b"}}
    assert denied(hook), "the permissive post-cd directory must not govern the session"
    # and the strict project's own stage still releases it
    after = transcript(tmp_path, [[skill("planner")]], name="after.jsonl", cwd=strict)
    hook["transcript_path"] = str(after)
    assert run_gate("pre", hook) is None


def test_session_start_dir_falls_back_when_it_has_no_loadout(tmp_path):
    """Only a starting directory that actually carries a LOADOUT.md takes precedence; otherwise
    the target path and the current cwd still resolve the policy as before."""
    proj = project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    t = transcript(tmp_path, [], cwd=outside)
    assert denied(pre_hook(proj, t, "Edit", file_path=str(proj / "a.py"), old_string="a", new_string="b"))


def test_unclassified_command_is_gated_until_stage_one(tmp_path):
    """A helper script is opaque to any regex: `python existing_writer.py` may write anything, so
    it waits for stage 1 like every other command. The script is never run here."""
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
    assert denied(pre_hook(proj, t, "Bash", command="python existing_writer.py"))
    after = transcript(tmp_path, [[skill("planner")]], name="after.jsonl")
    assert run_gate("pre", pre_hook(proj, after, "Bash", command="python existing_writer.py")) is None


def test_bootstrap_boundary_is_exact_not_blanket(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
    # activation files stay gated like any other mutation
    assert denied(pre_hook(proj, t, "Write", file_path=str(proj.parent / "LOADOUT.md"), content="x"))
    assert denied(pre_hook(proj, t, "Edit", file_path=str(proj.parent / "AGENTS.md"), old_string="a", new_string="b"))
    assert denied(pre_hook(proj, t, "Bash", command="cat > LOADOUT.md <<'EOF'"))
    assert denied(pre_hook(proj, t, "Bash", command="echo x >> CLAUDE.md"))
    # only the exact validated apply.py invocation (this skill's own file) is the bootstrap exception
    A = str(REPO / "scripts" / "apply.py")
    ok = [f'python "{A}" . --host claude-code', f'python3 "{A}" /p --loadout L.md --no-enforce',
          f'"{sys.executable}" "{A}" "C:\\proj"']
    for cmd in ok:
        assert run_gate("pre", pre_hook(proj, t, "Bash", command=cmd)) is None, cmd
    bad = [f'python "{A}" . --host claude-code && cat > LOADOUT.md', f'python "{A}" . ; rm -rf x',
           f'python "{A}" . > out', f'python "{A}" . --host claude-code --evil', f'python "{A}" . extra',
           "python x/notapply.py .", f'bash "{A}" .', f'python "{A}"', "python x/apply.py . --host claude-code"]
    for cmd in bad:
        assert denied(pre_hook(proj, t, "Bash", command=cmd)), cmd


def test_bootstrap_invocation_predicate():
    A = str(REPO / "scripts" / "apply.py")
    assert gate.bootstrap_invocation(f'python "{A}" . --host claude-code')
    assert gate.bootstrap_invocation(f'python "{A}" . --host claude-code --enforce-codex')
    assert gate.bootstrap_invocation(f'python "{A}" . --host dsh --enforce-dsh')
    assert gate.bootstrap_invocation(f'python3 "{A}" /p --loadout L.md --no-enforce')
    assert not gate.bootstrap_invocation(f'python "{A}" . | tee log')
    assert not gate.bootstrap_invocation(f'python "{A}" . $(id)')
    assert not gate.bootstrap_invocation(f'python "{A}" . `id`')
    assert not gate.bootstrap_invocation(f'python "{A}" . --host')
    assert not gate.bootstrap_invocation(f'python "{A}" . --host --no-enforce')
    assert not gate.bootstrap_invocation(f'python "{A}" . --host claude-code --evil')
    assert not gate.bootstrap_invocation(f'python "{A}" . extra')
    assert not gate.bootstrap_invocation(f'python x/apply.py . --host claude-code')


def test_slash_command_counts_as_invoked(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, ["<command-name>/planner</command-name>"])
    assert run_gate("pre", pre_hook(proj, t)) is None


def test_slash_command_counts_only_from_user_messages(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[{"type": "text", "text": "<command-name>/planner</command-name>"}]])
    assert denied(pre_hook(proj, t)), "assistant text must not count as an invocation"


def test_reading_the_enforcement_surface_is_not_an_edit(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[skill("planner")], [tool("Bash", command="cat CLAUDE.md"), tool("Read", file_path="AGENTS.md")]])
    assert run_gate("stop", stop_hook(proj, t)) is None, "read-only session must not be trapped"
    t = transcript(tmp_path, [])
    assert denied(pre_hook(proj, t, "Bash", command="cat CLAUDE.md")), "but before stage 1 it is still gated"


def test_pre_and_stop_through_subprocess_edge_cases(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [])
    after = transcript(tmp_path, [[skill("planner")]], name="after.jsonl")
    assert run_gate("pre", pre_hook(proj, after, "Bash", command="cmd 2>&1 >/dev/null")) is None
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    assert denied(pre_hook(proj, bad)), "garbage transcript: nothing invoked, edit still gated"
    assert run_gate("stop", stop_hook(proj, bad)) is None, "garbage transcript: no edits seen"


def test_broken_gate_fails_open_but_not_silently():
    r = subprocess.run([sys.executable, str(GATE), "pre"], input="not json", capture_output=True, encoding="utf-8")
    assert r.returncode == 0 and r.stdout == ""
    assert "loadout gate" in r.stderr.lower() and "Traceback" in r.stderr


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


def test_list_workflow_runs_does_not_arm_stop(tmp_path):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[skill("planner")],
                              [tool("mcp__github__list_workflow_runs")]])
    assert gate.transcript_facts(t).edited is False
    assert run_gate("stop", stop_hook(proj, t)) is None
    t = transcript(tmp_path, [[skill("planner")],
                              [tool("mcp__github__run_workflow")]])
    assert gate.transcript_facts(t).edited is True
    out = run_gate("stop", stop_hook(proj, t))
    assert out and out["decision"] == "block"


def test_unreadable_policy_warns_and_keeps_host_semantics(tmp_path, monkeypatch, capsys):
    proj = project(tmp_path)
    t = transcript(tmp_path, [[tool("Edit", file_path="a.py")]])
    orig = Path.read_text

    def boom(self, *a, **k):
        if self.name == "LOADOUT.md":
            raise OSError("denied")
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    assert gate.decide("pre", pre_hook(proj, t)) is None
    err = capsys.readouterr().err
    assert "unreadable policy" in err and "LOADOUT.md" in err


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
