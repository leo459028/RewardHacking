---
name: rh-phase-commit
description: Run RewardHacking Stage 1 checks and make a guarded phase commit on a release branch. Use when a release phase (P1, P2a, ...) is finished and its progress log entry is written, or when asked to run Stage 1 / "phase check" / commit a phase.
---

# Phase check and commit

Authority: `AGENTS.md` §3 (Stage 1) and §5 (phase commits). This skill only shows how to run them.

1. Make sure the plan's Implementation Progress Log (`docs/plan/vX_Y_Zplan.md` §9) has this phase's files, Stage 1 result and next step. Never stage that file.
2. Run the checks (ruff, `lint-imports`, `git diff --check`, targeted pytest with `-W error::DeprecationWarning`):

   ```bash
   python scripts/phase_check.py tests/test_v16x_<feature>.py
   ```

   Use `--full` instead of test files for refactors that can break anything (the whole suite takes about 4 minutes; run it in the background).
3. Commit only the listed files after the checks pass:

   ```bash
   python scripts/phase_check.py tests/test_v16x_<feature>.py \
     --commit "wip(vX.Y.Z): P<n> <phase summary>" \
     --paths <file> <file> ... \
     --trailer "<attribution line from the session's git instructions>"
   ```

   The script refuses `docs/plan/`, `docs/handoff.md` and `.env*` (except `.env.example`), refuses a malformed message, stops when `git add` fails and prints `git diff --cached --stat` before committing. It never pushes, resets or amends.
4. Update `docs/handoff.md` (current commit, next phase). Do not stage it.

If a check fails, fix the cause and rerun; never commit with a failing or skipped check.
