---
name: rh-plan-new-version
description: Create the local version plan (docs/plan/vX_Y_Zplan.md, Traditional Chinese, git-ignored) for the next RewardHacking release from the bundled template, with every section AGENTS.md requires. Use when the user asks to plan or write the plan for a new version (for example v1.6.1).
---

# New version plan

Authority: `AGENTS.md` §1 (mandatory sections) and §4.2 (language: plans in Traditional Chinese, never staged).

1. Read `docs/handoff.md`, then the target release in `docs/roadmap.md` and the previous plan's §1.3 (scope boundary) and §8 (known limits) only. Do not read the whole previous plan for its format: the format is `template.md` next to this file.
2. Verify the facts the plan relies on in the code (cite `file:line` in §1.2), so design decisions are grounded.
3. Copy `template.md` to `docs/plan/vX_Y_Zplan.md` (digits follow the version: `v1_6_1plan.md`) and fill every section. Keep decisions numbered (`D1`...) and actionable; each phase in §9 must end in a consistent, testable state.
4. Discuss open decisions with the user before implementation; record their answers in §1.1 and the decision table.
5. Update `docs/handoff.md` (next step: wait for plan confirmation). Never stage the plan or the handoff.
