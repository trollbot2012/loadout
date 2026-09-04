# Loadout: loadout
Harness: claude-code | Project type: CLI/library (stdlib Python scanner + portable SKILL.md), post-merge, release bookkeeping behind
Date: 2026-09-03
Enforcement: claude-code gate registered
Supersedes: loadout of 2026-09-03 (earlier run, pre-PR #6)

## What changed since the previous run
- PR #6 merged to main (3729e7e): skill-notes reference, MCP-capability rule, absent-skill rule, `check_notes.py`. 128 tests, CI 12/12.
- First audit run under the new step-3 rules. `check_notes.py --installed` reports 163 rows / 22 groups / 0 problems against this host's 165 listed skills, so every group preference names a skill that exists here.
- Version drift is now the live issue: SKILL.md frontmatter says 1.5.0, but no v1.5.0 tag was ever cut and 33 commits sit on top of v1.4.0.
- Workflow carried over unchanged; it earned its place this session (code-review found 5 real defects, and the two-arm subagent tests overturned two of my own wordings).

## Recommended workflow
1. Planning → `planning-with-files` — task_plan.md / progress.md are the durable record; they carried this session's context across a model switch
2. Skill authoring → `superpowers:writing-skills` — every SKILL.md rule is a skill edit; baseline-test the wording with subagents before adopting it
3. Implementation → `superpowers:test-driven-development` — 128 tests on a 3.9/3.11/3.13 x ubuntu/windows matrix; failing test first
4. Review → `code-review` — Standards + Spec in parallel; found 5 defects on the last diff that self-review had missed
5. Verify → `superpowers:verification-before-completion` — run the commands, quote the output; a green local run is not a green CI run
6. Finish → `superpowers:finishing-a-development-branch` — branch, PR, CI, merge, then re-run `--self-install` (a merge checkout renormalises line endings and makes every root stale)

## Situational (invoke when relevant)
- `superpowers:systematic-debugging` — when a test or a CI leg goes red. Deliberately not binding: a completion gate must not require manufacturing a failure to satisfy a stage
- `superpowers:dispatching-parallel-agents` — regenerating the notes table (8-way fan-out over ~160 skill dirs)
- `unlazy` — gated multi-step work; the repo already uses the GATES.md pattern
- `loopy` — if the audit / re-sync cycle becomes a repeatable loop
- `loadout` — re-run on itself after each version (this file is that dogfood)

## Capabilities (not stages; nothing here is numbered)
- `claude-code-docs` (MCP) — research/docs; I call it. This project implements Claude Code hooks, settings and skill loading, so the harness documentation is its primary source
- `context7` (MCP) — research/docs; I call it. Available but marginal here: the scanner is stdlib-only and has no third-party API to look up
- `Explore` (subagent) — delegation; I dispatch it. Read-only fan-out when a question spans many files, as the notes-table regeneration did
- `claude-code-guide` (subagent) — research/docs; I dispatch it. Answers hook, settings and skill-loading questions, which is this project's whole subject matter
- commands add nothing here: the six live ones are `ponytail:*`, the same capabilities as the ponytail plugin-skills on a second surface, so they are listed once as skills
- the three disk-listed agents (`feature-dev:*`) are all off behind a disabled plugin and would need re-enabling before use

## Skip / noise for this project
- `plan-blueprint-tdd`, `lazy-planner`, `superpowers:brainstorming` — greenfield planners; this is maintenance on a shipped skill
- `write-a-skill`, `writing-great-skills`, `anthropic-skills:skill-creator` — same job as superpowers:writing-skills; the notes table records it as the preferred member
- `full-output-enforcement` — conflicts with the always-on ponytail SessionStart hook (minimal output vs exhaustive)
- all `*-delegate` skills — a 900-line stdlib project; nothing here needs a background implementer
- all frontend/design skills and the `figma:*`, `playwright`, `chrome-devtools`, `browser-use` MCP servers — no UI, no browser
- mantis-* — no trust boundary worth a 20-stage campaign; the scanner reads names and frontmatter only
- `agent-reach`, `research` — no external research needed; claude-code-docs covers the one docs need

## Blocked
- `devteam:pipeline` — now visible in the listing (finding 2 fixed), but still not runnable here: it needs a `pipeline.json` in the workspace, which this repo does not have, and it is user-invoked only (`/devteam:pipeline`). Unblock: add a pipeline.json if this repo ever adopts the devteam pipeline.
- nothing else (git repository present, main in sync with origin, Python available, 128 tests green)

## Findings
1. CLOSED, not a defect (was carried through three audits on a wrong premise). The two maps cannot drift silently: `tests/test_apply.py::test_native_file_table_matches_the_scanner` asserts `apply.NATIVE == scan.NATIVE_FILES`, so adding a host to one alone fails the suite. Importing was also the worse trade: apply.py imports only stdlib today, and reaching a sibling module needs sys.path manipulation plus a 34KB import, from installed copies at arbitrary paths, to delete one line of data a one-line test already guards.
2. FIXED The scanner now recurses one level into `<bundle>/skills/` when a directory has no SKILL.md of its own, listing each nested skill as `<bundle>:<skill>` and dropping the bare bundle name, which was neither classifiable nor invocable. `devteam` now reports as `devteam:pipeline` with its full description.
3. FIXED `check_notes.py --installed` now reports every installed skill with no row. Coverage cannot be proven from the table alone, so it is checked only against a live listing. On its first run it found two real gaps (`figma:figma-shaders`, `figma:figma-generative-plugins`, shipped by a later plugin release than the table was generated against); rows added, now 165 rows against 165 listed skills.
4. FIXED Loadout now surfaces agents and commands. The `## Capabilities` section carries MCP servers, subagents and commands with what each is and who invokes it; the numbered workflow takes entries from `### skills` and `### plugin-skills` only. Baseline: a command (`security-review`) was recommended as a binding stage and both agents were ignored. 3/3 reps correct after the fix.
5. OPEN Version drift: frontmatter says 1.5.0, no v1.5.0 tag exists, 33 commits sit on v1.4.0. No changelog.

## Gaps
- none by category; every needed category is covered by an installed skill or a connected MCP server

## Accepted
- planning: `planning-with-files`
- skill authoring: `superpowers:writing-skills`
- implementation: `superpowers:test-driven-development`
- review: `code-review`
- verify: `superpowers:verification-before-completion`
- finish: `superpowers:finishing-a-development-branch`
- situational, when something breaks: `superpowers:systematic-debugging`
- situational, notes-table fan-out: `superpowers:dispatching-parallel-agents`
- situational, repeatable cycle: `loopy`
