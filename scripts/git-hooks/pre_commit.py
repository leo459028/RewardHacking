"""Git pre-commit check, independent of any coding tool (v1.6.2 D51).

Refuses a commit that stages local working files, environment files, private keys, the local judge profiles or a
developer's home directory path, and runs ``git diff --cached --check``. Enable once per clone with ``git config core.hooksPath scripts/git-hooks``.
"""

from __future__ import annotations

import re
import subprocess
import sys

FORBIDDEN_PREFIXES = ("docs/plan/",)
FORBIDDEN_FILES = ("docs/handoff.md", "config/judge_profiles.yaml")
ALLOWED_ENV_FILES = (".env.example",)
KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
_ENV_RE = re.compile(r"(^|/)\.env(\.[^/]+)?$")
_KEY_NAME_RE = re.compile(r"(^|/)id_rsa[^/]*$")
_HOME_RE = re.compile(r"\b[A-Za-z]:[\\/]+Users[\\/]+(?!me\b)[A-Za-z0-9._-]+")


def is_forbidden(path: str) -> bool:
    path = path.replace("\\", "/")
    if path in ALLOWED_ENV_FILES:
        return False
    return (
        path.startswith(FORBIDDEN_PREFIXES)
        or path in FORBIDDEN_FILES
        or path.lower().endswith(KEY_SUFFIXES)
        or _KEY_NAME_RE.search(path) is not None
        or _ENV_RE.search(path) is not None
    )


def names_local_home(diff: str) -> bool:
    """True when an added line holds a Windows home directory: the repository is public."""
    return any(line.startswith("+") and not line.startswith("+++") and _HOME_RE.search(line) for line in diff.splitlines())


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
    staged = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    if names_local_home(staged.stdout):
        print("pre-commit: an added line names a local home directory; replace it with a placeholder.", file=sys.stderr)
        return 1
    check = subprocess.run(["git", "diff", "--cached", "--check"])
    if check.returncode != 0:
        print("pre-commit: fix the whitespace problems listed above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
