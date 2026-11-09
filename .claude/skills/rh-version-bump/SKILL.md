---
name: rh-version-bump
description: Bump or verify the RewardHacking release version anchors (pyproject, FastAPI app, security scan banner, Operations default, browser E2E assertions, FINOPS_CALCULATION_VERSION). Use when preparing a release version (vX.Y.Z) or when a test or the release gate reports disagreeing version anchors.
---

# Version bump

Authority: `AGENTS.md` §4.1 lists the six anchors.

```bash
python scripts/bump_version.py --check            # all anchors agree?
python scripts/bump_version.py 1.6.1 --dry-run    # which files would change
python scripts/bump_version.py 1.6.1              # rewrite and re-check
```

- The version is `X.Y.Z` without a leading `v`; the script writes the `v` where an anchor needs it.
- Each anchor is an exact regex with an expected match count. A missing or duplicated anchor is an error, not a guess: fix the file (or the anchor list in `scripts/bump_version.py` when an anchor legitimately moved) and rerun.
- After bumping, the version-anchor tests of earlier releases are written as "series" checks; run `python scripts/phase_check.py --full` or at least the latest release test file.
- Bumping is part of the release phase (usually the last one); release notes and docs follow `AGENTS.md` §4.2.
