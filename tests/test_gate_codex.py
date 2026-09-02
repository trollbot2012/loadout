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
    block = user("Stop hook feedback: " + gate.STOP_REASON + ": stage (`loadout`)")
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
