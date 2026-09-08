#!/usr/bin/env python3
"""loadout skill audit — assess unfamiliar installed skills and persist the result locally.

`scan.py` answers "what is installed" and `check_notes.py` answers "is the notes table
self-consistent". Neither answers "have I ever understood this skill, and is that understanding
still about the bytes on disk". That is what this does, for the explicit audit only: discover
installed skills, bind each to a resolved local path and a digest of its actual SKILL.md bytes,
carry an assessment of it, and hand the recommendation step the ones that are still current.

    python3 scripts/skill_audit.py [--roots A,B] [--store P] [--assessments F] [--json]
    python3 scripts/skill_audit.py --work-list   [--roots A,B] [--store P]
    python3 scripts/skill_audit.py --recommendable [--roots A,B] [--store P] [--json]

Options:
  --roots A,B      audit exactly these skills directories instead of the installed harnesses.
                   Each must exist AND be a directory; missing, not-a-directory and inaccessible
                   are three different mistakes and are reported apart, never as an empty audit.
                   The same boundary applies to --recommendable, so a store is never consumed
                   against a wider inventory than it was audited against.
  --store P        the record file (default: references/skill-assessments.json beside this skill).
  --assessments F  assessor output to ingest this run (see "assessor input" below).
                   Inapplicable with --recommendable, which only consumes: that combination is
                   refused with exit 2 rather than silently ignored.
  --project D      the project directory whose .claude settings and project skill directories
                   take part in this audit (default: the current directory). It selects both the
                   settings stack that decides enabled plugins and individually disabled skills,
                   and the project-relative skill dirs that enter the inventory. Ignored under
                   --roots, which is a bounded inventory that the host's enable map does not
                   describe.
  --json           emit machine-readable records instead of the human summary.
  --work-list      emit only the entries that still need an assessment, as JSON.

--json and --work-list keep stdout to their documented payload; refused submissions are reported
on stderr, so a rejection is never invisible just because the mode is machine-readable.

Exit codes:
  0  audit completed and the store is current
  1  no skills found in any root, so nothing was published
  2  bad arguments, contradictory or inapplicable options, an unusable store, an unusable
     assessments file, or a root that could not be listed. In every exit-2 case the previous
     store is left exactly as it was, byte for byte.

A successful non-empty audit replaces the ENTIRE store with the state it just observed. It is not
a merge and not a log: an identity that is no longer installed in the audited roots is gone from
the file afterwards. So a narrower audit must not be published over a wider one's store - give
each audit scope (each --roots set, each --project) its own --store path, or the narrower run
deletes every record the wider one held.

Publishing while an identity is ambiguous or disabled DISCARDS the judgment for it. Recommendation
is withdrawn immediately, which is the point, but the assessment is not held in reserve: there is
no suspended-judgment state, so recovery after the ambiguity or the disable is resolved is a fresh
assessment, not a restore.

Assessor input is built from `--work-list` output alone: each entry already carries the `name`,
`source`, `skill_md` and `digest` an entry must echo back. Nothing requires reading the store.

The assessment itself is a judgment, so this file does not make it. An agent reads the bodies as
data and writes its judgments to a JSON file; `--assessments` feeds them back through deterministic
validation. Nothing here spawns a model, opens a socket, or installs anything - the scanner does
not become a model service. Skill bodies are never executed and their instructions are never
followed; they are descriptive input.

Facts and judgment stay separate in the record. `facts` is what was observed on disk. `assessment`
is what an agent concluded. A body's declared dependencies are recorded as declared, never as
observed availability - nothing here checks whether a dependency is present. `declared_dependencies`
absent means the body declared nothing, which is unknown; an explicit empty list means the body
declared *none*, which is a different and knowable fact.

The digest is change evidence only. It proves these are the bytes that were assessed. It does not
authenticate a publisher and it does not make a skill safe.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan  # noqa: E402  - sibling script, imported for root/plugin/description facts

SCHEMA = "loadout-skill-assessments"
VERSION = 1
ASSESSMENT_VERSION = 1
STORE = "references/skill-assessments.json"
# A SKILL.md is prose. Something larger than this is not a body to assess, and reading it to find
# that out is the cost this avoids; it becomes an explicit unreadable record, not a hang.
MAX_BODY = 4 << 20

# Closed set. Anything a record cannot explain in these terms is a bug here, not a new state.
REASONS = {"ambiguous-identity", "unreadable", "invalid-utf8", "malformed-metadata",
           "malformed-assessment", "unassessed", "stale-digest", "stale-input",
           "unsupported-metadata", "not-invocable"}
# Reasons no assessment may attach to. Not one condition but two: `unreadable`, `invalid-utf8`,
# `malformed-metadata` and `unsupported-metadata` have no facts to judge, while `ambiguous-identity`
# and `not-invocable` may have parsed perfectly and are excluded for what they mean, not for what
# they lack. Reading this set as "the body was never read" is wrong for both of those.
UNASSESSABLE = {"ambiguous-identity", "unreadable", "invalid-utf8", "malformed-metadata",
                "unsupported-metadata", "not-invocable"}

TEXT_FIELDS = ("purpose", "basis")
LIST_FIELDS = ("requirements", "overlaps", "conflicts", "uncertain")
RECORD_KEYS = {"name", "host", "source", "skill_md", "digest", "inputs", "status", "reason",
               "facts", "assessment", "invocable"}
SUPPLIED_KEYS = {"name", "source", "digest", "assessment", "inputs"}
STORE_KEYS = {"schema", "version", "records"}
# Reasons that carry no facts. Only `unreadable` captured no body: the other three DID capture
# the body and failed to parse facts out of it, so they legitimately have its byte count, digest
# and canonical body input row. Absence of facts here is "nothing was parsed", not "nothing was
# captured", and a diagnostic that says otherwise sends a reader looking for a permission error
# that never happened. `unreadable` is the narrower statement it looks like: the attempt returned
# no body bytes to this record, which covers a refused open and an oversized file alike, and it is
# not a claim that no read was attempted. Not the same set as UNASSESSABLE, and the difference is
# NOT that everything outside this set parsed. `ambiguous-identity` is stamped on by
# `resolve_identity` after `inspect` has already classified the body, so what it overwrites may be
# an unreadable record that captured nothing, or a parse failure that captured the body and got no
# facts out of it. `_validate_facts` admits that reason with facts or without, and `_withdraw`
# empties `facts`, `digest` and `inputs` only when a caller asks for it; the ambiguity paths that
# do not ask keep whatever payload the record already had. So neither an ambiguous reason nor a
# nonempty digest is evidence that a body parsed. Every OTHER remaining state must carry the facts
# read from its body - `not-invocable` included, which is excluded from an assessment for what it
# means and not for what it lacks.
NO_FACTS = {"unreadable", "invalid-utf8", "malformed-metadata", "unsupported-metadata"}
# The closed fact vocabulary `_facts` can emit, with the type each later reader indexes it as.
FACT_TYPES = {"bytes": int, "declared_description": str, "frontmatter_name": str,
              "declared_dependencies": list}

# The supported `skillOverrides` value table. Only "off" disables.
#
#   absent          enabled - there is no override for this skill
#   "on"            enabled, explicitly
#   "off"           disabled: not invocable, and not recommendable
#   falsy scalar    NOT disabling. false, null and "" are what the scanner already renders as
#                   no status at all (scan.py: `if st and st != "on"`), so reading them as off
#                   would give a current non-disabling state a new meaning it never had.
#   anything else   unsupported: refused at the strict boundary rather than guessed at. A typo
#                   like "disabled", or a `true`, is not a disable this program may invent.
OVERRIDE_ON, OVERRIDE_OFF = "on", "off"
# Roots whose skills Claude Code's `skillOverrides` actually governs: Claude's own global skills
# dir and the project's Claude-family skill dirs. Not the shared pool, not another harness's
# root, not a dynamic root, and not a plugin (which carries its plugin's enabled state).
CLAUDE_OVERRIDE_HOSTS = {"claude-code", "project:claude-code"}


class DiscoveryError(Exception):
    """A configured root could not be enumerated. Not the same as a root with no skills in it:
    publishing an audit that silently lost a root would delete records that are still installed."""


def digest(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def quoted(s):
    """Every skill-derived string leaves this program JSON-quoted and escaped. A body that opens
    with a table delimiter, a shell metacharacter or an imperative therefore prints as data. This
    is a serialization boundary, not a claim that the body is semantically safe to read."""
    return json.dumps(s, ensure_ascii=True)


def _is_digest(v):
    return isinstance(v, str) and v.startswith("sha256:") and len(v) == 71 \
        and all(c in "0123456789abcdef" for c in v[7:])


def _validate_facts(facts, reason):
    """None when `facts` is the shape this reason is allowed to have, else why it is not.

    The vocabulary is closed and the types are the ones a consumer indexes without checking, so a
    hand-edited `"bytes": "12"` cannot reach a comparison and a `True` cannot pass for a count -
    bool is an int in Python, which is exactly how a byte count of True gets this far."""
    if reason in NO_FACTS:
        return f"facts must be empty when no facts were parsed ({reason})" if facts else None
    if not facts:
        # `ambiguous-identity` is stamped on by resolve_identity *after* inspect has already
        # classified the body, so it overwrites whatever reason was there - including an
        # unreadable one. Such a record legitimately has no facts, and it is the only reason
        # that can go either way. Requiring facts here would refuse to publish a real state.
        if reason == "ambiguous-identity":
            return None
        return f"a {reason or 'assessed'} record needs the facts read from its body"
    unknown = set(facts) - set(FACT_TYPES)
    if unknown:
        return f"unknown fact(s) {sorted(unknown)}"
    for k in ("bytes", "declared_description"):
        if k not in facts:
            return f"facts must carry {k!r}"
    for k, v in facts.items():
        if k == "bytes":
            # bool first: isinstance(True, int) is True, and a byte count of True is not a count.
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                return "facts.bytes must be a non-negative integer"
        elif not isinstance(v, FACT_TYPES[k]):
            return f"facts.{k} must be {FACT_TYPES[k].__name__}"
    if not facts["declared_description"].strip():
        return "facts.declared_description must be non-empty"
    deps = facts.get("declared_dependencies")
    if deps is not None and any(not isinstance(d, str) or not d.strip() for d in deps):
        return "facts.declared_dependencies must be non-empty strings"
    return None


def _validate_provenance(r):
    """None when the record's inputs carry exactly one canonical body row for its digest.

    An assessment is a claim about specific bytes. The row naming `skill_md` at the record's own
    digest is what ties the two together; without it the judgment is attached to nothing, and with
    a different digest it is attached to bytes nobody read. `skill_md` must be the body inside
    `source`, so a record cannot be pointed at another skill's file while keeping its own name.

    Deliberate limit: this cannot detect the removal of a well-shaped *supporting* row. The
    shortened record is still entirely self-consistent. Do not read a pass here as that guarantee."""
    if r["skill_md"] != str(Path(r["source"]) / "SKILL.md"):
        return f"'skill_md' must be {r['source']}/SKILL.md"
    body = [i for i in r["inputs"] if i["path"] == r["skill_md"]]
    if r["digest"] == "":
        # An empty digest is "no bytes are this record's own": either none were captured for it,
        # or none of the observations of it was selected to govern. Provenance rows describe bytes
        # that do govern, so neither case may carry one - and the message may not assert which
        # case this is, nor how many readings were attempted or succeeded to reach it.
        return "a record with no digest of its own cannot carry inputs" if r["inputs"] else None
    if len(body) != 1:
        return f"expected exactly one body provenance row for {r['skill_md']}, found {len(body)}"
    if body[0]["digest"] != r["digest"]:
        return "the body provenance row does not carry this record's digest"
    return None


# ---------------------------------------------------------------- one record validator

def validate_record(r):
    """None when the record is complete and self-consistent, else why it is not.

    Used at load, at publication and at consumption, so a record that would be refused on the way
    in cannot be produced on the way out, and a hand-edited store cannot reach a `r["..."]` lookup
    and raise. Every field a later reader indexes is checked here."""
    if not isinstance(r, dict):
        return "record is not an object"
    extra = set(r) - RECORD_KEYS
    if extra:
        return f"unknown field(s) {sorted(extra)}"
    for k in ("name", "host", "source", "skill_md"):
        if not isinstance(r.get(k), str) or not r.get(k):
            return f"{k!r} must be a non-empty string"
    if "invocable" in r and not isinstance(r["invocable"], bool):
        return "'invocable' must be a boolean"
    if not isinstance(r.get("facts"), dict):
        return "'facts' must be an object"
    status, reason = r.get("status"), r.get("reason")
    if status not in ("assessed", "unresolved"):
        return f"'status' must be 'assessed' or 'unresolved', got {status!r}"
    if not isinstance(reason, str):
        return "'reason' must be a string"
    if status == "assessed" and reason != "":
        return "an assessed record must carry an empty reason"
    if status == "unresolved" and reason not in REASONS:
        return f"'reason' must be one of {sorted(REASONS)}, got {reason!r}"
    dg = r.get("digest")
    if dg != "" and not _is_digest(dg):
        return ("'digest' must be a sha256:<64 hex> string, or empty when no bytes are captured "
                "as this record's own")
    inputs = r.get("inputs")
    if not isinstance(inputs, list):
        return "'inputs' must be a list"
    seen = set()
    for i in inputs:
        if not isinstance(i, dict) or set(i) != {"path", "digest"}:
            return "each input must be exactly {path, digest}"
        if not isinstance(i["path"], str) or not i["path"] or not _is_digest(i["digest"]):
            return "each input needs a non-empty path and a sha256 digest"
        if i["path"] in seen:
            return f"duplicate input path {i['path']!r}"
        seen.add(i["path"])
    bad = _validate_provenance(r)
    if bad:
        return bad
    bad = _validate_facts(r["facts"], "" if status == "assessed" else reason)
    if bad:
        return bad
    a = r.get("assessment")
    if status == "assessed":
        # Persisted state carries the version explicitly. `validate_assessment` defaults it, which
        # is right for an assessor submission written by hand, and wrong here: a stored record with
        # no version is indistinguishable from one written by a future schema that dropped it.
        if not isinstance(a, dict) or a.get("assessment_version") != ASSESSMENT_VERSION:
            return f"a persisted assessment must declare assessment_version {ASSESSMENT_VERSION}"
        if validate_assessment(a) is None:
            return "an assessed record needs a complete, valid assessment"
        if dg == "":
            return "an assessed record needs the digest of the bytes that were assessed"
    elif a is not None:
        return "an unresolved record must not carry an assessment"
    return None


def validate_records(recs, where):
    """Raise ValueError naming the first bad record, or a duplicate identity. Identity is
    (name, source): two records for one identity make every later read ambiguous."""
    if not isinstance(recs, list):
        raise ValueError(f"{where}: records must be a list")
    seen = set()
    for i, r in enumerate(recs):
        bad = validate_record(r)
        if bad:
            name = r.get("name") if isinstance(r, dict) else None
            raise ValueError(f"{where}: record {i}"
                             + (f" ({name!r})" if isinstance(name, str) else "") + f": {bad}")
        key = (r["name"], r["source"])
        if key in seen:
            raise ValueError(f"{where}: duplicate record for {r['name']!r} at {r['source']!r}")
        seen.add(key)
    return recs


# ---------------------------------------------------------------- discovery (facts)

def _role(p, what):
    """scan.dir_role_strict, with an inaccessible path reported as the discovery failure it is
    rather than escaping as a bare OSError out of the middle of an enumeration."""
    try:
        return scan.dir_role_strict(p, what)
    except OSError as e:
        raise DiscoveryError(str(e))


def _anchor_role(p, what):
    """scan.anchor_role_strict, with the same DiscoveryError wrapping as `_role`."""
    try:
        return scan.anchor_role_strict(p, what)
    except OSError as e:
        raise DiscoveryError(str(e))


def _require_dir_or_absent(p, what):
    """A CONFIGURED anchor may be a directory or genuinely absent. Anything else is a structural
    mistake about where state lives, and publishing an inventory without it deletes the records
    it held - silently, at exit 0, because every read underneath answers "not there".

    Absence is judged on the ANCESTRY, not on this path alone: an anchor named underneath a
    regular file reports the same FileNotFoundError/winerror 3 a missing directory does, so
    stat-ing only the anchor filed the structural mistake as ordinary absence and published
    around it.

    Only anchors are checked this way: the host roots, the shared pool, the project directory and
    a plugin's installPath are all paths someone named. The convention directories under them are
    not, so a project that merely holds a file called `.agents` is still ordinary absence."""
    if _anchor_role(p, what) == scan.ROOT_NOT_DIR:
        raise DiscoveryError(f"{what} is not a directory: {p}")


def root_source(d):
    """The resolved source string for root directory `d`, in the same representation a record's
    own source is stored in.

    `inspect` stores `str(srcdir.resolve())` and `resolve_identity` keys identity on
    (name, that string). This reducer keyed on the LEXICAL spelling instead, so two spellings of
    one directory were two roots here and one identity there, and the two boundaries disagreed in
    the fail-open direction both times. Those spellings are ordinary, not exotic: `--project .`
    makes a project root RELATIVE while every other root is absolute, and a `..` component
    survives `Path` construction where a single `.` does not. Only `resolve()` collapses either.
    Their enable states were never conjoined here, and `resolve_identity`
    then kept the FIRST of the colliding pair and dropped the second, so a disable carried by the
    later spelling was discarded rather than ANDed. By the same split an unqualified root spelled
    differently from a disabled plugin's installPath never entered `qualified`, so it survived as
    a bare invocable name over a plugin the host had turned off - exactly the outcome the
    qualified-over-unqualified rule exists to prevent.

    Resolution and its error policy are `inspect`'s, not a second policy invented here: a source
    that cannot be resolved falls back to its own spelling, exactly as the record does, and the
    representation is the native one `Path` produces in both branches - nothing rewrites it
    afterwards.

    `scan.norm` used to run over the result, and it is lossy in two ways this key cannot afford. It
    rewrites a literal backslash to a separator, so where a backslash is an ordinary filename
    character two genuinely distinct directories reduce to one string and are merged as one root
    before anything can call them ambiguous. It also strips trailing separators, so a filesystem
    root stops naming a directory at all - it becomes a bare drive prefix, or the empty string.
    Neither loss buys anything after `resolve()`, which already returns one separator style; and in
    the OSError branch the separator-insensitivity it provided was exactly the collapse, one
    unresolved spelling silently standing in for another that was never resolved either. What
    `resolve()` does about CASE is whatever the platform does with it - nothing here decides that,
    and no case rule is assumed.

    What this string is NOT is the source the surviving record carries. `inspect` resolves the
    CHILD directory, at a different path and a later moment; this resolves the ROOT. They are two
    observations and they can disagree - most plainly when two roots that are correctly distinct
    hold children that resolve together. So this pass is not a proof about final records. It buys
    one thing: not inspecting a single directory twice under a single identity. Reconciling what
    was actually observed is `resolve_identity`'s job, over the child sources it has."""
    try:
        return str(Path(d).resolve())
    except OSError:
        return str(Path(d))


def resolve_root_identities(entries):
    """One entry per LOGICAL root identity, from [(host, dir, invocable)] that may repeat a path.

    Two entries for one directory under one identity would inspect the same skill twice and then
    collide as a duplicate at publication, so those still collapse - and eligibility is their
    conjunction, so neither entry's disable can be discarded by the other's silence.

    A root's identity is never its bytes. Two installed plugins may legitimately share one
    installPath, and each still contributes a distinct <plugin>:<skill> name that the scanner
    renders, with its OWN enabled state. Keying those by path alone kept only the first and ANDed
    the second's state into it: the second plugin's whole identity vanished from the inventory,
    and a disable on either silently withdrew the other's skills. Sharing bytes is not sharing
    identity, and two distinct valid names are not an ambiguity.

    An unqualified root carries no identity beyond its path, so it loses outright to any plugin
    at the same path - that is what stops a DISABLED plugin's skills from reappearing under a
    bare, invocable name - and its own eligibility is folded into every plugin it lost to.

    A plugin's identity is therefore its name AND the source it was installed from - both, not
    either. Keying by name alone collapsed one name installed from two different directories into
    a single record and ANDed their states together, which silently PICKED one set of bytes to
    represent both and let a disable on one withdraw the other. Those two are not one root; they
    are the same name claimed by two sources, which is exactly the ambiguity resolve_identity
    already refuses to resolve. Keeping both here is what lets it see them.

    "Source" is `root_source`, which is the resolved ROOT and not the string the record ends up
    carrying - `inspect` resolves the child. This pass and `resolve_identity` therefore CAN
    disagree, and the one that settles identity is `resolve_identity`, because it is the first
    place the child sources exist. Roots kept apart here on a real difference may still hold
    children that converge there; that is expected, not a failure of this reducer."""
    index, unique, srcs = {}, [], []
    for host, d, inv in entries:
        # a plugin key is the plugin AND its source; everything else is keyed by the directory
        src = root_source(d)
        key = ("plugin", host, src) if host.startswith("plugin:") else ("path", src)
        if key in index:
            kept = unique[index[key]]
            kept[2] = kept[2] and inv
            continue
        index[key] = len(unique)
        unique.append([host, d, inv])
        srcs.append(src)
    qualified = {s for e, s in zip(unique, srcs) if e[0].startswith("plugin:")}
    for e, s in zip(unique, srcs):
        if e[0].startswith("plugin:") or s not in qualified:
            continue
        for q, qs in zip(unique, srcs):
            if q[0].startswith("plugin:") and qs == s:
                q[2] = q[2] and e[2]
    return [tuple(e) for e, s in zip(unique, srcs)
            if e[0].startswith("plugin:") or s not in qualified]


def skill_roots(roots=None, project=None):
    """[(host, dir, invocable)] skills directories.

    An explicit --roots wins outright, so a test or a bounded audit runs against synthetic homes
    and never touches the real inventory. Explicit roots must exist: a typo that silently audits
    nothing would publish an empty store over a good one. Default harness roots are a different
    case - most of them are legitimately absent on any given machine - so absence there is normal.

    Absent is the only normal non-directory answer, though. A configured location that EXISTS and
    is not a directory is a structural mistake about where state lives, and every read underneath
    it answers "not there": audited around, it publishes an inventory with that whole root missing
    and deletes the records it held, at exit 0. Each anchor someone named is therefore checked for
    its role. The convention directories under them are not, so a project that merely holds a file
    called `.agents` is still ordinary absence.

    Plugin-provided skills are included through the scanner's own manifest reading, so a skill
    that `scan.py` already lists as `plugin:name` is assessable by the same name here. So are the
    dynamic roots and project skill directories the scanner already shows: the contract is that
    every unfamiliar *installed* skill enters the workflow, and an inventory that stops at the
    static host table leaves the ones a user is most likely to be unfamiliar with outside it."""
    if roots:
        out = []
        for r in roots:
            p = Path(r).expanduser()
            # `Path.is_dir()` answered False for all three of missing, not-a-directory and
            # inaccessible, so every one of them was reported as the middle case and sent the
            # operator to fix the wrong thing.
            role = _anchor_role(p, "configured root")
            if role == scan.ROOT_ABSENT:
                raise DiscoveryError(f"configured root does not exist: {p}")
            if role == scan.ROOT_NOT_DIR:
                raise DiscoveryError(f"configured root is not a directory: {p}")
            out.append(("explicit", p, True))
        return out
    out = []
    proj = Path(project or ".")
    _require_dir_or_absent(proj, "project directory")
    for host, (root, subs) in scan.HOSTS.items():
        if subs.get("skills"):
            hostroot = Path(root).expanduser()
            _require_dir_or_absent(hostroot, f"{host} host root")
            out.append((host, hostroot / subs["skills"], True))
    shared = Path(scan.SHARED_ROOT).expanduser()
    _require_dir_or_absent(shared, f"{scan.SHARED} root")
    out.append((scan.SHARED, shared / "skills", True))
    out += [("discovered", Path(p).expanduser(), True) for p in scan.EXTRA_SKILL_ROOTS]
    # Keep the PROJECT_ASSETS family tag in the label. Flattening every project dir to "project"
    # makes .claude/skills and .codex/skills indistinguishable downstream, and the per-skill
    # Claude enable map then has no way to tell the root it governs from one it does not.
    out += [(f"project:{fam}", proj / sub, True) for fam, sub in scan.PROJECT_ASSETS
            if sub.endswith("/skills")]
    # The same lossless key the scanner compares against, so a root this audit already owns
    # excludes exactly itself. `scan.norm` here rewrote a literal backslash into a separator, and
    # a known root spelled either way then excluded a genuinely distinct dynamic one - which this
    # audit reads as those skills having been uninstalled. Membership stays deliberately
    # different from the scanner's: EXTRA roots are explicitly owned here, discovered there.
    known = {scan.discovery_key(d.parent) for _h, d, _i in out}
    try:
        for _label, found in sorted(scan.discover_roots(known, strict=True).items()):
            out.append(("discovered", Path(found["root"]) / "skills", True))
    except (OSError, ValueError) as e:
        raise DiscoveryError(f"cannot enumerate dynamic skill roots: {e}")
    out += plugin_roots(project)
    return resolve_root_identities(out)


def plugin_roots(project=None):
    """[(host, dir, invocable)] for each installed Claude Code plugin's skills dir.

    A disabled plugin is still listed - the audit can describe it - but it is marked not
    invocable, and the consumer never recommends something the host cannot run.

    Read strictly. A manifest or settings file that exists but does not parse is a failure here,
    not an empty plugin list: the tolerant read publishes "no plugins installed" and "every
    installed plugin is enabled" from the same unreadable file."""
    rootp = Path(scan._root("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    _require_dir_or_absent(rootp, "claude-code host root")
    proj = Path(project or ".")
    enabled = {}
    try:
        for _label, data in scan.settings_stack(rootp, proj, strict=True):
            enabled.update(data.get("enabledPlugins") or {})
        installed = scan.installed_plugins(rootp, enabled, strict=True)
    except (OSError, ValueError) as e:
        raise DiscoveryError(f"cannot resolve installed plugins: {e}")
    out = []
    for name, ip, on, _version in installed:
        if ip:
            p = Path(ip)
            # An installPath is a configured anchor like any other: present-but-not-a-directory
            # read as absence withdraws every skill the plugin provides without saying so.
            _require_dir_or_absent(p, f"plugin {name!r} installPath")
            out.append((f"plugin:{name}", p / "skills", on))
    return out


def _override_state(name, st, label):
    """OVERRIDE_ON/OVERRIDE_OFF for one raw `skillOverrides` value, or ValueError.

    Only "off" disables. A falsy SCALAR is accepted and is not disabling: false, null and "" are
    what the scanner already renders as no status at all, so reading them as off would give a
    state the host already has a meaning it never had, and would withdraw a working skill.

    Every other value is refused here rather than guessed at. A typo like "disabled", a bare
    `true`, or a container is not a vocabulary this program may extend on the host's behalf: one
    guess silently withdraws a working skill, the other keeps a disabled one recommendable, and
    neither is visible in the published record afterwards."""
    if st == OVERRIDE_OFF:
        return OVERRIDE_OFF
    # `not st` must not swallow [] / {}: those are wrong-type, not a falsy scalar the host writes.
    if st == OVERRIDE_ON or (not st and not isinstance(st, (list, dict))):
        return OVERRIDE_ON
    raise ValueError(f"settings skillOverrides[{name!r}] must be {OVERRIDE_ON!r} or "
                     f"{OVERRIDE_OFF!r}, got {st!r}: {label}")


def skill_overrides(project=None):
    """Merged Claude Code `skillOverrides`, in settings-stack precedence order.

    Plugin enabled state does not cover an individually disabled skill: the plugin is on, the
    host still will not invoke this one, and recommending it sends an agent to something that
    cannot run. Read through the same strict stack as the plugin state, for the same reason.

    Values are normalised to the supported vocabulary HERE, once, so every consumer downstream
    compares against exactly two strings and no caller has to re-derive what a `false` means."""
    rootp = Path(scan._root("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    try:
        stack = scan.settings_stack(rootp, Path(project or "."), strict=True)
    except (OSError, ValueError) as e:
        raise DiscoveryError(f"cannot resolve skill overrides: {e}")
    out = {}
    try:
        for label, data in stack:
            for name, st in (data.get("skillOverrides") or {}).items():
                out[name] = _override_state(name, st, label)
    except ValueError as e:
        raise DiscoveryError(f"cannot resolve skill overrides: {e}")
    return out


def candidates(roots, overrides=None):
    """[(name, dir, host, invocable)] for everything scan.py would list as a skill.

    Entries with no readable SKILL.md are kept deliberately: the audit reports them as unresolved
    rather than dropping them, because a listed skill the audit silently ignores is exactly the
    blind spot this exists to close.

    A root that cannot be enumerated raises instead of contributing nothing. Silently treating an
    unreadable root as empty is what turns a transient permission error into the deletion of every
    record it held. The same applies one level down, inside a bundle.

    `overrides` is the host's per-skill enable map. A skill it disables stays in the audit and
    becomes explicitly not invocable, so the state is visible rather than absent.

    It applies to ordinary skills only, which is the domain the scanner projects it over: plugin
    skills carry their plugin's enabled state instead. Extending it across both would invent an
    override-by-plugin precedence the host does not define - and an ordinary "on" entry would
    then be able to resurrect a skill inside a disabled plugin.

    "Ordinary" is also bounded by HOST, not just by not-being-a-plugin. `skillOverrides` is Claude
    Code settings, and the scanner projects it over Claude Code's own skills alone. Applying it to
    every root reaches into ~/.codex/skills, the shared ~/.agents pool, a project's .codex/skills
    and every dynamic root, where Claude's settings have no authority - a skill named `helper` in
    another harness would be withdrawn by a Claude disable it is not governed by."""
    out = []
    # Only "off" disables. Absent is enabled, and a falsy value is what the scanner already
    # renders as not-disabling; reading either as off would invent a disable nobody wrote.
    # `skill_overrides` has already refused any value outside that vocabulary.
    off = {n for n, st in (overrides or {}).items() if st == OVERRIDE_OFF}
    for host, d, invocable in roots:
        governed = host in CLAUDE_OVERRIDE_HOSTS
        # Absent is normal - most harness roots are legitimately missing on any machine - but a
        # skills root that EXISTS and is not a directory is not absent. `is_dir_strict` answers
        # False for both, and reading that False as absence published an inventory without the
        # root and deleted the records it held.
        role = _role(d, "root")
        if role == scan.ROOT_ABSENT:
            continue
        if role == scan.ROOT_NOT_DIR:
            raise DiscoveryError(f"skills root is not a directory: {d}")
        try:
            entries = sorted(d.iterdir())
        except OSError as e:
            raise DiscoveryError(f"cannot list root {d}: {e}")
        for p in entries:
            if p.name.startswith(".") or p.name in scan.INFRA_NAMES:
                continue
            try:
                inner = scan.bundle_skills(p, strict=True)
                is_dir = scan.is_dir_strict(p, "entry")
            except OSError as e:
                raise DiscoveryError(str(e))
            # host is "plugin:<plugin>" for a plugin root; scan.py names those skills
            # "<plugin>:<skill>", so reuse that exact name rather than inventing another.
            prefix = host.split("plugin:", 1)[1] + ":" if host.startswith("plugin:") else ""
            if inner is not None:
                out += [(prefix + n, c, host,
                         invocable and not (governed and prefix + n in off))
                        for n, c in inner]
            elif is_dir:
                name = prefix + p.name
                out.append((name, p, host, invocable and not (governed and name in off)))
    return out


def _dependencies(fm):
    """(value, supported) for the frontmatter `dependencies:` declaration.

    Returns (None, True) when nothing is declared - unknown, not none. An explicit `[]` returns
    ([], True): the body declared none, which is a fact worth keeping distinct from silence.
    A form this bounded grammar does not read returns (None, False) so the record says so
    explicitly rather than losing the declaration and reporting it as unknown. A bare
    `dependencies:` with nothing under it is such a form: in YAML it is null, not an empty
    sequence, and reporting it as declared-none states a fact the body never declared."""
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("dependencies:"):
            continue
        rest = line[len("dependencies:"):].strip()
        if rest.startswith("[") and rest.endswith("]"):
            body = rest[1:-1].strip()
            if not body:
                return [], True                       # dependencies: []  -> declared none
            return _split_inline(body)
        if rest == "":
            items = []
            for nxt in lines[i + 1:]:
                if nxt.strip() == "":
                    continue
                if nxt[:1] in " \t" and nxt.strip().startswith("- "):
                    # Raw token, quotes intact: the block form goes through the SAME item policy
                    # the inline form does. Stripping quotes here is what let `- "a, b"` and
                    # `- ""` past the bounded rules `[...]` enforces, so the two spellings of one
                    # declaration disagreed about what the body said.
                    items.append(nxt.strip()[2:])
                    continue
                if nxt[:1] in " \t":
                    return None, False                # indented, but not a block sequence
                break
            if not items:
                return None, False                    # bare key: null, not an empty sequence
            # A block sequence cannot produce a blank token (`- ` alone does not match "- "), so
            # the trailing-separator rule below is unreachable from here and the two forms share
            # one policy without the block form inheriting an inline-only allowance.
            return _dep_items(items)
        return None, False                            # a scalar or a form not read here
    return None, True                                 # absent -> unknown


DEP_QUOTES = "\"'"


def _dep_item(tok):
    """(item, supported) for ONE raw dependency token under the bounded grammar.

    A token is a bare name, or one quote pair wrapping the whole item and nothing else. Anything
    else is refused, because every other reading of it invents a dependency:

      a"b"     concatenating to `ab` reports a name the body never wrote
      "a"x     the same, one character later
      "a, b"   a quoted comma is the shape the splitter would have divided on; the inline form
               already refuses it, and the block form must not become the way around that
      ""       an empty item is not a dependency, and dropping it turns `[rg,,git]` into a
               two-item list nobody declared

    Refusing is not a loss: the record says unsupported-metadata, which is a publishable fact
    about the body. Guessing produces a record that states dependencies as observed."""
    t = tok.strip()
    if t.startswith(("\"", "'")):
        if len(t) < 2 or t[-1] != t[0]:
            return None, False                # unterminated, or a suffix after the closing quote
        t = t[1:-1]
        if any(c in t for c in DEP_QUOTES) or "," in t:
            return None, False                # a second quote pair, or a quoted comma
    elif any(c in t for c in DEP_QUOTES):
        return None, False                    # a quote opening mid-token
    t = t.strip()
    return (t, True) if t else (None, False)


def _dep_items(toks):
    """(items, supported) for the raw tokens of one dependency list.

    The trailing-separator rule, stated once: a SINGLE empty token at the end is the ordinary
    flow-sequence trailing comma and is dropped, so `[rg,]` declares `["rg"]`. It does not become
    declared-none, and it is the only empty token this grammar accepts. A leading or repeated
    separator (`[,rg]`, `[rg,,git]`) is refused instead: an empty slot in the middle of a list is
    an item the writer meant to be there, and dropping it publishes a shorter list as fact."""
    if len(toks) > 1 and toks[-1].strip() == "":
        toks = toks[:-1]
    items = []
    for tok in toks:
        item, ok = _dep_item(tok)
        if not ok:
            return None, False
        items.append(item)
    return items, True


def _split_inline(body):
    """(items, supported) for the inside of an inline `[...]` dependency list.

    Splitting on every comma turns one quoted `"ripgrep, or rg"` into two dependencies that the
    body never declared, and the record then reports invented facts with no sign anything was
    lost. This is a bounded grammar, not a YAML parser: this function only finds the commas that
    separate items, and `_dep_item` decides whether each token is a shape it can read."""
    toks, cur, quote = [], "", ""
    for ch in body:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
        elif ch in DEP_QUOTES:
            quote = ch
            cur += ch
        elif ch == ",":
            toks.append(cur)
            cur = ""
        else:
            cur += ch
    if quote:
        return None, False                            # unterminated quote: shape is not readable
    toks.append(cur)
    return _dep_items(toks)


def _facts(text, raw):
    """Observed metadata, or (None, reason) when the frontmatter is not a shape this reads.

    `description` is what the host triggers on, so a frontmatter block without one is malformed
    for this purpose. The description is read through scan.description_from, the scanner's own
    grammar, so quoted, folded and block forms give the same value the inventory shows."""
    fm = scan.frontmatter(text)
    if not fm.strip():
        return None, "malformed-metadata"
    desc = scan.description_from(fm)
    if not desc:
        return None, "malformed-metadata"
    facts = {"bytes": len(raw), "declared_description": desc}
    for line in fm.splitlines():
        if line.startswith("name:"):
            facts["frontmatter_name"] = line[len("name:"):].strip()
    deps, supported = _dependencies(fm)
    if not supported:
        return None, "unsupported-metadata"
    if deps is not None:
        # Declared, not observed. Nothing here checks that any of these is installed.
        facts["declared_dependencies"] = deps
    return facts, None


def inspect(name, srcdir, host, invocable=True):
    """One record's facts. Never raises for a bad skill: a bad skill becomes an explicit
    unresolved record, which is the whole point of the status field."""
    # This resolve-with-spelling-fallback is the record's half of the identity boundary;
    # `root_source` is the reducer's half and must keep answering about the same directories.
    try:
        source = str(srcdir.resolve())
    except OSError:
        source = str(srcdir)
    md = Path(source) / "SKILL.md"
    rec = {"name": name, "host": host, "source": source, "skill_md": str(md),
           "digest": "", "inputs": [], "status": "unresolved", "reason": "unreadable",
           "facts": {}, "assessment": None, "invocable": bool(invocable)}
    try:
        if not md.is_file() or md.stat().st_size > MAX_BODY:
            return rec
        raw = md.read_bytes()
    except OSError:
        return rec
    rec["digest"] = digest(raw)
    # Only files actually read and hashed are listed, so a record can never imply it inspected
    # something it did not open.
    rec["inputs"] = [{"path": rec["skill_md"], "digest": rec["digest"]}]
    try:
        # Strict, unlike scan.desc_of's errors="replace": an undecodable body is a fact to report,
        # not a blank description to classify around.
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        rec["reason"] = "invalid-utf8"
        return rec
    facts, bad = _facts(text, raw)
    if facts is None:
        rec["reason"] = bad
        return rec
    rec["facts"] = facts
    rec["reason"] = "unassessed" if invocable else "not-invocable"
    return rec


def _withdraw(r, reason, drop_observations=False):
    """Put `r` into the unresolved state `reason` names: status, reason and assessment always, and
    the observed payload only when `drop_observations` asks for it.

    Status, reason and assessment only mean anything together. A reason updated under a stale
    "assessed" status, or an assessment left behind on a record nobody may act on, is precisely the
    shape that carries a withdrawn identity back out as a recommendation.

    `drop_observations` additionally clears the observed payload, for the dispositions where
    keeping it is the defect: no authoritative observed payload is selected under any of the three
    dispositions below, so a digest, facts and provenance left attached there would be published as
    the record's own when "no winner was picked" is the whole content of the state. The empty
    payload is what the schema already provides for it: `ambiguous-identity` is the one reason
    `_validate_facts` lets go either way on facts, and `_validate_provenance` requires an empty
    `inputs` for an empty digest, which is exactly this shape.

    An empty payload here means NO AUTHORITATIVE OBSERVATION WAS SELECTED, and nothing more. It
    is not a claim about how many readings were attempted, how many reached the body, or whether
    any of them succeeded; a captured payload and an uncaptured one can be what conflicts.

    Three separate dispositions reach it, and the generic wording above must fit all three:

      - two observations OF THIS IDENTITY were recorded and their captured state differs;
      - an ordinary observation of the same child source conflicts with this qualified identity
        and transfers, which is settled at the SOURCE - the ordinary sighting's displayed name
        need not be this identity's, so the second observation may be of another name entirely;
      - two distinct represented plugin hosts compete for this displayed name at this source,
        which withdraws it even when both captured the same bytes.

    So neither "a second observation under this name" nor "they disagree about bytes" is a
    condition, and neither may be stated as one. `ambiguous-identity` is also reached WITHOUT
    `drop_observations`, for one name at two sources: that record keeps the payload it observed,
    and the reason alone does not say the payload was cleared.

    The ordinary read/parse failures keep their own payload contract and are not cleared here:
    `unreadable` has no captured body provenance because the attempt returned no body bytes to it,
    while `invalid-utf8` and the metadata failures DID capture the body and keep its digest and
    canonical row, having failed only to parse facts out of it."""
    r["status"], r["reason"], r["assessment"] = "unresolved", reason, None
    if drop_observations:
        r["digest"], r["facts"], r["inputs"] = "", {}, []


# What two records for ONE identity can disagree about and still both be real observations of it.
# `name`, `source` and `skill_md` are the identity; `status` and `reason` are derived from these
# plus `invocable`, which is conjoined rather than compared.
#
# `host` is absent because it is not an observation OF the body - see the fold below, where it is
# handled on its own terms. A bare and a plugin observation can share one displayed name at one
# source, because the displayed name of a plugin skill is `<plugin>:<skill>` and a bare directory
# may be named that literally. That is a difference in QUALIFICATION, which decides precedence,
# and it is settled rather than withdrawn. Two plugin origins reaching one displayed name at one
# source is neither: it is settled nowhere and withdrawn there, also below.
_OBSERVED = ("digest", "facts", "inputs")
# Reasons `resolve_identity` may overwrite with `not-invocable`. The remaining unresolved reasons
# all describe a body that failed to read or parse: they are already unassessable, they carry no
# facts, and `not-invocable` requires facts - so restating them would both lose the diagnostic and
# produce a record `validate_record` refuses.
_INVOCABILITY_OVERWRITABLE = {"", "unassessed", "not-invocable"}


def _qualifies(r):
    """Whether this observation is a plugin's. Qualification is a fact about the HOST, which is
    what makes a skill a plugin's; never about a colon in the displayed name, which any bare
    directory may contain."""
    return r["host"].startswith("plugin:")


def resolve_identity(records):
    """Identity is (name, resolved path), never the displayed name alone.

    A symlink and its target resolve to one path and collapse to one record. A name that resolves
    to two distinct real paths is ambiguous and stays that way: no root wins by precedence, and
    guessing here would silently attach one skill's judgment to another's bytes.

    This is where identity is actually settled, because it is the first place the CHILD sources are
    known. `resolve_root_identities` reduced the roots, but two roots it kept apart on a real
    difference can still hold children that resolve together, and the record's source is the
    child's. So three things happen here, in this order, before ambiguity is judged.

    First, records for one (name, source) are CONJOINED instead of first-won. The old dedup kept
    whichever arrived first and dropped the rest, so a `False` invocable carried by a later one was
    discarded and which eligibility survived depended on root order. Eligibility is now the AND
    over every observation: order-independent, and unable to lose a disable. Where the observations
    themselves disagree - different bytes, different parsed facts, different provenance rows for
    one identity - publishing either one means silently picking a winner for the other and
    attaching whatever judgment follows to bytes that may not be the ones read. Such an identity is
    unresolved and carries nothing. That is not a new state: `ambiguous-identity` already means
    "this name does not resolve to one thing", and it is already the one reason allowed to go
    either way on facts.

    Second, qualified beats unqualified AT THE CHILD SOURCE. `resolve_root_identities` applies that
    rule over roots and structurally cannot reach this case: when only the children converge, the
    roots are legitimately distinct, so a bare invocable name survived over a plugin the host had
    turned off - the exact outcome the rule exists to prevent, one level down. A shared root helper
    would not have found it either, for the same reason: the roots really are two. Precedence is
    decided on HOST, which is what makes a skill a plugin's; not on a colon in the displayed name,
    which any bare name may contain. The unqualified record's own eligibility folds into every
    qualified record at that source before it goes, so a `skillOverrides` disable on the bare name
    is not lost by being outranked.

    Distinct qualified names sharing one source are untouched by all of this. They are two
    identities that happen to share bytes, each keeps its own eligibility, and they are not an
    ambiguity - sharing a directory is not sharing a name.

    Third, and only over what remains, one name at two sources is ambiguous exactly as before.
    Doing it last is what makes it correct: a bare name that lost to a plugin is no longer in the
    inventory, so it can no longer make a surviving copy of that name look contested.

    THE FOLD KEYS ON ROLE AS WELL AS (name, source), which is what makes step two independent of
    input order without discarding anything step two then needs. A bare and a plugin observation
    CAN share one displayed name at one source: a plugin skill is displayed `<plugin>:<skill>`, and
    an ordinary sighting reaches that same string either as a directory literally named it, where
    the filesystem allows a colon, or - on any filesystem - as an ordinary BUNDLE `<plugin>` whose
    `skills/<skill>` `scan.bundle_skills` names by that same rule. Keeping the first record
    whole read precedence off whichever arrived first; merging them under the plugin host fixed
    that, but then the bare observation was no longer anyone's bare sighting, and its disable and
    its disagreement stopped reaching the OTHER qualified names at that directory. Both are the
    same mistake made at different times - deciding a role before every contribution under it has
    been collected. So the roles are kept apart here, transferred at the source in step two, and
    only then does the losing one go.

    That settles qualification. It does not settle TWO PLUGINS' competing claims to one displayed
    name at one source, and nothing here can: each says the name is that plugin's, and choosing
    between them by any rule available at this layer - lexical order, arrival order, punctuation -
    would hand one plugin's authority over a name the other also claims. The composition is not
    hypothetical: `candidates` builds the displayed name from a plugin name and an entry name it
    does not restrict, so plugin `a` over a bundle `b/skills/c` and plugin `a:b` over `skills/c`
    compose `a:b:c` at one child with no colon in any filesystem component. Such an identity is
    unresolved and carries nothing, exactly as a disagreement about bytes is. Its eligibility is
    still the conjunction, and its published host is a deterministic label - the lexical minimum
    of the hosts that reported the SURVIVING group, never a minimum taken across the bare and
    plugin sightings of one name - so that the record can be named at all. That is an inventory
    label, not an origin chosen and not authority granted; the same is true of the ordinary host
    on a coherent record whose sightings came from more than one ordinary root."""
    folded, index, conflict, origins = [], {}, set(), []
    for r in records:
        # ROLE is part of the fold key. Merging an equal-name bare and plugin observation here and
        # promoting the host settled qualification correctly, but it ERASED the bare role before
        # `outranked` was built: that observation was no longer any source's bare sighting, so its
        # disable and its disagreement stopped reaching every OTHER qualified identity at the same
        # directory - the transfer below is what they were supposed to reach. The two roles are
        # reconciled after every contribution is collected, not merged before any of it is.
        key = (r["name"], r["source"], _qualifies(r))
        at = index.get(key)
        if at is None:
            index[key] = len(folded)
            folded.append(r)
            origins.append({r["host"]})
            continue
        kept = folded[at]
        kept["invocable"] = bool(kept.get("invocable", True)) and bool(r.get("invocable", True))
        origins[at].add(r["host"])
        if r["host"] < kept["host"]:
            kept["host"] = r["host"]
        if any(kept.get(f) != r.get(f) for f in _OBSERVED):
            conflict.add(at)
    # A group's `host` is the lexical minimum of the hosts that reported THAT GROUP - one fold
    # key, so one role. The minimum is never taken across a bare and a plugin sighting of one
    # name: those are two groups, precedence between them is decided below on qualification, and
    # a bare host that sorts first cannot label a record a plugin won. Keeping whichever arrived
    # first was observable and order-dependent for ordinary hosts too, not only across the
    # bare/plugin line. What the minimum is, is a DETERMINISTIC INVENTORY LABEL: one existing host
    # that really reported this name at this source. It is not a complete list of origins and it
    # confers no authority.
    #
    # For two admitted PLUGIN origins at one displayed name and source, a label is not enough.
    # Each is a claim that this name is that plugin's; they cannot both govern it, and publishing
    # the smaller one would attach the surviving plugin's authority - and any judgment that
    # follows - to a name the other also claims. `candidates` composes the displayed name as
    # `<plugin><colon><entry>` from names it does not restrict, so plugin `a` over bundle
    # `b/skills/c` and plugin `a:b` over `skills/c` compose one displayed name with no colon in
    # any filesystem component. That is an unresolved identity in the vocabulary that already
    # exists: withdrawn with a neutral payload, eligibility still conjoined, no new state and no
    # naming rule invented to forbid the composition.
    #
    # STATED LIMIT, so the check is not read as more than it is. An origin here is the REPRESENTED
    # HOST LABEL, and that label is `plugin:` plus the short plugin name `scan.installed_plugins`
    # derives by dropping everything from the manifest key's `@`. Two installed keys that differ
    # only past that `@` therefore report one label, and if their trees also converge on one final
    # child directory this is one represented origin, folded and not withdrawn. What the check
    # covers is DISTINCT represented hosts; a count of sightings under one host is deliberately
    # not a competition, and install-instance identity - marketplace, version, manifest key - is
    # not modelled at this layer at all. Carrying one would have to start where the key is read,
    # not here.
    for at, hosts in enumerate(origins):
        if len(hosts) > 1 and _qualifies(folded[at]):
            conflict.add(at)
    qualified = {r["source"] for r in folded if _qualifies(r)}
    outranked = {at for at, r in enumerate(folded)
                 if not _qualifies(r) and r["source"] in qualified}
    # Every fold that can still change an eligibility runs before anything reads one. Stamping a
    # record's reason in the same pass would read whatever its invocable happened to be at that
    # index and miss a disable folded in from a later one - order dependence reintroduced at the
    # step that exists to remove it.
    #
    # A removed record's DISAGREEMENT carries the same way its disable does. The bare label is what
    # loses precedence; what it observed at that directory is not thereby retracted, and dropping
    # it silently would let removal certify one qualified reading of a body another sighting read
    # differently. Both directions count: a group of bare sightings that already disagreed among
    # themselves, and a bare sighting that disagrees with the qualified record it loses to.
    # Sharing a source is not itself a disagreement - coherent records at one directory fold
    # nothing here and keep their own identity and eligibility.
    #
    # The transfer is per SOURCE, to every qualified identity there, and it does not care whether
    # the bare name equals one of theirs. That is the whole point: a bare sighting whose displayed
    # name collides with `alpha`'s is still an ordinary sighting of that directory, and `beta` at
    # the same directory is as entitled to it as `alpha` is. The reverse never runs - one plugin's
    # own disable is its own, and sharing bytes with another plugin does not transfer it.
    for at in outranked:
        r = folded[at]
        for qi, q in enumerate(folded):
            if _qualifies(q) and q["source"] == r["source"]:
                q["invocable"] = bool(q.get("invocable", True)) and bool(r.get("invocable", True))
                if at in conflict or any(q.get(f) != r.get(f) for f in _OBSERVED):
                    conflict.add(qi)
    unique = []
    for at, r in enumerate(folded):
        if at in outranked:
            continue
        if at in conflict:
            # Withdrawal for disagreement runs before the eligibility stamp, so a conflicted
            # identity cannot be relabelled `not-invocable` - a reason that asserts a body parsed
            # to one set of facts, which is the claim in dispute.
            _withdraw(r, "ambiguous-identity", drop_observations=True)
        elif not r.get("invocable", True) and r.get("reason") in _INVOCABILITY_OVERWRITABLE:
            # The conjunction above can turn an eligibility False after `inspect` already wrote the
            # reason for the True it saw. Leaving "unassessed" there publishes a disabled identity
            # as an ordinary one waiting for judgment, which is a state the consumer will act on.
            _withdraw(r, "not-invocable")
        unique.append(r)
    paths = {}
    for r in unique:
        paths.setdefault(r["name"], set()).add(r["source"])
    for r in unique:
        if len(paths[r["name"]]) > 1:
            _withdraw(r, "ambiguous-identity")
    return unique


# ---------------------------------------------------------------- assessment (judgment)

def validate_assessment(a):
    """Normalized assessment, or None. Rejection is the honest outcome for a malformed assessor
    result; there is no partial accept and no default category."""
    if not isinstance(a, dict):
        return None
    allowed = set(TEXT_FIELDS) | set(LIST_FIELDS) | {"assessment_version"}
    if set(a) - allowed:
        return None  # unknown keys are a schema failure, not fields to carry along
    if a.get("assessment_version", ASSESSMENT_VERSION) != ASSESSMENT_VERSION:
        return None
    out = {"assessment_version": ASSESSMENT_VERSION}
    for f in TEXT_FIELDS:
        v = a.get(f)
        if not isinstance(v, str) or not v.strip():
            return None
        out[f] = v
    for f in LIST_FIELDS:
        v = a.get(f, [])
        if not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v):
            return None
        out[f] = list(v)
    return out


def supporting_inputs(entry, rec):
    """(inputs, error) for a supplied entry: the SKILL.md plus any supporting files it names.

    An assessment based only on the body needs no supporting inputs and gets none invented for it.
    A supporting input that is named must resolve, must still be readable, and its digest must be
    the bytes on disk now - a claim about a file the assessor did not actually read, or read in a
    different state, is refused rather than persisted as provenance."""
    inputs = [{"path": rec["skill_md"], "digest": rec["digest"]}]
    named = entry.get("inputs")
    if named is None:
        return inputs, None
    if not isinstance(named, list):
        return None, "'inputs' must be a list"
    for i in named:
        if not isinstance(i, dict) or set(i) != {"path", "digest"} \
                or not isinstance(i.get("path"), str) or not _is_digest(i.get("digest", "")):
            return None, "each supporting input must be exactly {path, digest}"
        try:
            p = str(Path(i["path"]).resolve())
            raw = Path(p).read_bytes()
        except OSError:
            return None, f"supporting input is unreadable: {i['path']}"
        if digest(raw) != i["digest"]:
            return None, f"supporting input digest does not match its bytes: {i['path']}"
        if p == rec["skill_md"]:
            continue  # already carried, and carrying it twice would be a duplicate path
        inputs.append({"path": p, "digest": i["digest"]})
    return inputs, None


def inputs_current(rec):
    """True when every input this record claims to have assessed is still exactly those bytes.

    Checked at audit and again at consumption: a judgment that rested on a reference file is no
    longer supported once that file changes or disappears, even if the SKILL.md never moved."""
    for i in rec.get("inputs") or []:
        try:
            raw = Path(i["path"]).read_bytes()
        except OSError:
            return False
        if digest(raw) != i["digest"]:
            return False
    return True


def load_supplied(path):
    """{(name, source): entry} from an assessor output file.

    Shape errors here are argument errors, not per-record unresolved states: the file as a whole
    did not parse, so no part of it can be trusted to mean what it says. Two entries for one
    identity is such an error - silently letting the last one win would make which judgment landed
    depend on file order."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"unusable assessments file {path}: {e}")
    if not isinstance(data, list):
        raise ValueError(f"unusable assessments file {path}: expected a list of entries")
    out = {}
    for n, e in enumerate(data):
        if not isinstance(e, dict):
            raise ValueError(f"unusable assessments file {path}: entry {n} is not an object")
        extra = set(e) - SUPPLIED_KEYS
        if extra:
            raise ValueError(f"unusable assessments file {path}: entry {n} has unknown "
                             f"field(s) {sorted(extra)}")
        if not isinstance(e.get("name"), str) or not isinstance(e.get("source"), str):
            raise ValueError(f"unusable assessments file {path}: entry {n} needs name and source")
        key = (e["name"], e["source"])
        if key in out:
            raise ValueError(f"unusable assessments file {path}: duplicate entry for "
                             f"{e['name']!r} at {e['source']!r}")
        out[key] = e
    return out


# ---------------------------------------------------------------- store

def load_store(path):
    """Records from the store, [] when it does not exist yet.

    A corrupt, future-version or incomplete store raises. It is not overwritten from empty and not
    repaired: destroying the evidence that something is wrong, along with the state, is worse than
    stopping. Recovery is to inspect or delete the machine-local file and re-audit."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"unusable store {path}: {e}")
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"unusable store {path}: not a {SCHEMA} file")
    # An unknown top-level field is state this program does not understand. Reading past it would
    # publish a store with that field dropped, which is a silent repair of someone else's data.
    extra = set(data) - STORE_KEYS
    if extra:
        raise ValueError(f"unusable store {path}: unknown top-level field(s) {sorted(extra)}")
    if data.get("version") != VERSION:
        raise ValueError(f"unusable store {path}: version {data.get('version')!r}, "
                         f"expected {VERSION}")
    return validate_records(data.get("records"), f"unusable store {path}")


def serialize(records):
    """The exact bytes of the store. Sorted, no timestamps, LF - so an unchanged audit produces a
    byte-identical file and re-running it is not a diff."""
    payload = {"schema": SCHEMA, "version": VERSION,
               "records": sorted(records, key=lambda r: (r["name"], r["source"]))}
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def publish(path, records):
    """Write the complete validated state, or leave the previous file exactly as it was.

    Every record is validated before anything is written, so a half-formed state cannot reach the
    file. A byte-identical result is not rewritten at all: no replacement, no churn. The temp file
    is unique to this attempt and in the same directory, so a concurrent attempt cannot delete
    ours and the replace stays atomic on the same volume.

    This does not merge concurrent logical updates. Two audits racing will each publish a complete
    state and the later replace wins outright; unique temp names prevent a corrupt file, not a lost
    update. No locking is implemented."""
    validate_records(records, "refusing to publish")
    blob = serialize(records)
    p = Path(path)
    if p.exists():
        try:
            if p.read_bytes() == blob:
                return False  # unchanged: do not touch the file at all
        except OSError:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f".{p.name}.{os.getpid()}.{id(records):x}.tmp"
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()  # only ever our own attempt's file
        except OSError:
            pass
        raise
    return True


# ---------------------------------------------------------------- lifecycle

def audit(roots, store_path, supplied=None, overrides=None):
    """discover -> identity -> digest -> carry or invalidate judgment. Returns (records, rejected).

    `rejected` names supplied entries that were refused in favour of state already on disk, so a
    stale submission is visible rather than silently dropped.

    An identity that is gone from the roots is simply not in the result, which is what keeps it
    out of recommendations; the store is current state, not a log."""
    supplied = dict(supplied or {})
    prior = {(r["name"], r["source"]): r for r in load_store(store_path)}
    records = resolve_identity([inspect(n, d, h, inv)
                                for n, d, h, inv in candidates(roots, overrides)])
    rejected = []
    for r in records:
        key = (r["name"], r["source"])
        if r["reason"] in UNASSESSABLE:
            if key in supplied:
                rejected.append((key, f"identity is {r['reason']}"))
            continue
        old = prior.get(key)
        carried = None
        if old and old.get("status") == "assessed" and old.get("digest") == r["digest"] \
                and inputs_current(old):
            carried = old
        got = supplied.get(key)
        if got is not None:
            if got.get("digest") != r["digest"]:
                # Assessed against bytes that are not the bytes on disk now. If a valid current
                # judgment already exists, keep it rather than downgrading to unresolved.
                if carried is not None:
                    rejected.append((key, "supplied digest is stale; kept the current assessment"))
                    r["status"], r["reason"] = "assessed", ""
                    r["assessment"], r["inputs"] = carried["assessment"], carried["inputs"]
                else:
                    rejected.append((key, "supplied digest does not match the bytes on disk"))
                    r["reason"] = "stale-digest"
                continue
            ok = validate_assessment(got.get("assessment"))
            if ok is None:
                # The original contract: a malformed judgment is explicitly unresolved.
                rejected.append((key, "supplied assessment is malformed"))
                r["reason"] = "malformed-assessment"
                continue
            inputs, bad = supporting_inputs(got, r)
            if inputs is None:
                rejected.append((key, bad))
                r["reason"] = "malformed-assessment"
                continue
            r["status"], r["reason"], r["assessment"], r["inputs"] = "assessed", "", ok, inputs
            continue
        if carried is not None:
            r["status"], r["reason"] = "assessed", ""
            r["assessment"], r["inputs"] = carried["assessment"], carried["inputs"]
        elif old and old.get("status") == "assessed":
            # Changed bytes, or a supporting input that moved, invalidate the old judgment before
            # anything can reuse it.
            r["reason"] = "stale-digest" if old.get("digest") != r["digest"] else "stale-input"
    for key, why in ((k, "no such installed identity in the audited roots")
                     for k in supplied if k not in {(r["name"], r["source"]) for r in records}):
        rejected.append((key, why))
    return records, rejected


def recommendable(store_path, roots, overrides=None):
    """What the recommendation step may use: assessed, still installed *now*, still the same bytes,
    still supported by every input it was based on, and invocable.

    Current identity is resolved against the same roots the audit uses rather than trusting the
    stored path. A skill uninstalled since the audit, or one whose name a second install has made
    ambiguous, must stop being recommended even though its old target bytes are still on disk."""
    live = {}
    for r in resolve_identity([inspect(n, d, h, inv)
                               for n, d, h, inv in candidates(roots, overrides)]):
        live[(r["name"], r["source"])] = r
    out = []
    for r in load_store(store_path):
        if r["status"] != "assessed":
            continue
        now = live.get((r["name"], r["source"]))
        if now is None or now["reason"] in UNASSESSABLE or not now.get("invocable", True):
            continue
        if now["digest"] != r["digest"] or not inputs_current(r):
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- cli

def public_view(r):
    """The documented record shape. Same fields the store holds, so an agent can build assessor
    input from this output alone without reading the store."""
    return {k: r[k] for k in sorted(RECORD_KEYS) if k in r}


def report_rejected(rejected, stream):
    """One line per refused submission. Same text the human report prints, on the stream the
    mode leaves free, so rejection is observable in every legal audit mode rather than only the
    one that happens to print prose."""
    for (name, source), why in rejected:
        print(f"rejected: {quoted(name)} {quoted(source)}: {why}", file=stream)


def report(records):
    assessed = [r for r in records if r["status"] == "assessed"]
    print(f"{len(records)} skills, {len(assessed)} assessed, "
          f"{len(records) - len(assessed)} unresolved")
    by_reason = {}
    for r in records:
        if r["status"] != "assessed":
            by_reason.setdefault(r["reason"], []).append(r)
    for reason in sorted(by_reason):
        print(f"{reason}:")
        for r in sorted(by_reason[reason], key=lambda r: (r["name"], r["source"])):
            print(f"  {quoted(r['name'])} {quoted(r['source'])} {quoted(r['skill_md'])} "
                  f"{r['digest'] or '-'}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, mode, as_json = {}, "audit", False
    modes = []
    while argv:
        a = argv.pop(0)
        if a == "--recommendable":
            mode = "recommendable"
            modes.append(a)
        elif a == "--work-list":
            mode = "work-list"
            modes.append(a)
        elif a == "--json":
            as_json = True
        elif a in ("--roots", "--store", "--assessments", "--project"):
            if not argv:
                print(f"skill_audit: {a} needs a value", file=sys.stderr)
                return 2
            opts[a] = argv.pop(0)
        else:
            print(f"skill_audit: unknown argument {a!r}", file=sys.stderr)
            return 2
    # Before anything reads a root or touches the store. An option that cannot apply in the mode
    # it was given with is a mistake about what this run will do, and the honest answer is to
    # refuse it, not to run a different command than the one that was typed.
    if len(set(modes)) > 1:
        print(f"skill_audit: {' and '.join(sorted(set(modes)))} are different modes; pick one",
              file=sys.stderr)
        return 2
    if mode == "recommendable" and "--assessments" in opts:
        print("skill_audit: --assessments does not apply to --recommendable, which consumes the "
              "store and never ingests; run the audit first", file=sys.stderr)
        return 2
    store = opts.get("--store") or str(Path(__file__).resolve().parent.parent / STORE)
    try:
        rootnames = [s for s in (opts.get("--roots") or "").split(",") if s.strip()]
        roots = skill_roots(rootnames, opts.get("--project"))
        # Explicit roots are a bounded inventory of their own; the host's per-skill enable map
        # describes the real installation and does not apply to one.
        overrides = None if rootnames else skill_overrides(opts.get("--project"))
        if mode == "recommendable":
            got = sorted(recommendable(store, roots, overrides),
                         key=lambda r: (r["name"], r["source"]))
            if as_json:
                print(json.dumps([public_view(r) for r in got], indent=2, sort_keys=True))
            else:
                for r in got:
                    print(f"{quoted(r['name'])} {quoted(r['source'])} "
                          f"{quoted(r['assessment']['purpose'])}")
            return 0
        supplied = load_supplied(opts["--assessments"]) if "--assessments" in opts else None
        records, rejected = audit(roots, store, supplied, overrides)
        # stdout carries the mode's documented payload and nothing else, so a JSON consumer keeps
        # parsing. A refused submission still has to be visible somewhere, or an assessor that
        # published against stale bytes gets exit 0 and an empty stream and believes it landed.
        #
        # EVERY mode, not only the machine ones. Routing plain-mode rejections into the human
        # report put them on the one stream a `> out.txt` redirect captures and a terminal user
        # reads last, and made "where is a rejection reported" depend on which flags were typed.
        # One stream, one answer, in all three legal audit modes.
        report_rejected(rejected, sys.stderr)
        if mode == "work-list":
            print(json.dumps([public_view(r) for r in sorted(records, key=lambda r: r["name"])
                              if r["status"] != "assessed" and r["reason"] not in UNASSESSABLE],
                             indent=2, sort_keys=True))
            return 0
        if not records:
            # Same policy either way - nothing is published and the exit stays 1 - but --json
            # promised stdout is a JSON list, and a consumer that gets prose there fails at
            # parse and never reaches the exit code that would have explained it. The empty
            # inventory is the list; the explanation goes where the other diagnostics go.
            print("no skills found in any root; nothing published", file=sys.stderr)
            if as_json:
                print(json.dumps([], indent=2, sort_keys=True))
            return 1
        publish(store, records)
        if as_json:
            print(json.dumps([public_view(r) for r in
                              sorted(records, key=lambda r: (r["name"], r["source"]))],
                             indent=2, sort_keys=True))
        else:
            report(records)
    except DiscoveryError as e:
        print(f"skill_audit: {e}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"skill_audit: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
