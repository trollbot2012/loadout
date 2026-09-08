"""OC1-CX qualification reproducers for the Codex enforcement loop.

The six groups of RESEARCH/LOADOUT_OC1_QUALIFICATION_2026_09_06/QUALIFICATION_PACKET.md Q3,
derived against accepted base 8e68ecc. Every fixture is synthetic and lives under a single
disposable root R inside pytest's tmp_path: no host is launched, no installed profile,
credential or real LOADOUT.md is read, and `assert_inside` pins the fixture tree to R.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import gate  # noqa: E402
import gate_codex  # noqa: E402

GATE = REPO / "scripts" / "gate.py"

STAGE = "superpowers:systematic-debugging"
LEAF = "systematic-debugging"
ENFORCED = ("# Loadout: cx\nEnforcement: codex gate registered\n\n"
            "## Accepted\n- diagnosis: `" + STAGE + "`\n")
PROSE_ONLY = "# Loadout: cx\nEnforcement: prose only\n\n## Accepted\n- diagnosis: `" + STAGE + "`\n"
# the Codex-specific half of the pre denial: the withdrawn allowance left a limitation the
# operator has to be told about, and the denial is the only place it is surfaced
NOTE_MARK = "admits no such read on the enforced pending-stage path"
# and the note has to stay accurate about the one command that *is* admitted pre-stage
BOOTSTRAP_MARK = "the one Bash command it lets through before the stage is the validated apply.py"


# ---------------------------------------------------------------- root R and containment

def qual_root(tmp_path):
    """R with the Q0 layout: home/config/data/cache/tmp plus projects/A and projects/B under a
    synthetic repository boundary. Returns (R, A, B)."""
    R = tmp_path / "R"
    for leaf in ("home", "config", "data", "cache", "tmp", "projects"):
        (R / leaf).mkdir(parents=True)
    (R / "projects" / ".git").mkdir()  # the synthetic repository boundary above A and B
    A, B = R / "projects" / "A", R / "projects" / "B"
    A.mkdir()
    B.mkdir()
    for p in (A, B, R / "tmp", R / "home"):
        assert_inside(R, p)
    return R, A, B


def assert_inside(R, p):
    """Every fixture path this harness creates or names must resolve inside R."""
    r = os.path.normcase(str(Path(R).resolve()))
    q = os.path.normcase(str(Path(p).resolve()))
    assert q == r or q.startswith(r + os.sep), "%s escapes %s" % (p, R)
    return p


def write_loadout(d, text):
    p = Path(d) / "LOADOUT.md"
    p.write_bytes(text.encode("utf-8"))
    return p


def plugin_skill(R, plugin="superpowers", name=LEAF, market="market", version="6.3.0", sep="/"):
    """<R>/home/.codex/plugins/cache/<market>/<plugin>/<version>/skills/<name>/SKILL.md."""
    p = R / "home" / ".codex" / "plugins" / "cache" / market / plugin / version / "skills" / name / "SKILL.md"
    assert_inside(R, p.parent)
    s = str(p).replace("\\", "/")
    return s if sep == "/" else s.replace("/", "\\")


def standalone_skill(R, name=LEAF, sep="/"):
    """<R>/home/.agents/skills/<name>/SKILL.md: no plugin, so only a leaf identity exists."""
    p = R / "home" / ".agents" / "skills" / name / "SKILL.md"
    assert_inside(R, p.parent)
    s = str(p).replace("\\", "/")
    return s if sep == "/" else s.replace("/", "\\")


# ---------------------------------------------------------------- rollout fixtures

def rollout(d, lines, name="rollout-cx.jsonl"):
    p = Path(d) / name
    body = "\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines)
    p.write_bytes((body + "\n").encode("utf-8"))
    return p


def meta(cwd, sid="s1"):
    return {"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd)}}


def item(it):
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": it}}


def user(text):
    return item({"type": "UserMessage", "content": [{"type": "text", "text": text}]})


def agent(text):
    return item({"type": "AgentMessage", "content": [{"type": "text", "text": text}]})


_MISSING = object()  # "the key is absent", distinct from an explicit null


def command(cmd, parsed_cmd=None, status="completed", exit_code=0):
    """A CommandExecution item. The outcome defaults to the one the recorded rollout shows for a
    command that worked: status="completed" with exit_code=0. Pass `_MISSING` to omit a key."""
    it = {"type": "CommandExecution", "command": ["pwsh.exe", "-Command", cmd]}
    if parsed_cmd is not None:
        it["parsed_cmd"] = parsed_cmd
    if status is not _MISSING:
        it["status"] = status
    if exit_code is not _MISSING:
        it["exit_code"] = exit_code
    return item(it)


def read_cmd(path, shell="pwsh"):
    """The two prerequisite read forms Q3 pins, verbatim."""
    return ("Get-Content -LiteralPath '%s'" % path) if shell == "pwsh" else ("cat '%s'" % path)


def read_of(path, shell="pwsh", status="completed", exit_code=0):
    """A CommandExecution recording a read of `path`, in both real forms Codex normalises:
    PowerShell `Get-Content -LiteralPath` and POSIX `cat`."""
    cmd = read_cmd(path, shell)
    return command(cmd, parsed_cmd=[{"type": "read", "cmd": cmd, "name": "SKILL.md", "path": path}],
                   status=status, exit_code=exit_code), cmd


def search_of(path):
    cmd = "rg -n %s '%s'" % (LEAF, path)
    return command(cmd, parsed_cmd=[{"type": "search", "cmd": cmd, "query": LEAF, "path": path}])


def file_change(proj, name="a.py"):
    return item({"type": "FileChange", "changes": {str(Path(proj) / name): {"type": "add", "content": "x"}}})


def exec_call(js):
    return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": js}}


# ---------------------------------------------------------------- gate invocation

_KEEP_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "SYSTEMDRIVE")


def child_env(R, **extra):
    """A minimal explicit environment for a gate child, with every profile-shaped variable pointed
    at a synthetic directory inside R.

    Nothing is inherited except the interpreter's own loader needs (`_KEEP_ENV`): no LOADOUT_ENFORCE,
    no real HOME/USERPROFILE/APPDATA/XDG root, no real CODEX_HOME, no real TEMP. That bounds where
    *this harness's fixtures* live and which profile the gate can resolve. It is not host isolation:
    the child is an ordinary process and could still reach the real filesystem by absolute path.

    PATH is the one inherited value this rebuilds rather than copies: see `child_path`.
    """
    home = assert_inside(R, R / "home")
    e = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    e["PATH"] = child_path()
    e.update({"HOME": str(home), "USERPROFILE": str(home),
              "APPDATA": str(assert_inside(R, R / "config")),
              "LOCALAPPDATA": str(assert_inside(R, R / "data")),
              "XDG_CONFIG_HOME": str(R / "config"), "XDG_DATA_HOME": str(R / "data"),
              "XDG_CACHE_HOME": str(assert_inside(R, R / "cache")),
              "CODEX_HOME": str(assert_inside(R, R / "home" / ".codex")),
              "TEMP": str(assert_inside(R, R / "tmp")), "TMP": str(R / "tmp"),
              "TMPDIR": str(R / "tmp")})
    e.update(extra)
    return e


def run_gate(mode, hook, R, host=None, **env):
    args = [sys.executable, str(GATE), mode] + (["--host", host] if host else [])
    r = subprocess.run(args, input=json.dumps(hook), capture_output=True, encoding="utf-8",
                       env=child_env(R, **env))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def codex_hook(proj, t, mode, **extra):
    base = {"session_id": "s1", "turn_id": "t1", "transcript_path": str(t), "cwd": str(proj),
            "host": "codex", "hook_event_name": "PreToolUse" if mode == "pre" else "Stop",
            "permission_mode": "bypassPermissions"}
    base.update(extra)
    return base


def denied(out):
    return out is not None and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def blocked(out):
    return out is not None and out.get("decision") == "block"


# ================================================================ CX-1
# session_meta(cwd=A), explanation only, then a harmless `echo hello`, no FileChange.
# Nothing may be fabricated: no edit, no skill credit. pre and stop are recorded separately.

def test_cx1_explanation_and_harmless_command_fabricate_no_edit_and_no_credit(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    t = rollout(R / "tmp", [meta(A), user("explain what is going on"),
                            agent("Here is the explanation; nothing was changed."),
                            command("echo hello", parsed_cmd=[{"type": "unknown", "cmd": "echo hello"}])])
    f = gate_codex.transcript_facts(t)
    assert f.invoked == set(), "no skill was loaded"
    assert f.edited is False, "echo hello is not write-shaped"
    assert f.cwd == str(A)
    assert f.blocks == 0

    # pre: before stage 1 no command is exempt, read-only shape included
    pre = run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                     tool_input={"command": "echo hello"}), R, host="codex")
    assert denied(pre) and STAGE in reason(pre)
    # stop: an unedited session is never trapped, recorded independently of the pre decision
    assert run_gate("stop", codex_hook(A, t, "stop", stop_hook_active=False), R, host="codex") is None


# ================================================================ CX-2
# A real FileChange in a prior turn, an explanation-only next turn while the qualified stage is
# missing, then a completed CommandExecution reading the plugin SKILL.md.

def test_cx2_prior_edit_survives_and_the_qualified_plugin_read_releases_the_stop(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    turn1 = [meta(A), user("fix it"), file_change(A)]
    t1 = rollout(R / "tmp", turn1, name="rollout-cx2-t1.jsonl")
    f1 = gate_codex.transcript_facts(t1)
    assert f1.edited is True and f1.invoked == set()
    out = run_gate("stop", codex_hook(A, t1, "stop"), R, host="codex")
    assert blocked(out) and ("diagnosis (`%s`)" % STAGE) in out["reason"]

    # next turn: explanation only. The historical edit fact must survive and the block must repeat.
    turn2 = turn1 + [user("why did that happen?"), agent("Because the parser drops the namespace.")]
    t2 = rollout(R / "tmp", turn2, name="rollout-cx2-t2.jsonl")
    f2 = gate_codex.transcript_facts(t2)
    assert f2.edited is True, "a later explanation-only turn must not erase a real edit"
    assert f2.invoked == set()
    assert blocked(run_gate("stop", codex_hook(A, t2, "stop"), R, host="codex"))

    # the agent then actually loads the skill: a completed read of the plugin SKILL.md
    read, cmd = read_of(plugin_skill(R))
    t3 = rollout(R / "tmp", turn2 + [read], name="rollout-cx2-t3.jsonl")
    f3 = gate_codex.transcript_facts(t3)
    assert f3.edited is True, "the historical edit is still the reason stages are required"
    assert STAGE in f3.invoked, "qualified load not credited; invoked=%r cmd=%r" % (f3.invoked, cmd)
    assert run_gate("stop", codex_hook(A, t3, "stop"), R, host="codex") is None, "completion must be released"


# ================================================================ CX-3
# The pre-tool prerequisite read and its POSIX pair, both slash styles.

def test_cx3_both_shells_and_both_slash_styles_are_the_same_qualified_load(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    for shell in ("pwsh", "posix"):
        for sep in ("/", "\\"):
            path = plugin_skill(R, sep=sep)
            read, cmd = read_of(path, shell=shell)
            t = rollout(R / "tmp", [meta(A), file_change(A), read],
                        name="rollout-cx3-%s-%s.jsonl" % (shell, "fwd" if sep == "/" else "bck"))
            f = gate_codex.transcript_facts(t)
            assert STAGE in f.invoked, "shell=%s sep=%r cmd=%r invoked=%r" % (shell, sep, cmd, f.invoked)
            assert LEAF in f.invoked, "the leaf identity is retained alongside the qualified one"
            assert run_gate("stop", codex_hook(A, t, "stop"), R, host="codex") is None


def test_cx3_a_read_the_gate_denied_never_reaches_the_ledger(tmp_path):
    # a denied pre-tool read produces no item_completed at all, so it cannot earn load credit.
    # The read denied here is a *near miss* of the admitted form: same skill, but `-Raw` instead
    # of the pinned `-LiteralPath`, so it is not one of the two forms Q3 pins.
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    path = plugin_skill(R)
    t = rollout(R / "tmp", [meta(A), user("read the skill")], name="rollout-cx3-denied.jsonl")
    out = run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                     tool_input={"command": "Get-Content -Raw '%s'" % path}),
                   R, host="codex")
    assert denied(out) and STAGE in reason(out)
    assert gate_codex.transcript_facts(t).invoked == set()


# ---- CX-3 prerequisite admission: withdrawn, and the controls that keep it withdrawn ---------
# CX2 admitted one command before stage 1 -- a fully tokenised `cat <path>` or
# `Get-Content -LiteralPath <path>` naming the pending stage's own SKILL.md -- to close the Codex
# loop, where a skill load *is* a shell read and there is no host-side Stop cap. Independent
# execution disproved the read-only premise: a `cat` earlier on PATH satisfied the same spelling
# and the same suffix, ran an unrelated body and reported exit 0
# (WORK_LOGS/LOADOUT_OC1_CX2_CODEX_EVIDENCE_2026_09_06/path-hijack-repro.txt). Command spelling is
# not a read primitive, so the allowance is withdrawn outright rather than narrowed: a `$`
# blacklist would not have stopped that reproducer. The loop it closed is therefore open again --
# see test_cx3_the_withdrawal_reopens_the_codex_prerequisite_deadlock, which pins that cost
# instead of hiding it. CX-3's recovery flow stays PARTIAL until an observed host-native read
# primitive is qualified.

def admit(R, A, cmd, host="codex", t=None, **extra):
    """The pre decision for `cmd` on a session that has not yet run stage 1."""
    t = t or rollout(R / "tmp", [meta(A), user("go")], name="rollout-cx3-admit.jsonl")
    return run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                      tool_input={"command": cmd}, **extra), R, host=host)


def test_cx3_the_two_formerly_pinned_read_forms_are_no_longer_admitted(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    for shell in ("pwsh", "posix"):
        for sep in ("/", "\\"):
            cmd = read_cmd(plugin_skill(R, sep=sep), shell=shell)
            out = admit(R, A, cmd)
            assert denied(out), "the withdrawn form must stay denied: shell=%s sep=%r %r" % (shell, sep, cmd)
            assert STAGE in reason(out), "the denial must still name the pending stage"


def test_cx3_the_denial_explains_the_codex_limitation_without_naming_a_runnable_command(tmp_path):
    # P1 product gap: a denial that cannot explain the recovery limitation strands the operator.
    # It must say the stage is still required, that no shell command clears it on this host, and
    # point at the operator hatch -- and it must not print a command that is in fact denied.
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    r = reason(admit(R, A, read_cmd(plugin_skill(R))))
    assert STAGE in r, "the pending stage is still required and must be named"
    assert NOTE_MARK in r, "the denial must state that no shell command clears the stage here"
    # and it must not overclaim while doing it: one command *is* admitted before stage 1, and the
    # earlier unqualified "no shell command is admitted" was simply untrue (CX3 Codex report
    # 31BB3A69...1088F). Naming the exception is what makes the rest of the sentence checkable.
    assert BOOTSTRAP_MARK in r, "the denial must name the one pre-stage exception, not deny it exists"
    assert "LOADOUT_ENFORCE=0" in r, "the documented operator recovery path must be named"
    for form in ("cat ", "Get-Content"):
        assert form not in r, "a denied command must not be offered as runnable: %r" % form
    # the same limitation note is Codex-specific and must not leak into another host's denial
    t = claude_transcript(R / "tmp", A, name="session-cx3-note.jsonl")
    hook = {"session_id": "s1", "transcript_path": str(t), "cwd": str(A),
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": "echo hi"}}
    cc = reason(run_gate("pre", hook, R, host="claude-code"))
    assert STAGE in cc and NOTE_MARK not in cc, "the Codex note must not appear on Claude Code"
    assert BOOTSTRAP_MARK not in cc, "neither must the Codex bootstrap disclosure"


def test_cx4_the_denials_bootstrap_exception_is_the_behaviour_not_just_wording(tmp_path):
    """The note says one command runs before stage 1. This checks the gate agrees.

    Kept as a pair because a wording assertion alone can drift into a false claim: if the
    bootstrap were ever denied, the sentence would be wrong in the other direction.
    """
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    boot = "python '%s' . --host codex" % str(Path(gate.apply.__file__).resolve()).replace("\\", "/")
    assert admit(R, A, boot) is None, "the validated apply.py bootstrap must still run pre-stage"
    # and it is the *only* one: an ordinary command, and the withdrawn read, are both denied
    assert denied(admit(R, A, "echo hi"))
    assert denied(admit(R, A, read_cmd(plugin_skill(R))))
    # the bootstrap is not a stage-satisfying command either -- it leaves the Stop blocked
    t = rollout(R / "tmp", [meta(A), file_change(A), command(boot)],
                name="rollout-cx4-bootstrap.jsonl")
    assert blocked(run_gate("stop", codex_hook(A, t, "stop"), R, host="codex")), \
        "running the bootstrap must not credit the pending stage"


def test_cx3_the_withdrawal_reopens_the_codex_prerequisite_deadlock(tmp_path):
    # The honest cost of the withdrawal, pinned rather than left implicit: a Codex session that
    # already edited has no in-session move that clears the stage. Every command is denied, Stop
    # keeps blocking however long it runs, and only the operator hatch ends it. This test fails
    # the day a qualified host-native primitive is added, which is exactly when it should be
    # re-read: CX-3's recovery flow is PARTIAL until then.
    # Ceiling: the commands below are a sample, so this is a maintenance tripwire over the forms
    # named here -- it will not notice a future host-native primitive that is not one of them. It
    # says "these stay denied", never "nothing else could ever clear the stage".
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    t = rollout(R / "tmp", [meta(A), file_change(A)], name="rollout-cx3-deadlock.jsonl")
    for cmd in (read_cmd(plugin_skill(R), "pwsh"), read_cmd(plugin_skill(R), "posix"), "whoami"):
        assert denied(admit(R, A, cmd, t=t)), "no command clears stage 1 on Codex: %r" % cmd
    for n in (0, gate.STOP_BLOCK_CAP, gate.STOP_BLOCK_CAP + 25):
        blocks = [item({"type": "HookPrompt", "id": "b%d" % i,
                        "fragments": [{"text": "Loadout gate: stages not run this session"}]})
                  for i in range(n)]
        tn = rollout(R / "tmp", [meta(A), file_change(A)] + blocks,
                     name="rollout-cx3-deadlock-%d.jsonl" % n)
        assert blocked(run_gate("stop", codex_hook(A, tn, "stop"), R, host="codex")), \
            "Codex has no host cap; the gate must keep blocking after %d" % n
    # the operator hatch is the whole of the recovery, and it does release
    assert run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash", tool_input={"command": "whoami"}),
                    R, host="codex", LOADOUT_ENFORCE="0") is None


def test_cx3_a_completed_stage_is_still_what_releases_the_pre_gate(tmp_path):
    # The withdrawal removes an exception, not the contract: once the ledger records a successful
    # load, pre allows again and Stop still requires the remaining stage.
    R, A, _ = qual_root(tmp_path)
    two = ("# Loadout: cx\n\n## Accepted\n- diagnosis: `" + STAGE + "`\n"
           "- review: `superpowers:code-review`\n")
    write_loadout(A, two)
    ok, _ = read_of(plugin_skill(R))
    t = rollout(R / "tmp", [meta(A), file_change(A), ok], name="rollout-cx3-stage2.jsonl")
    assert admit(R, A, "whoami", t=t) is None
    assert blocked(run_gate("stop", codex_hook(A, t, "stop"), R, host="codex")), "stage 2 still required"


# Every one of these was already denied under CX2, and stays denied now. The block is kept, and
# extended, because it is the record of *what* CX2's grammar did and did not reach: the expansion
# rows below are the ones reviewers showed could tokenise to the permitted shape and satisfy the
# suffix match, so with the allowance gone they are the regression that would notice it coming
# back in a narrower disguise. The two withdrawn forms themselves are covered separately.
PRE_STAGE_DENIALS = [
    ("wrong flag",            "Get-Content -Raw '%(p)s'"),
    ("no flag",               "Get-Content '%(p)s'"),
    ("extra flag",            "Get-Content -LiteralPath '%(p)s' -Raw"),
    ("extra argument",        "cat '%(p)s' '%(p)s'"),
    ("trailing command",      "Get-Content -LiteralPath '%(p)s'; whoami"),
    ("leading command",       "whoami; cat '%(p)s'"),
    ("and-chained",           "cat '%(p)s' && echo x"),
    ("piped",                 "Get-Content -LiteralPath '%(p)s' | tee out.txt"),
    ("redirected",            "cat '%(p)s' > out.txt"),
    ("appended",              "cat '%(p)s' >> out.txt"),
    ("substitution",          "cat $(echo '%(p)s')"),
    ("backtick substitution", "cat `echo %(p)s`"),
    ("brace substitution",    "cat ${SKILL:-'%(p)s'}"),
    ("newline second line",   "cat '%(p)s'\necho x"),
    ("executable by path",    "/bin/cat '%(p)s'"),
    ("executable with suffix", "cat.exe '%(p)s'"),
    ("shell indirection",     "sh -c \"cat '%(p)s'\""),
    ("script indirection",    "python read.py '%(p)s'"),
    ("wildcard path",         "cat '%(star)s'"),
    ("path mention only",     "echo \"read %(p)s\""),
    ("unbalanced quoting",    "cat '%(p)s"),
    # expansion forms: unexpanded text ends in the qualified suffix, so under CX2's grammar these
    # tokenised to the permitted shape and matched. The shell, not the gate, decides what they name.
    ("posix variable",        "cat $HOME/%(tail)s"),
    ("posix braced variable", "cat ${HOME}/%(tail)s"),
    ("powershell env var",    "Get-Content -LiteralPath $env:HOME/%(tail)s"),
    ("powershell variable",   "Get-Content -LiteralPath $P/%(tail)s"),
    ("tilde home",            "cat ~/%(tail)s"),
    ("tilde user",            "cat ~someone/%(tail)s"),
    ("brace expansion",       "cat {%(p)s,other}"),
    ("double-quoted variable", "cat \"$P/%(tail)s\""),
    ("double-quoted env var", "Get-Content -LiteralPath \"$env:HOME/%(tail)s\""),
]


@pytest.mark.parametrize("label,template", PRE_STAGE_DENIALS,
                         ids=[r[0].replace(" ", "-") for r in PRE_STAGE_DENIALS])
def test_cx3_no_shell_command_is_admitted_before_stage_one(tmp_path, label, template):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    path = plugin_skill(R)
    star = path.replace("/6.3.0/", "/*/")
    tail = path.split("/plugins/", 1)[1] if "/plugins/" in path else path
    out = admit(R, A, template % {"p": path, "star": star, "tail": "plugins/" + tail})
    assert denied(out), "%s must stay denied: %r" % (label, template)
    assert STAGE in reason(out), "%s: the denial must still name the pending stage" % label


def test_cx3_no_path_identity_earns_a_pre_stage_exception(tmp_path):
    # Identity was the CX2 allowance's whole guard. With the allowance withdrawn it no longer
    # buys anything at pre: the stage's own SKILL.md is denied exactly like every look-alike.
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    others = {
        "the pending stage itself":  plugin_skill(R),
        "another plugin's copy": plugin_skill(R, plugin="otherpack", version="1.0.0"),
        "another leaf":          plugin_skill(R, name="code-review"),
        "a standalone root":     standalone_skill(R),          # leaf only, never the qualified key
        "a look-alike file":     plugin_skill(R).replace("SKILL.md", "SKILL.md.bak"),
        "a directory":           plugin_skill(R).rsplit("/", 1)[0],
    }
    for label, p in others.items():
        for shell in ("pwsh", "posix"):
            assert denied(admit(R, A, read_cmd(p, shell))), "%s (%s) must stay denied" % (label, shell)


def test_cx3_the_pre_gate_is_uniform_across_hosts_and_never_releases_the_surface(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    cmd = read_cmd(plugin_skill(R))
    # Claude Code loads a skill through its own tool, not a shell read; Codex now behaves the same
    t = claude_transcript(R / "tmp", A, name="session-cx3.jsonl")
    hook = {"session_id": "s1", "transcript_path": str(t), "cwd": str(A),
            "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}}
    assert denied(run_gate("pre", hook, R, host="claude-code"))
    assert denied(admit(R, A, cmd))
    # and the operator-owned surface check still runs first, at every stage
    assert denied(admit(R, A, "echo x >> gate.py"))


# ---- CX-3 observable flow: what the gate denies, and what a recorded load still releases -----
# The parser fixtures above prove identity extraction. These execute real shells, for two
# separate purposes: to prove the withdrawn command really is denied and really does not run,
# and to prove the credit half of the loop -- a real read, a real exit code, the ledger -- is
# untouched by the withdrawal.

_SH_CANDIDATES = (r"C:\Program Files\Git\usr\bin\sh.exe", r"C:\Program Files\Git\bin\sh.exe")
_PWSH_CANDIDATES = (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",)
# `cat` is not decoration: the POSIX read form the CX rows execute is `cat '<path>'`, so the
# shell alone is not enough -- the shell has to be able to find that child.
_CAT_CANDIDATES = (r"C:\Program Files\Git\usr\bin\cat.exe",)


def _resolve_exe(name, candidates):
    """PATH first, then the known install roots. Returns (path or None, what was searched).
    A PATH lookup alone made the POSIX rows skip on a host where `sh.exe` was installed but not
    on PATH, and the skip named the wrong reason, so the searched locations are reported."""
    searched = ["PATH lookup %r" % name]
    exe = shutil.which(name)
    for c in candidates:
        if exe:
            break
        searched.append(c)
        if Path(c).is_file():
            exe = c
    return exe, searched


PWSH, PWSH_SEARCHED = _resolve_exe("powershell", _PWSH_CANDIDATES)
POSIX_SH, POSIX_SEARCHED = _resolve_exe("sh", _SH_CANDIDATES)
CAT, CAT_SEARCHED = _resolve_exe("cat", _CAT_CANDIDATES)
SHELLS = {"pwsh": (PWSH, lambda c: [PWSH, "-NoProfile", "-NonInteractive", "-Command", c]),
          "posix": (POSIX_SH, lambda c: [POSIX_SH, "-c", c])}
SEARCHED = {"pwsh": PWSH_SEARCHED, "posix": POSIX_SEARCHED}
# The tools a row needs are resolved here, not assumed to be on whatever PATH pytest inherited:
# `powershell` supplies its own `Get-Content`, but the POSIX form needs a separate `cat`.
NEEDS = {"pwsh": (("powershell", PWSH, PWSH_SEARCHED),),
         "posix": (("sh", POSIX_SH, POSIX_SEARCHED), ("cat", CAT, CAT_SEARCHED))}
# Fixed at import from what was actually resolved, so a later PATH change cannot take a tool away
# from a child. Order is resolution order and does not depend on the operator's PATH.
_TOOL_DIRS = []
for _exe in (POSIX_SH, CAT, PWSH):
    if _exe and str(Path(_exe).parent) not in _TOOL_DIRS:
        _TOOL_DIRS.append(str(Path(_exe).parent))


def child_path():
    """The PATH a gate/shell child gets: the resolved tool directories, then the inherited PATH.

    Copying the parent PATH verbatim made the POSIX rows depend on how the operator's shell was
    arranged. Codex's independent CX3 run resolved `sh.exe` by absolute path from a parent PATH
    with no Git `usr\\bin`, and the read then died in the shell's child: `rc=127`, empty stdout
    (WORK_LOGS/LOADOUT_OC1_CX3_CODEX_VERIFIED_REVIEW_2026_09_06.md, reproduced before this fix).
    Whatever this module resolved is what the child can reach.

    The inherited PATH is kept behind the resolved directories so ordinary host tools still work;
    a row that needs something ahead of them -- the hijack negative -- prepends it explicitly.
    """
    inherited = os.environ.get("PATH", "")
    return os.pathsep.join(_TOOL_DIRS + ([inherited] if inherited else []))


def need_shell(shell):
    """Skip only when a required executable does not exist anywhere we look. A command that runs
    and fails is a failure, never a skip."""
    for name, exe, searched in NEEDS[shell]:
        if not exe:
            pytest.skip("no %s executable for the %s rows; searched: %s"
                        % (name, shell, "; ".join(searched)))
    return SHELLS[shell][0]


def real_run(shell, cmd, R, **env):
    """Run `cmd` in the pinned shell inside R's environment. Returns (exit_code, stdout)."""
    exe, argv = SHELLS[shell]
    r = subprocess.run(argv(cmd), capture_output=True, encoding="utf-8", errors="replace",
                       env=child_env(R, **env), cwd=str(R / "tmp"))
    return r.returncode, r.stdout


def hijack_bin(R, marker, sentinel):
    """A `cat` on PATH that ignores its argument, writes `sentinel` and exits 0.

    This is the frozen Codex reproducer
    (WORK_LOGS/LOADOUT_OC1_CX2_CODEX_EVIDENCE_2026_09_06/path-hijack-repro.txt) rebuilt inside R.
    It is why the pinned-spelling allowance was withdrawn rather than narrowed: the command text
    is byte-identical to the form CX2 admitted, and nothing in the command text distinguishes it.
    Bare `Get-Content` is unproven for the same reason and is not kept as a substitute."""
    d = assert_inside(R, R / "tmp" / "hijack-bin")
    d.mkdir(parents=True, exist_ok=True)
    p = assert_inside(R, d / "cat")
    p.write_text("#!/bin/sh\nprintf '%s\\n' '" + marker + "'\n"
                 "printf 'changed' > '" + str(sentinel).replace("\\", "/") + "'\nexit 0\n",
                 encoding="utf-8", newline="\n")
    p.chmod(0o755)
    return d


def completion_of(cmd, path, code):
    """The completion envelope for a command that really ran, carrying its real exit code."""
    return command(cmd, parsed_cmd=[{"type": "read", "cmd": cmd, "name": "SKILL.md", "path": path}],
                   status="completed" if code == 0 else "failed", exit_code=code)


def test_cx3_flow_the_frozen_path_replacement_is_denied_and_its_body_never_runs(tmp_path):
    """The independent negative that withdrew the allowance, kept as a standing control.

    Codex ran the exact form CX2 admitted with a replacement `cat` earlier on PATH: admitted,
    exit 0, `HIJACKED` on stdout, an unrelated sentinel written. The gate must now deny it, and
    because it denies it the body must never run. The reproducer is then fired deliberately, so
    this control cannot pass because the hijack was inert -- it is the denial that stops it,
    not the spelling.
    """
    need_shell("posix")
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    path = plugin_skill(R)
    body = assert_inside(R, Path(path))
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text("# %s\nreal-skill-body-9b2e71\n" % LEAF, encoding="utf-8")
    sentinel = assert_inside(R, A / "hijack-sentinel.txt")
    sentinel.write_bytes(b"before\n")
    hb = hijack_bin(R, "HIJACKED", sentinel)
    # ahead of the resolved tool directories, not merely ahead of whatever the parent had: the
    # replacement has to beat a real `cat` the child can otherwise reach, or the negative is vacuous
    hijacked_path = str(hb) + os.pathsep + child_path()
    cmd = "cat '%s'" % path
    t = rollout(R / "tmp", [meta(A), file_change(A)], name="rollout-cx3-hijack.jsonl")

    out = run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash", tool_input={"command": cmd}),
                   R, host="codex")
    if not denied(out):
        code, stdout = real_run("posix", cmd, R, PATH=hijacked_path)
        pytest.fail("admitted %r; the PATH replacement then ran: rc=%r stdout=%r sentinel=%r"
                    % (cmd, code, stdout.strip(), sentinel.read_bytes()))
    assert STAGE in reason(out), "the denial must still name the pending stage"
    assert sentinel.read_bytes() == b"before\n", "a denied command must have no body effect"
    assert blocked(run_gate("stop", codex_hook(A, t, "stop"), R, host="codex")), \
        "the stage is still outstanding after the denial"

    # the reproducer is live: the same spelling, in that shell, is the replacement, not a read
    code, stdout = real_run("posix", cmd, R, PATH=hijacked_path)
    assert code == 0 and "HIJACKED" in stdout, "hijack inert: rc=%r stdout=%r" % (code, stdout)
    assert sentinel.read_bytes() == b"changed", "hijack inert: it never wrote the sentinel"
    assert "real-skill-body-9b2e71" not in stdout, "the replacement did not read the real file"


@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_cx3_flow_the_withdrawn_read_is_denied_but_a_recorded_load_still_releases(tmp_path, shell):
    exe = need_shell(shell)
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    path = plugin_skill(R)
    marker = "cx3-marker-%s-4f1c9a" % shell
    body = Path(path)
    body.parent.mkdir(parents=True, exist_ok=True)
    assert_inside(R, body)
    body.write_text("# %s\n%s\n" % (LEAF, marker), encoding="utf-8")

    # 1. the session already edited and the stage is missing, so Stop blocks
    t1 = rollout(R / "tmp", [meta(A), file_change(A)], name="rollout-cx3-flow-1-%s.jsonl" % shell)
    assert blocked(run_gate("stop", codex_hook(A, t1, "stop"), R, host="codex"))

    # 2. the gate denies the read: there is no admitted pre-stage command any more
    cmd = read_cmd(path, shell)
    out = run_gate("pre", codex_hook(A, t1, "pre", tool_name="Bash",
                                     tool_input={"command": cmd}), R, host="codex")
    assert denied(out) and STAGE in reason(out), \
        "the withdrawn form must be denied; %s: %r" % (exe, cmd)
    assert blocked(run_gate("stop", codex_hook(A, t1, "stop"), R, host="codex")), \
        "and the denial leaves the Stop blocked: this is the deadlock the withdrawal accepts"

    # 3. the credit half is untouched. Whatever produced it -- the operator running under the
    # hatch, or a host-native load once one is qualified -- a real read with a real exit code
    # is still what the ledger credits, and that still releases the Stop.
    code, stdout = real_run(shell, cmd, R)
    assert code == 0 and marker in stdout, "%s did not read the file: rc=%r out=%r" % (exe, code, stdout)
    t2 = rollout(R / "tmp", [meta(A), file_change(A), completion_of(cmd, path, code)],
                 name="rollout-cx3-flow-2-%s.jsonl" % shell)
    f = gate_codex.transcript_facts(t2)
    assert f.edited is True and STAGE in f.invoked, "invoked=%r" % (f.invoked,)
    assert run_gate("stop", codex_hook(A, t2, "stop"), R, host="codex") is None


def test_cx4_the_posix_read_survives_a_neutral_parent_path(tmp_path, monkeypatch):
    """The portability regression Codex's independent CX3 run exposed, kept as a control.

    That run resolved the fallback `C:\\Program Files\\Git\\usr\\bin\\sh.exe` by absolute path
    from a parent PATH with no Git `usr\\bin`. The shell started; its child `cat` did not exist,
    so the credit row died at `rc=127` with empty stdout and the flow only passed once the
    directory was pinned by hand. `child_path` now builds the child PATH from the tools this
    module resolved, so emptying the parent cannot take `cat` away from the shell.

    Emptying the parent PATH is what makes this non-vacuous: without the fix this row is the
    original rc=127.
    """
    need_shell("posix")
    R, _, _ = qual_root(tmp_path)
    path = plugin_skill(R)
    marker = "cx4-neutral-path-7d3a25"
    body = assert_inside(R, Path(path))
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text("# %s\n%s\n" % (LEAF, marker), encoding="utf-8")

    monkeypatch.setenv("PATH", str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"))
    assert shutil.which("cat") is None, "the parent PATH still supplies cat; this proves nothing"

    code, stdout = real_run("posix", read_cmd(path, "posix"), R)
    assert code == 0 and marker in stdout, \
        "%s lost its child under a neutral parent PATH: rc=%r out=%r" % (POSIX_SH, code, stdout)


@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_cx3_flow_a_read_that_really_failed_earns_nothing(tmp_path, shell):
    exe = need_shell(shell)
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    path = plugin_skill(R)  # identity matches the pending stage, but nothing is on disk
    assert not Path(path).exists()
    block = item({"type": "HookPrompt", "id": "m1",
                  "fragments": [{"text": "Loadout gate: stages not run this session: diagnosis"}]})
    cmd = read_cmd(path, shell)
    t1 = rollout(R / "tmp", [meta(A), file_change(A), block], name="rollout-cx3-fail-1-%s.jsonl" % shell)

    # denied at pre like every other command; the failure branch below is about what the ledger
    # does with an outcome, which is reached whenever a read did run by some other route
    assert denied(run_gate("pre", codex_hook(A, t1, "pre", tool_name="Bash",
                                             tool_input={"command": cmd}), R, host="codex"))
    code, _ = real_run(shell, cmd, R)
    assert code != 0, "%s must fail on a missing file, got rc=0" % exe

    t2 = rollout(R / "tmp", [meta(A), file_change(A), block, completion_of(cmd, path, code), block],
                 name="rollout-cx3-fail-2-%s.jsonl" % shell)
    f = gate_codex.transcript_facts(t2)
    assert STAGE not in f.invoked, "a read that really failed earned credit: %r" % (f.invoked,)
    assert f.blocks == 2, "a read that really failed reset the block streak: blocks=%r" % f.blocks
    assert blocked(run_gate("stop", codex_hook(A, t2, "stop"), R, host="codex"))


@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_cx3_flow_a_mixed_read_and_write_is_denied_with_zero_body_effect(tmp_path, shell):
    need_shell(shell)
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    path = plugin_skill(R)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("# %s\n" % LEAF, encoding="utf-8")
    sentinel = assert_inside(R, A / "sentinel.txt")
    sentinel.write_bytes(b"before\n")
    t = rollout(R / "tmp", [meta(A), user("load it and stash a note")],
                name="rollout-cx3-mixed-%s.jsonl" % shell)

    for cmd in ("%s; echo after > '%s'" % (read_cmd(path, shell), sentinel),
                "%s && echo after > '%s'" % (read_cmd(path, shell), sentinel)):
        out = run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                         tool_input={"command": cmd}), R, host="codex")
        assert denied(out) and STAGE in reason(out), "mixed command was admitted: %r" % cmd
        assert sentinel.read_bytes() == b"before\n", "a denied command must have no body effect"
        assert gate_codex.transcript_facts(t).invoked == set()


# ---- CX-3 outcome credit: only an explicitly successful load counts -------------------------
# Q3: "Denied/failed read cannot earn completed-load credit." The recorded rollout
# (tests/fixtures/codex-rollout.jsonl) is the whole supported outcome vocabulary this repair
# relies on: status="completed" with exit_code=0, and status="failed" with exit_code=1.
# Anything else -- absent, null, unknown, or a wrong-typed status, and any present exit evidence
# that contradicts or cannot be read -- does not *prove* a successful load, so it earns nothing.
# This intentionally tightens the accepted contract, which credited every completion record.

OUTCOMES = [
    ("completed and exit 0",       "completed",  0,         True),
    ("completed, exit absent",     "completed",  _MISSING,  True),
    ("failed",                     "failed",     1,         False),
    ("failed, exit absent",        "failed",     _MISSING,  False),
    ("status absent",              _MISSING,     0,         False),
    ("status null",                None,         0,         False),
    ("status unknown verb",        "in_progress", 0,        False),
    ("status wrong type",          1,            0,         False),
    ("completed but exit 1",       "completed",  1,         False),
    ("completed but exit null",    "completed",  None,      False),
    ("completed but exit '0'",     "completed",  "0",       False),
    ("completed but exit garbage", "completed",  "ok",      False),
    ("completed but exit True",    "completed",  True,      False),
]


@pytest.mark.parametrize("label,status,exit_code,credited", OUTCOMES,
                         ids=[o[0].replace(" ", "-") for o in OUTCOMES])
def test_cx3_only_an_explicitly_successful_read_earns_load_credit(tmp_path, label, status, exit_code,
                                                                  credited):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    read, _ = read_of(plugin_skill(R), status=status, exit_code=exit_code)
    t = rollout(R / "tmp", [meta(A), file_change(A), read], name="rollout-cx3-outcome.jsonl")
    f = gate_codex.transcript_facts(t)
    got = STAGE in f.invoked
    assert got is credited, "%s: credited=%r, expected %r (invoked=%r)" % (label, got, credited, f.invoked)
    # the Stop decision is the observable consequence of that credit, recorded separately
    out = run_gate("stop", codex_hook(A, t, "stop"), R, host="codex")
    if credited:
        assert out is None, "%s: a proven load must release the stop" % label
    else:
        assert blocked(out), "%s: an unproven load must keep the stop blocked" % label


def test_cx3_an_unsuccessful_read_does_not_reset_the_consecutive_block_streak(tmp_path):
    """The progress event and the credit are the same fact: a read that did not demonstrably
    succeed is not progress, so it must not zero the runaway counter either."""
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    block = item({"type": "HookPrompt", "id": "m1",
                  "fragments": [{"text": "Loadout gate: stages not run this session: diagnosis"}]})
    failed, _ = read_of(plugin_skill(R), status="failed", exit_code=1)
    ok, _ = read_of(plugin_skill(R))
    t = rollout(R / "tmp", [meta(A), file_change(A), block, block, failed, block],
                name="rollout-cx3-streak-failed.jsonl")
    assert gate_codex.transcript_facts(t).blocks == 3, "a failed read is not progress"
    t = rollout(R / "tmp", [meta(A), file_change(A), block, block, ok, block],
                name="rollout-cx3-streak-ok.jsonl")
    assert gate_codex.transcript_facts(t).blocks == 1, "a successful read resets the streak"


def test_cx3_edit_accounting_stays_outcome_blind(tmp_path):
    """Deliberately asymmetric: credit needs proof of success, an edit does not. A write that
    failed still marks the session edited, because the conservative direction for `edited` is to
    require the stages, and for `invoked` it is to withhold the release."""
    R, A, _ = qual_root(tmp_path)
    for status, exit_code in (("failed", 1), (_MISSING, _MISSING), ("in_progress", 0)):
        t = rollout(R / "tmp", [meta(A), command("Set-Content -LiteralPath 'x.txt' -Value y",
                                                 status=status, exit_code=exit_code)],
                    name="rollout-cx3-edit-blind.jsonl")
        assert gate_codex.transcript_facts(t).edited is True, "status=%r must not excuse a write" % status


# ================================================================ CX-4
# Collisions, standalone roots, and the shapes that must never count.

def test_cx4_the_same_leaf_in_another_plugin_does_not_satisfy_the_qualified_stage(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    read, _ = read_of(plugin_skill(R, plugin="otherpack", version="1.0.0"))
    t = rollout(R / "tmp", [meta(A), file_change(A), read], name="rollout-cx4-collide.jsonl")
    f = gate_codex.transcript_facts(t)
    assert "otherpack:" + LEAF in f.invoked, "the other plugin gets its own qualified identity"
    assert STAGE not in f.invoked, "a different plugin's copy must not credit the intended namespace"
    assert blocked(run_gate("stop", codex_hook(A, t, "stop"), R, host="codex"))


def test_cx4_a_standalone_root_yields_only_the_leaf(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    read, _ = read_of(standalone_skill(R))
    t = rollout(R / "tmp", [meta(A), file_change(A), read], name="rollout-cx4-standalone.jsonl")
    f = gate_codex.transcript_facts(t)
    assert f.invoked == {LEAF}, "no plugin in the path means no qualified identity to record"
    assert blocked(run_gate("stop", codex_hook(A, t, "stop"), R, host="codex"))


def test_cx4_search_bare_mention_and_prose_path_never_credit(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    path = plugin_skill(R)
    cases = (("search", [search_of(path)]),
             ("mention", [user("please $" + LEAF + " and then $" + STAGE)]),
             ("prose", [agent("the skill lives at " + path)]))
    for label, extra in cases:
        t = rollout(R / "tmp", [meta(A), file_change(A)] + extra, name="rollout-cx4-%s.jsonl" % label)
        f = gate_codex.transcript_facts(t)
        assert f.invoked == set(), "%s must not count as a load; invoked=%r" % (label, f.invoked)
        assert blocked(run_gate("stop", codex_hook(A, t, "stop"), R, host="codex"))


def test_cx4_a_leaf_keyed_stage_is_satisfied_by_any_plugin_copy_known_ceiling(tmp_path):
    """Ceiling of retaining the leaf alias: a LOADOUT.md naming the bare leaf accepts any
    plugin's copy of it. A qualified key discriminates the plugin name; a bare leaf does not."""
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, "# Loadout: cx\n\n## Accepted\n- diagnosis: `" + LEAF + "`\n")
    read, _ = read_of(plugin_skill(R, plugin="otherpack", version="1.0.0"))
    t = rollout(R / "tmp", [meta(A), file_change(A), read], name="rollout-cx4-ceiling.jsonl")
    assert run_gate("stop", codex_hook(A, t, "stop"), R, host="codex") is None


# ---- CX-4 the exact width of the qualified key ----------------------------------------------
# `plugin:name` is a *namespace* key, not a physical-origin identity. It discriminates the plugin
# segment and the skill leaf and nothing else: the market segment, the version segment and the
# cache root above them are all discarded. These pin what that does and does not exclude, so no
# reader has to infer collision safety the key does not have.

COLLIDING = [
    ("a different market",  dict(market="other-market")),
    ("a different version", dict(version="9.9.9")),
    ("both",                dict(market="other-market", version="9.9.9")),
]


@pytest.mark.parametrize("label,kw", COLLIDING, ids=[c[0].replace(" ", "-") for c in COLLIDING])
def test_cx4_same_plugin_and_leaf_from_another_market_or_version_collide(tmp_path, label, kw):
    """KNOWN CEILING, disclosed not repaired: the market and version segments are outside the key,
    so an unrelated `<other market>/superpowers/<any version>` copy satisfies a stage that meant
    the audited one. Narrowing the key would change accepted LOADOUT.md line syntax, which is
    outside this repair; physical-origin exclusion is therefore NOT demonstrated."""
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    read, _ = read_of(plugin_skill(R, **kw))
    t = rollout(R / "tmp", [meta(A), file_change(A), read], name="rollout-cx4-collide-key.jsonl")
    assert STAGE in gate_codex.transcript_facts(t).invoked, "%s: key width changed" % label
    assert run_gate("stop", codex_hook(A, t, "stop"), R, host="codex") is None
    # The pre-tool admission that shared this identity helper is withdrawn, so the collision no
    # longer has a second reachable consumer. It is unchanged where it still bites: the ledger,
    # and therefore Stop. Both assertions above are that surface.


def test_cx4_any_plugins_cache_root_yields_the_qualified_key(tmp_path):
    """Same ceiling on the root: the key is anchored on the `plugins/cache/<market>/<plugin>/`
    shape wherever it appears, not on a trusted install root. A project-local tree of that shape
    produces the same qualified key."""
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    local = A / "vendor" / "plugins" / "cache" / "market" / "superpowers" / "6.3.0" / "skills" / LEAF / "SKILL.md"
    assert_inside(R, local.parent)
    read, _ = read_of(str(local).replace("\\", "/"))
    t = rollout(R / "tmp", [meta(A), file_change(A), read], name="rollout-cx4-localroot.jsonl")
    assert STAGE in gate_codex.transcript_facts(t).invoked, "the root is not part of the key"


def test_cx4_a_path_outside_the_cache_shape_yields_only_the_leaf(tmp_path):
    """The complement: without the `plugins/cache/<market>/<plugin>/<version>/` shape there is no
    qualified identity at all, only the leaf -- so it can never satisfy a qualified stage."""
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    for p in (str(A / "skills" / LEAF / "SKILL.md").replace("\\", "/"),
              standalone_skill(R),
              "%s/home/plugins/superpowers/skills/%s/SKILL.md" % (str(R).replace("\\", "/"), LEAF)):
        read, _ = read_of(p)
        t = rollout(R / "tmp", [meta(A), file_change(A), read], name="rollout-cx4-noqual.jsonl")
        f = gate_codex.transcript_facts(t)
        assert f.invoked == {LEAF}, "%s: invoked=%r" % (p, f.invoked)
        assert blocked(run_gate("stop", codex_hook(A, t, "stop"), R, host="codex"))
        assert denied(admit(R, A, read_cmd(p)))


# ================================================================ CX-5
# Attempt versus effect, with real on-disk sentinels.

def satisfied(R, A, extra=(), name="rollout-cx5-ok.jsonl"):
    read, _ = read_of(plugin_skill(R))
    return rollout(R / "tmp", [meta(A), read] + list(extra), name=name)


def test_cx5_a_denied_mutation_leaves_the_sentinel_byte_identical(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    sentinel = assert_inside(R, A / "sentinel.txt")
    sentinel.write_bytes(b"before\n")
    t = rollout(R / "tmp", [meta(A), user("overwrite it")], name="rollout-cx5-denied.jsonl")
    cmd = "echo after > '%s'" % sentinel
    assert denied(run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                             tool_input={"command": cmd}), R, host="codex"))
    assert sentinel.read_bytes() == b"before\n", "a denied call must never reach the filesystem"


def test_cx5_an_allowed_mutation_actually_moves_the_sentinel(tmp_path):
    need_shell("pwsh")  # resolved once, reported when absent, not spelled bare at the call site
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    sentinel = assert_inside(R, A / "sentinel.txt")
    sentinel.write_bytes(b"before\n")
    t = satisfied(R, A)
    cmd = "Set-Content -LiteralPath '%s' -Value after" % sentinel
    assert run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                      tool_input={"command": cmd}), R, host="codex") is None
    # the synthetic child environment, like every other executing fixture here
    code, out = real_run("pwsh", cmd, R)
    assert code == 0, "the allowed command failed: rc=%r out=%r" % (code, out)
    assert sentinel.read_text(encoding="utf-8").strip() == "after", "the allowed body effect is real"


def test_cx5_a_failed_mutation_still_marks_the_session_edited_known_ceiling(tmp_path):
    """Accepted source classifies the command shape, not its outcome: an attempt that failed is
    still `edited`. That errs towards requiring the stages, so it is recorded as a ceiling
    rather than repaired."""
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    t = rollout(R / "tmp", [meta(A), command("Set-Content -LiteralPath 'x.txt' -Value y",
                                             status="failed", exit_code=1)],
                name="rollout-cx5-failed.jsonl")
    assert gate_codex.transcript_facts(t).edited is True


def test_cx5_exec_fallback_distinguishes_apply_patch_from_a_read(tmp_path):
    R, A, _ = qual_root(tmp_path)
    t = rollout(R / "tmp", [meta(A), exec_call('await tools.apply_patch("*** Begin Patch")')],
                name="rollout-cx5-patch.jsonl")
    assert gate_codex.transcript_facts(t).edited is True
    t = rollout(R / "tmp", [meta(A), exec_call('await tools.exec_command({"cmd":"git status --short"})')],
                name="rollout-cx5-read.jsonl")
    assert gate_codex.transcript_facts(t).edited is False


def test_cx5_mixed_operators_and_look_alikes(tmp_path):
    R, A, _ = qual_root(tmp_path)
    edits = ["git status; echo x > out.txt", "cat a.txt && tee b.txt", "cat a.txt | tee b.txt",
             "sudo rm -rf build", "python -c 'open(\"x\",\"w\")'"]
    reads = ["git status --short", "cat LOADOUT.md", "grep -rn mv .", "ls out.txt",
             "echo mvp", "./tee-report.sh", "cat 2>&1", "echo hi > /dev/null"]
    # accepted-source over-classification: a write word inside a quoted argument is read in
    # command position, because `sh -c 'tee x'` has to be. Documented, not repaired.
    false_mutations = ["echo 'tee is a word here'", "git log --grep='rm -rf'"]
    for c in edits:
        t = rollout(R / "tmp", [meta(A), command(c)], name="rollout-cx5-mix.jsonl")
        assert gate_codex.transcript_facts(t).edited is True, "missed write shape: %r" % c
    for c in reads:
        t = rollout(R / "tmp", [meta(A), command(c)], name="rollout-cx5-mix.jsonl")
        assert gate_codex.transcript_facts(t).edited is False, "false mutation: %r" % c
    for c in false_mutations:
        t = rollout(R / "tmp", [meta(A), command(c)], name="rollout-cx5-mix.jsonl")
        assert gate_codex.transcript_facts(t).edited is True, "ceiling changed: %r" % c


def test_cx5_naming_the_surface_is_gated_only_when_the_command_writes(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, ENFORCED)
    t = satisfied(R, A, name="rollout-cx5-surface.jsonl")
    out = run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                     tool_input={"command": "echo x >> gate.py"}), R, host="codex")
    assert denied(out) and "operator-owned" in reason(out)
    assert run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                      tool_input={"command": "cat gate.py"}), R, host="codex") is None


# ================================================================ CX-6
# The prose-only opt-out: scope by host, by project and by exact syntax.

def test_cx6_prose_only_releases_codex(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, PROSE_ONLY)
    t = rollout(R / "tmp", [meta(A), file_change(A)], name="rollout-cx6-prose.jsonl")
    assert run_gate("stop", codex_hook(A, t, "stop"), R, host="codex") is None
    assert run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                      tool_input={"command": "echo hi > a.txt"}), R, host="codex") is None
    # and through host auto-detection, with no --host flag
    t2 = rollout(R / "tmp", [meta(A), file_change(A)], name="rollout-2026-09-06T00-00-00-abc.jsonl")
    assert run_gate("stop", codex_hook(A, t2, "stop"), R) is None


def claude_transcript(d, cwd, name="session.jsonl"):
    lines = [{"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": "do it"}},
             {"type": "assistant", "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "id": "u1", "name": "Write",
                  "input": {"file_path": str(Path(cwd) / "a.py")}}]}}]
    p = Path(d) / name
    p.write_bytes(("\n".join(json.dumps(l) for l in lines) + "\n").encode("utf-8"))
    return p


def test_cx6_prose_only_is_codex_scoped_and_never_releases_claude(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, PROSE_ONLY)
    t = claude_transcript(R / "tmp", A)
    hook = {"session_id": "s1", "transcript_path": str(t), "cwd": str(A), "hook_event_name": "Stop"}
    out = run_gate("stop", hook, R, host="claude-code")
    assert blocked(out) and STAGE in out["reason"], "the opt-out is scoped to Codex only"
    out = run_gate("pre", dict(hook, hook_event_name="PreToolUse", tool_name="Write",
                               tool_input={"file_path": str(A / "a.py")}), R, host="claude-code")
    assert denied(out) and STAGE in reason(out)


def test_cx6_an_enforced_sibling_keeps_its_enforcement(tmp_path):
    R, A, B = qual_root(tmp_path)
    write_loadout(A, PROSE_ONLY)
    write_loadout(B, ENFORCED)
    t = rollout(R / "tmp", [meta(B), file_change(B)], name="rollout-cx6-sibling.jsonl")
    assert blocked(run_gate("stop", codex_hook(B, t, "stop"), R, host="codex"))


def test_cx6_the_nearer_policy_governs_in_both_directions(tmp_path):
    R, A, B = qual_root(tmp_path)
    write_loadout(R / "projects", ENFORCED)          # ancestor
    write_loadout(A, PROSE_ONLY)                     # nearer, wins
    t = rollout(R / "tmp", [meta(A), file_change(A)], name="rollout-cx6-near1.jsonl")
    assert run_gate("stop", codex_hook(A, t, "stop"), R, host="codex") is None
    write_loadout(R / "projects", PROSE_ONLY)        # ancestor
    write_loadout(B, ENFORCED)                       # nearer, wins
    t = rollout(R / "tmp", [meta(B), file_change(B)], name="rollout-cx6-near2.jsonl")
    assert blocked(run_gate("stop", codex_hook(B, t, "stop"), R, host="codex"))


def test_cx6_only_the_exact_header_opts_out(tmp_path):
    R, A, _ = qual_root(tmp_path)
    t = rollout(R / "tmp", [meta(A), file_change(A)], name="rollout-cx6-syntax.jsonl")
    accept = ["Enforcement: prose only", "enforcement:  PROSE ONLY", "Enforcement:prose only   ",
              "Enforcement:\tprose only\t"]
    reject = ["  Enforcement: prose only", "Enforcement: prose only please", "# Enforcement: prose only",
              "Enforcement: prose  only", "Enforcement: codex gate registered", "Enforcement: prose",
              "Enforcement: prose onlyx"]
    for header in accept:
        write_loadout(A, "# Loadout: cx\n" + header + "\n\n## Accepted\n- diagnosis: `" + STAGE + "`\n")
        assert run_gate("stop", codex_hook(A, t, "stop"), R, host="codex") is None, "must opt out: %r" % header
    for header in reject:
        write_loadout(A, "# Loadout: cx\n" + header + "\n\n## Accepted\n- diagnosis: `" + STAGE + "`\n")
        assert blocked(run_gate("stop", codex_hook(A, t, "stop"), R, host="codex")), "must enforce: %r" % header


def test_cx6_prose_only_does_not_release_the_operator_owned_surface(tmp_path):
    """Deliberate deviation from a literal "before binding-stage processing" placement: the
    opt-out releases the stage requirement, not the enforcement surface. A LOADOUT.md able to
    switch off protection of gate.py -- and of itself -- would be the agent-side override the
    accepted contract says does not exist (LOADOUT_ENFORCE=0 is the only full hatch)."""
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, PROSE_ONLY)
    t = rollout(R / "tmp", [meta(A), file_change(A)], name="rollout-cx6-surface.jsonl")
    patch = "*** Begin Patch\n*** Update File: gate.py\n+x\n*** End Patch"
    out = run_gate("pre", codex_hook(A, t, "pre", tool_name="apply_patch",
                                     tool_input={"command": patch}), R, host="codex")
    assert denied(out) and "operator-owned" in reason(out)
    out = run_gate("pre", codex_hook(A, t, "pre", tool_name="Bash",
                                     tool_input={"command": "echo x >> LOADOUT.md"}), R, host="codex")
    assert denied(out) and "operator-owned" in reason(out)


def test_cx6_prose_only_holds_on_the_next_turn_and_a_resumed_session(tmp_path):
    R, A, _ = qual_root(tmp_path)
    write_loadout(A, PROSE_ONLY)
    turn = [meta(A), file_change(A)]
    t = rollout(R / "tmp", turn + [user("and again"), file_change(A, "b.py")], name="rollout-cx6-next.jsonl")
    assert run_gate("stop", codex_hook(A, t, "stop"), R, host="codex") is None

    # resumed: no transcript_path on the Stop payload, so the rollout is found by session_id
    home = assert_inside(R, R / "home" / "codex")
    sessions = home / "sessions" / "2026" / "09" / "06"
    sessions.mkdir(parents=True)
    sid = "01a074bd-7420-7d20-aa0b-a83fbd86db0e"
    rollout(sessions, turn, name="rollout-2026-09-06T11-00-00-%s.jsonl" % sid)
    hook = {"session_id": sid, "cwd": str(A), "hook_event_name": "Stop", "stop_hook_active": False}
    r = subprocess.run([sys.executable, str(GATE), "stop", "--host", "codex"],
                       input=json.dumps(hook), capture_output=True, encoding="utf-8",
                       env=child_env(R, CODEX_HOME=str(home)))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", "resumed session must stay released, got %r" % r.stdout
