"""Git pre-commit check, independent of any coding tool (v1.6.2 D51).

Refuses a commit that stages local working files, environment files, private keys or the local judge profiles, and runs
``git diff --cached --check``. Enable once per clone with ``git config core.hooksPath scripts/git-hooks``.
"""

from __future__ import annotations

import re
import subprocess
import sys

FORBIDDEN_PREFIXES = ("docs/plan/",)
FORBIDDEN_FILES = ("docs/handoff.md", "config/judge_profiles.yaml")
ALLOWED_ENV_FILES = (".env.example",)
_ENV_RE = re.compile(r"(^|/)\.env(\.[^/]+)?$")


def is_forbidden(path: str) -> bool:
    path = path.replace("\\", "/")
    if path in ALLOWED_ENV_FILES:
        return False
    return (
        path.startswith(FORBIDDEN_PREFIXES)
        or path in FORBIDDEN_FILES
        or path.endswith(".pem")
        or _ENV_RE.search(path) is not None
    )


def staged_paths() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return [name for name in result.stdout.split("\0") if name]


def main() -> int:
    forbidden = [path for path in staged_paths() if is_forbidden(path)]
    if forbidden:
        print("pre-commit: these files must never be committed:", file=sys.stderr)
        for path in forbidden:
            print(f"  {path}", file=sys.stderr)
        print("Unstage them with: git restore --staged <file>", file=sys.stderr)
        return 1
    check = subprocess.run(["git", "diff", "--cached", "--check"])
    if check.returncode != 0:
        print("pre-commit: fix the whitespace problems listed above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
