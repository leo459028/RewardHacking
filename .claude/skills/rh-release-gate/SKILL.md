---
name: rh-release-gate
description: Run the RewardHacking Stage 2 release gate (version anchors, secret scan, ruff, lint-imports, diff check, full pytest, Docker build and Alembic migration, browser E2E) and prepare the test report entry. Use before a release commit, or when asked to run Stage 2, the release gate or the full verification.
---

# Release gate (Stage 2)

Authority: `AGENTS.md` §3 Stage 2 and §5 (release commit only after every step passes).

1. Ask the user to start Docker Desktop if it is not running. Make sure no local LLM inference is running (CPU contention slows the suite).
2. Run the gate in the background (it takes several minutes) and write the summary outside the repo:

   ```bash
   python scripts/release_gate.py --out <scratchpad>/release_gate.md
   python scripts/release_gate.py --skip-docker --skip-e2e --out ...   # automated suite only
   python scripts/release_gate.py --list                               # show the steps
   ```

   It stops at the first failure (`--keep-going` runs the rest) and returns non-zero when any step failed.
3. Read the summary. On failure, fix and rerun; never record or commit a failing gate.
4. Prepend a new entry to the top of `docs/test_report.md` from the summary plus the live IDs printed by the browser E2E (run / review / benchmark IDs). Record only what actually ran.
5. Before pushing, run the suite once in a fresh venv built from `requirements.txt` + `requirements-otel.txt` (CI resolves newer versions than the local environment; see `docs/testing_guide.md` § CI / CD Verification Gate).
6. Then the release commit (`feat(vX.Y.Z): <title>`), using `rh-phase-commit` step 3 with the `feat(...)` message.

Notes: the browser E2E runs against the persistent local stack, so dedup / cap responses on same-day reruns are expected (see `docs/manual_test.md`). The script never edits `docs/test_report.md` itself.
