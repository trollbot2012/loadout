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
