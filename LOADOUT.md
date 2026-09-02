# Loadout: loadout
Harness: claude-code | Project type: CLI/library (stdlib Python scanner + portable SKILL.md), maintenance + refinement, cross-harness portability is the special need
Date: 2026-09-02

## Recommended workflow
1. Planning → `planning-with-files` — the project already keeps task_plan.md / findings.md / progress.md; keep using them (they are stale, see Findings)
2. Skill authoring → `superpowers:writing-skills` — every SKILL.md edit is a skill edit; test the prose with a subagent before shipping
3. Debugging → `superpowers:systematic-debugging` — one real defect found (LOADOUT_HOST override), root-cause it, do not patch brief mode
4. Implementation → `superpowers:test-driven-development` — 22 pytest tests on a 3.9/3.11/3.13 × ubuntu/windows CI matrix; failing test first
5. Review → `code-review` — Standards + Spec axes since a fixed commit; git + origin present so it is not blocked
6. Finish → `superpowers:verification-before-completion` then `superpowers:finishing-a-development-branch` — run `--check`, tests, and re-sync copies with `--self-install` before calling a version done

## Situational (invoke when relevant)
- `unlazy` — for a multi-step version bump; this repo already uses its GATES.md pattern
- `ponytail:ponytail-review` — on the diff before review; ponytail is already always-on via SessionStart hook
- `loopy` — if the re-audit + re-sync cycle becomes a repeatable loop
- `loadout` — re-run on itself after each version (dogfood); this file then gets a Supersedes line

## Skip / noise for this project
- `plan-blueprint-tdd`, `lazy-planner`, `superpowers:brainstorming` — greenfield planners; this is v1.2.2 maintenance
- `write-a-skill`, `anthropic-skills:skill-creator` — redundant with superpowers:writing-skills; pick one
- `full-output-enforcement` — conflicts with the always-on ponytail hook (minimal output vs exhaustive)
- all `*-delegate` skills, `superpowers:dispatching-parallel-agents`, `superpowers:subagent-driven-development` — 900-line project, nothing to parallelise
- all frontend/design skills (design-taste-*, gpt-taste, brandkit, imagegen-*, figma:*, frontend-design, minimalist-ui, industrial-brutalist-ui, redesign-existing-projects, high-end-visual-design, image-to-code) — no UI
- `agent-reach`, `research`, `context7-mcp` — stdlib only, no external docs needed
- mantis-* — security-audit pipeline; the scanner reads names/frontmatter only, no trust boundary worth a campaign
- `commit-commands:*`, `feature-dev:*`, `claude-md-management:*` — off in this host

## Blocked
- none (git repository present, origin/main in sync, Python available)

## Gaps
- none by category. Process gap only: no changelog; version history lives in git subjects and a stale progress.md

## Findings (defects and improvements in the skill itself, from this audit)
1. DEFECT `LOADOUT_HOST` is used verbatim, never normalised to a host key. `LOADOUT_HOST=claude` (the obvious value; SKILL.md says only `<host>`) makes `--brief` drop the current-host section entirely and makes the full run render claude-code as a foreign host (names only, `…+78`). Fix in detect_host: map aliases (claude→claude-code, dsh→deepseek, …), warn and fall back to `unknown` on an unrecognised value; document the valid names in SKILL.md and README.
2. `--brief` is not brief: 118 skills still print 118 lines at 200 chars each. SKILL.md tells the model to use it above ~50 skills, so it should drop to name + first sentence.
3. Two native-file tables (scan.py NATIVE_FILES, apply.py NATIVE) can drift; apply.py sits next to scan.py and can import it.
4. GATES.md G10 greps for `deepseek-harness`, a host name that no longer exists (now `deepseek`); the gate would fail today. task_plan.md ended at "Done" before v1.1.0 and progress.md at v1.1.0 while the repo was at v1.2.2. They are gitignored (local notes only), so this is a bookkeeping gap, not a public one. Updated in the v1.3.0 pass.
5. Non-current hosts list hooks as bare event names (`SessionStart, SessionStart, …`); the current-host rendering (matcher + command + source) is good and could be reused with a shorter form.

## Accepted
- planning: `planning-with-files`
- skill authoring: `superpowers:writing-skills`
- debugging: `superpowers:systematic-debugging`
- implementation: `superpowers:test-driven-development`
- review: `code-review`
- verify: `superpowers:verification-before-completion`
- finish: `superpowers:finishing-a-development-branch`
- situational, gated multi-step work: `unlazy`
- situational, diff review: `ponytail:ponytail-review`
- situational, repeatable cycle: `loopy`
- situational, self re-audit: `loadout`
