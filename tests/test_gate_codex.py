"""Tests for scripts/gate_codex.py: the Codex CLI rollout ledger behind the loadout gate."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import gate  # noqa: E402
import gate_codex  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "codex-rollout.jsonl"


def rollout(tmp_path, lines, name="rollout-x.jsonl"):
    """Write a Codex rollout JSONL (LF) from a list of dicts or raw strings."""
    p = tmp_path / name
    out = [l if isinstance(l, str) else json.dumps(l) for l in lines]
    p.write_bytes(("\n".join(out) + "\n").encode("utf-8"))
    return p


def meta(cwd="C:\proj"):
    return {"type": "session_meta", "payload": {"id": "s1", "cwd": cwd}}


def item(it):
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": it}}


def user(text):
    return item({"type": "UserMessage", "content": [{"type": "text", "text": text}]})


def command(cmd):
    return item({"type": "CommandExecution", "command": ["pwsh.exe", "-Command", cmd], "cwd": "file:///C:/proj"})


def exec_call(js):
    return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": js}}


def test_fixture_facts():
    f = gate_codex.transcript_facts(FIXTURE)
    assert f.edited is True
    assert f.cwd.endswith("codex-probe2")
    assert f.blocks == 0
    assert f.invoked == set()  # the probe prompt mentions apply_patch, not a $skill


def test_skill_mention_and_command_name(tmp_path):
    p = rollout(tmp_path, [meta(), user("please $loadout this, then <command-name>/loopy</command-name>")])
    assert gate_codex.transcript_facts(p).invoked == {"loadout", "loopy"}


def test_write_shaped_command_is_an_edit(tmp_path):
    p = rollout(tmp_path, [meta(), command("echo hi > out.txt")])
    assert gate_codex.transcript_facts(p).edited is True


def test_read_only_command_is_not_an_edit(tmp_path):
    p = rollout(tmp_path, [meta(), command("git status --short")])
    f = gate_codex.transcript_facts(p)
    assert f.edited is False and f.cwd == "C:\proj"


def test_exec_fallback_apply_patch_and_exec_command(tmp_path):
    p = rollout(tmp_path, [meta(), exec_call('await tools.apply_patch("*** Begin Patch\n*** End Patch")')])
    assert gate_codex.transcript_facts(p).edited is True
    p = rollout(tmp_path, [meta(), exec_call('await tools.exec_command({"cmd":"git status"})')])
    assert gate_codex.transcript_facts(p).edited is False
    p = rollout(tmp_path, [meta(), exec_call('await tools.exec_command({"cmd":"sed -i s/a/b/ f"})')])
    assert gate_codex.transcript_facts(p).edited is True


def test_agent_text_does_not_count_as_invocation(tmp_path):
    p = rollout(tmp_path, [meta(), item({"type": "AgentMessage", "content": [{"type": "Text", "text": "$loadout"}]})])
    assert gate_codex.transcript_facts(p).invoked == set()


def test_blocks_count_consecutively_and_reset_on_skill(tmp_path):
    # recorded live: the injected block reason is a HookPrompt item, not a user message
    block = item({"type": "HookPrompt", "id": "m1", "fragments": [{"text": gate.STOP_REASON + ": stage (`loadout`)", "hookRunId": "stop:9:x"}]})
    p = rollout(tmp_path, [meta(), block, block])
    assert gate_codex.transcript_facts(p).blocks == 2
    p = rollout(tmp_path, [meta(), block, user("$loadout"), block])
    assert gate_codex.transcript_facts(p).blocks == 1


def test_tolerates_garbage(tmp_path):
    p = rollout(tmp_path, ["not json", "[1,2]", json.dumps({"type": "event_msg", "payload": "x"}),
                           json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": None}}),
                           command("touch a"), user("$loadout")])
    f = gate_codex.transcript_facts(p)
    assert f.edited is True and f.invoked == {"loadout"} and f.cwd is None
    assert gate_codex.transcript_facts(tmp_path / "missing.jsonl") == gate.Facts(set(), False, None, 0)
    assert gate_codex.transcript_facts(None) == gate.Facts(set(), False, None, 0)


def test_is_codex_transcript(tmp_path):
    assert gate_codex.is_codex_transcript(FIXTURE)
    claude = tmp_path / "s.jsonl"
    claude.write_bytes(b'{"type":"user","cwd":"C:\\p","message":{"role":"user","content":"hi"}}\n')
    assert not gate_codex.is_codex_transcript(claude)
    assert gate_codex.is_codex_transcript(rollout(tmp_path, ["garbage"], name="rollout-2026.jsonl"))
    assert gate_codex.is_codex_transcript(rollout(tmp_path, ["", meta()], name="s2.jsonl"))
    assert not gate_codex.is_codex_transcript(tmp_path / "nope.jsonl")
    assert not gate_codex.is_codex_transcript(None)


# ---------------------------------------------------------------- gate.py end to end with --host codex

import os
import subprocess

GATE = REPO / "scripts" / "gate.py"
LOADOUT = "# Loadout: x\n\n## Accepted\n- planning: `planner`\n- review: `reviewer`\n- situational, x: `unlazy`\n"


def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "LOADOUT.md").write_bytes(LOADOUT.encode("utf-8"))
    return proj


def run_gate(mode, hook, host=None):
    e = {k: v for k, v in os.environ.items() if k != "LOADOUT_ENFORCE"}
    args = [sys.executable, str(GATE), mode] + (["--host", host] if host else [])
    r = subprocess.run(args, input=json.dumps(hook), capture_output=True, encoding="utf-8", env=e)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def codex_hook(proj, t, mode, **extra):
    # exactly the shape Codex hands a hook (recorded live 2026-09-02): Claude-style tool_name/tool_input
    base = {"session_id": "s1", "turn_id": "t1", "transcript_path": str(t), "cwd": str(proj),
            "hook_event_name": "PreToolUse" if mode == "pre" else "Stop", "permission_mode": "bypassPermissions"}
    base.update(extra)
    return base


def file_change(proj):
    return item({"type": "FileChange", "changes": {str(proj / "a.py"): {"type": "add", "content": "x"}}})


def test_codex_pre_denies_write_before_stage_one_and_allows_after(tmp_path):
    proj = project(tmp_path)
    t = rollout(tmp_path, [meta(str(proj)), user("do the thing")])
    hook = codex_hook(proj, t, "pre", tool_name="Bash", tool_input={"command": "echo hi > a.txt"}, tool_use_id="e1")
    out = run_gate("pre", hook, host="codex")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "`planner`" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert run_gate("pre", codex_hook(proj, t, "pre", tool_name="Bash", tool_input={"command": "git status --short"}), host="codex") is None
    t = rollout(tmp_path, [meta(str(proj)), user("$planner then do the thing")])
    assert run_gate("pre", hook, host="codex") is None


def test_codex_stop_blocks_after_an_edit_with_stages_missing(tmp_path):
    proj = project(tmp_path)
    t = rollout(tmp_path, [meta(str(proj)), user("$planner go"), file_change(proj)])
    out = run_gate("stop", codex_hook(proj, t, "stop", stop_hook_active=False, last_assistant_message="done"), host="codex")
    assert out["decision"] == "block" and "review (`reviewer`)" in out["reason"] and "unlazy" not in out["reason"]
    t = rollout(tmp_path, [meta(str(proj)), user("$planner go"), user("$reviewer"), file_change(proj)])
    assert run_gate("stop", codex_hook(proj, t, "stop", stop_hook_active=True), host="codex") is None
    t = rollout(tmp_path, [meta(str(proj)), user("just a question"), command("git status --short")])
    assert run_gate("stop", codex_hook(proj, t, "stop"), host="codex") is None, "read-only session is never trapped"


def test_codex_transcript_is_auto_detected_without_host_flag(tmp_path):
    proj = project(tmp_path)
    t = rollout(tmp_path, [meta(str(proj)), user("do it")], name="rollout-2026-09-02T00-00-00-abc.jsonl")
    hook = codex_hook(proj, t, "pre", tool_name="Bash", tool_input={"command": "echo hi > a.txt"})
    assert run_gate("pre", hook)["hookSpecificOutput"]["permissionDecision"] == "deny"
    t = rollout(tmp_path, [meta(str(proj)), user("$planner do it")], name="rollout-2026-09-02T00-00-01-abc.jsonl")
    assert run_gate("pre", hook | {"transcript_path": str(t)}) is None


def test_codex_apply_patch_is_an_edit_and_the_surface_stays_operator_owned(tmp_path):
    proj = project(tmp_path)
    patch = "*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch"
    t = rollout(tmp_path, [meta(str(proj)), user("do it")])
    hook = codex_hook(proj, t, "pre", tool_name="apply_patch", tool_input={"command": patch}, tool_use_id="e1")
    assert run_gate("pre", hook, host="codex")["hookSpecificOutput"]["permissionDecision"] == "deny"
    t = rollout(tmp_path, [meta(str(proj)), user("$planner do it")])
    assert run_gate("pre", hook | {"transcript_path": str(t)}, host="codex") is None
    surface = "*** Begin Patch\n*** Update File: LOADOUT.md\n@@\n-- review: `reviewer`\n*** End Patch"
    out = run_gate("pre", hook | {"transcript_path": str(t), "tool_input": {"command": surface}}, host="codex")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "operator-owned" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_codex_stop_without_transcript_path_finds_the_rollout_by_session_id(tmp_path, monkeypatch):
    proj = project(tmp_path)
    home = tmp_path / "codex-home"
    sessions = home / "sessions" / "2026" / "09" / "02"
    sessions.mkdir(parents=True)
    sid = "01a0643e-4855-7422-a7b7-3f301d7bf153"
    rollout(sessions, [meta(str(proj)), user("$planner go"), file_change(proj)], name=f"rollout-2026-09-02T17-30-00-{sid}.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(home))
    hook = {"session_id": sid, "turn_id": "t1", "cwd": str(proj), "hook_event_name": "Stop",
            "permission_mode": "bypassPermissions", "stop_hook_active": False, "last_assistant_message": "done"}
    out = run_gate("stop", hook, host="codex")
    assert out and out["decision"] == "block" and "review (`reviewer`)" in out["reason"]
    assert gate_codex.find_rollout("no-such-session", home) is None


def test_reading_a_skill_file_counts_as_invoking_it(tmp_path):
    # recorded live: Codex has no skill event; the agent reads <skills root>/<name>/SKILL.md via a shell read
    read = item({"type": "CommandExecution", "command": ["pwsh.exe", "-Command", "Get-Content -Raw 'C:/u/.agents/skills/loadout/SKILL.md'"],
                 "cwd": "file:///C:/proj", "parsed_cmd": [{"type": "read", "cmd": "x", "name": "SKILL.md", "path": "C:/u/.agents/skills/loadout/SKILL.md"}],
                 "status": "failed"})
    t = rollout(tmp_path, [meta(), user("go"), read])
    facts = gate_codex.transcript_facts(t)
    assert facts.invoked == {"loadout"} and facts.edited is False
    other = item({"type": "CommandExecution", "command": ["pwsh.exe", "-Command", "cat README.md"], "cwd": "file:///C:/proj",
                  "parsed_cmd": [{"type": "read", "cmd": "cat README.md", "name": "README.md", "path": "C:/proj/README.md"}]})
    assert gate_codex.transcript_facts(rollout(tmp_path, [meta(), other])).invoked == set()


def hook_prompt(text):
    # recorded live 2026-09-02: Codex records an injected Stop-block reason as a HookPrompt item
    return item({"type": "HookPrompt", "id": "m1", "fragments": [{"text": text, "hookRunId": "stop:9:x"}]})


def test_stop_block_cap_counts_hook_prompt_items(tmp_path):
    proj = project(tmp_path)
    block = hook_prompt("Loadout gate: stages not run this session: review (`reviewer`). Invoke them, then stop.")
    base = [meta(str(proj)), user("$planner go"), file_change(proj)]
    t = rollout(tmp_path, base + [block] * (gate.STOP_BLOCK_CAP - 1))
    assert gate_codex.transcript_facts(t).blocks == gate.STOP_BLOCK_CAP - 1
    assert run_gate("stop", codex_hook(proj, t, "stop", stop_hook_active=True), host="codex")["decision"] == "block"
    t = rollout(tmp_path, base + [block] * gate.STOP_BLOCK_CAP)
    assert run_gate("stop", codex_hook(proj, t, "stop", stop_hook_active=True), host="codex") is None, "cap: allow"
    t = rollout(tmp_path, base + [block] * gate.STOP_BLOCK_CAP + [user("$reviewer")])
    assert gate_codex.transcript_facts(t).blocks == 0, "a skill invocation resets the run"


def test_apply_patch_move_to_surface_file_is_denied(tmp_path):
    proj = project(tmp_path)
    t = rollout(tmp_path, [meta(str(proj)), user("$planner go")])
    rename = "*** Begin Patch\n*** Update File: notes.md\n*** Move to: LOADOUT.md\n@@\n-a\n+b\n*** End Patch"
    hook = codex_hook(proj, t, "pre", tool_name="apply_patch", tool_input={"command": rename}, tool_use_id="e1")
    out = run_gate("pre", hook, host="codex")
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_hook_config_is_operator_owned_surface(tmp_path):
    proj = project(tmp_path)
    t = rollout(tmp_path, [meta(str(proj)), user("$planner go")])  # stage 1 done: only the surface is gated
    patch = lambda path: f"*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch"  # noqa: E731
    for target in [".codex/hooks.json", str(tmp_path / "home" / ".codex" / "config.toml"), "~/.codex/hooks.json"]:
        hook = codex_hook(proj, t, "pre", tool_name="apply_patch", tool_input={"command": patch(target)})
        out = run_gate("pre", hook, host="codex")
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", target
    hook = codex_hook(proj, t, "pre", tool_name="apply_patch", tool_input={"command": patch("config.toml")})
    assert run_gate("pre", hook, host="codex") is None, "a project's own config.toml is not enforcement surface"
    for cmd in ['Set-Content ~/.codex/hooks.json "{}"', "echo x > .codex/config.toml", "rm ~/.codex/config.toml"]:
        hook = codex_hook(proj, t, "pre", tool_name="Bash", tool_input={"command": cmd})
        out = run_gate("pre", hook, host="codex")
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
    hook = codex_hook(proj, t, "pre", tool_name="Bash", tool_input={"command": "cat ~/.codex/config.toml"})
    assert run_gate("pre", hook, host="codex") is None, "reading the surface after stage 1 is fine"
