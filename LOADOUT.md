# Loadout: loadout
Harness: claude-code | Project type: CLI/library (stdlib Python scanner + portable SKILL.md), maintenance + refinement, cross-harness portability is the special need
Date: 2026-09-03
Enforcement: claude-code gate registered
Supersedes: loadout of 2026-09-02

## What changed since 2026-09-02
- v1.2.2 -> v1.5.x: Codex and DeepSeek Harness adapters merged, 22 -> 115 tests. Project type and needs unchanged; workflow carried over.
- First task: skill-notes reference. Loadout classifies from frontmatter descriptions only (scan.py MAX_DESC, SKILL.md step 3); thin or empty descriptions (e.g. `devteam` prints none) give it nothing to classify by. Fix: step-3 rule change (read the body when the description is thin, prefer a notes row when one exists), generated `references/skill-notes.md`, add it to SKILL_FILES for self-install, re-sync.

## Recommended workflow
1. Planning → `planning-with-files` — task_plan.md / progress.md exist and are stale again; refresh with this task
2. Skill authoring → `superpowers:writing-skills` — the step-3 edit and the reference file are skill edits; test the prose with a subagent
3. Debugging → `superpowers:systematic-debugging` — only if the rule change misbehaves
4. Implementation → `superpowers:test-driven-development` — one failing test: `--self-install` copies references/skill-notes.md and `--check` flags it stale
5. Review → `code-review` — Standards + Spec since db2a5da
6. Finish → `superpowers:verification-before-completion` then `superpowers:finishing-a-development-branch` — tests, `--check`, `--self-install`

## Situational (invoke when relevant)
- `superpowers:dispatching-parallel-agents` — fan out subagents over the ~119 skill dirs to generate notes rows
- `unlazy` — multi-step gated work; the repo already uses GATES.md
- `loopy` — if the re-audit + re-sync cycle becomes a repeatable loop
- `loadout` — re-run on itself after each version (dogfood)

## Skip / noise for this project
- `plan-blueprint-tdd`, `lazy-planner`, `superpowers:brainstorming` — greenfield planners; this is maintenance
- `write-a-skill`, `anthropic-skills:skill-creator` — redundant with superpowers:writing-skills
- `full-output-enforcement` — conflicts with the always-on ponytail hook
- `ponytail:ponytail-review` — dropped this audit; ponytail is already always-on via SessionStart
- all `*-delegate` skills, `superpowers:subagent-driven-development` — small repo, only the notes generation parallelises and dispatching-parallel-agents covers it
- all frontend/design skills (design-taste-*, gpt-taste, brandkit, imagegen-*, figma:*, frontend-design, minimalist-ui, industrial-brutalist-ui, redesign-existing-projects, high-end-visual-design, image-to-code) — no UI
- `agent-reach`, `research`, `context7-mcp` — stdlib only; upstream lookups for skill-notes are a grep for github URLs in local files, not research
- mantis-* — no trust boundary worth a campaign
- `commit-commands:*`, `feature-dev:*`, `claude-md-management:*` — off in this host

## Blocked
- none (git repository present, origin/main in sync, Python available, 115 tests green)

## Gaps
- none by category. Process gap carried over: no changelog

## Findings (carried forward from the 2026-09-02 audit, with current status)
1. FIXED `LOADOUT_HOST` was used verbatim, never normalised to a host key. `HOST_ALIASES` now maps claude -> claude-code, dsh -> deepseek and falls back to `unknown` (scan.py:102, applied at :400).
2. FIXED `--brief` printed every description in full. It now emits the first sentence only (scan.py:659).
3. OPEN Two native-file tables can drift: `NATIVE_FILES` (scan.py:71) and `NATIVE` (apply.py:32) hold the same map. apply.py sits next to scan.py and can import it.
4. FIXED GATES.md G10 grepped for the retired host name `deepseek-harness`; it now checks that `~/.agents` appears as a host.
5. FIXED Non-current hosts listed hooks as bare event names; `test_foreign_host_hooks_are_collapsed_with_counts` covers the collapsed rendering.

## Findings (new, this audit)
6. FIXED (this change) Step 3 classified from the frontmatter description alone. Across 162 installed skills the shortest description is 14 characters and 55 are under 80, so a skill could not describe its own value. Step 3 now reads the body when a description is thin and prefers a `references/skill-notes.md` row when one exists.
7. OPEN A plugin bundle installed under `skills/` (for example `devteam`) has no top-level SKILL.md, so the scanner lists a bare name with no description at all. The notes table carries a row for it; the scanner could instead recurse one level into `<bundle>/skills/`.

## Accepted
- planning: `planning-with-files`
- skill authoring: `superpowers:writing-skills`
- debugging: `superpowers:systematic-debugging`
- implementation: `superpowers:test-driven-development`
- review: `code-review`
- verify: `superpowers:verification-before-completion`
- finish: `superpowers:finishing-a-development-branch`
- situational, notes-table fan-out: `superpowers:dispatching-parallel-agents`
- situational, gated multi-step work: `unlazy`
- situational, repeatable cycle: `loopy`
- situational, self re-audit: `loadout`
