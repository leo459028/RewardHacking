"""Stage 1 checks and a guarded phase commit (v1.6.0 D31; AGENTS.md sections 3 and 5).

Usage:
    python scripts/phase_check.py tests/test_v160_x.py [more test files]
    python scripts/phase_check.py --full                      # full pytest instead of targeted files
    python scripts/phase_check.py tests/test_v160_x.py \\
        --commit "wip(v1.6.0): P3 retention service" --paths app/services/retention.py tests/test_v160_retention.py \\
        --trailer "Co-Authored-By: Name <email>"

Checks run in order and stop at the first failure: ruff, lint-imports, git diff --check, pytest
(-W error::DeprecationWarning). ``--commit`` stages only the listed paths after every check passed, refuses
git-ignored working files (docs/plan/, docs/handoff.md, .env), prints ``git diff --cached --stat`` and commits only
when staging succeeded. It never pushes, resets or amends.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGE_RE = re.compile(r"^(wip\(v\d+\.\d+\.\d+\): P\d+[a-z]?([.-]\d+[a-z]?)? \S.*|feat\(v\d+\.\d+\.\d+\): \S.*)$")
FORBIDDEN_PREFIXES = ("docs/plan/",)
FORBIDDEN_FILES = ("docs/handoff.md",)
_ENV_RE = re.compile(r"(^|/)\.env(\..+)?$")


def _run(cmd: list[str], cwd: Path) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=cwd).returncode


def check_steps(test_files: list[str], full: bool) -> list[list[str]]:
    pytest_cmd = [sys.executable, "-m", "pytest", "-q", "-W", "error::DeprecationWarning"]
    if not full:
        pytest_cmd += test_files
    return [
        ["ruff", "check", "app", "tests", "scripts"],
        ["lint-imports"],
        ["git", "diff", "--check"],
        pytest_cmd,
    ]


def run_checks(test_files: list[str], full: bool, cwd: Path = ROOT) -> int:
    if not full and not test_files:
        print("Pass the targeted test files, or --full.", file=sys.stderr)
        return 2
    for step in check_steps(test_files, full):
        code = _run(step, cwd)
        if code != 0:
            print(f"FAILED: {' '.join(step)} (exit {code})", file=sys.stderr)
            return code
    print("Stage 1 checks passed.")
    return 0


def _normalize(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def forbidden_paths(paths: list[str]) -> list[str]:
    bad = []
    for raw in paths:
        path = _normalize(raw)
        if path.startswith(FORBIDDEN_PREFIXES) or path in FORBIDDEN_FILES or _ENV_RE.search(path):
            if path != ".env.example":
                bad.append(raw)
    return bad


def validate_message(message: str) -> str | None:
    first = message.strip().splitlines()[0] if message.strip() else ""
    if not MESSAGE_RE.match(first):
        return "Commit message must start with 'wip(vX.Y.Z): P<n> <summary>' or 'feat(vX.Y.Z): <title>'."
    return None


def guarded_commit(message: str, paths: list[str], trailer: str = "", cwd: Path = ROOT) -> int:
    error = validate_message(message)
    if error:
        print(error, file=sys.stderr)
        return 2
    if not paths:
        print("--paths is required with --commit (stage explicit files only).", file=sys.stderr)
        return 2
    bad = forbidden_paths(paths)
    if bad:
        print(f"Refusing to stage git-ignored working files: {', '.join(bad)}", file=sys.stderr)
        return 2
    if _run(["git", "add", "--", *paths], cwd) != 0:
        print("git add failed; nothing committed.", file=sys.stderr)
        return 1
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=cwd, capture_output=True, text=True)
    staged_files = [line for line in staged.stdout.splitlines() if line]
    bad = forbidden_paths(staged_files)
    if bad:
        print(f"Forbidden files are staged: {', '.join(bad)}; unstage them first. Nothing committed.", file=sys.stderr)
        return 2
    if not staged_files:
        print("Nothing staged; nothing committed.", file=sys.stderr)
        return 1
    _run(["git", "diff", "--cached", "--stat"], cwd)
    body = message.strip() + ("\n\n" + trailer.strip() if trailer.strip() else "") + "\n"
    result = subprocess.run(["git", "commit", "-q", "-F", "-"], cwd=cwd, input=body, text=True)
    if result.returncode != 0:
        print("git commit failed.", file=sys.stderr)
        return result.returncode
    _run(["git", "log", "--oneline", "-1"], cwd)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tests", nargs="*", help="targeted test files")
    parser.add_argument("--full", action="store_true", help="run the full pytest suite")
    parser.add_argument("--commit", metavar="MESSAGE", help="commit after all checks pass")
    parser.add_argument("--paths", nargs="+", default=[], help="files to stage for --commit")
    parser.add_argument("--trailer", default="", help="attribution line appended to the commit message")
    args = parser.parse_args(argv)
    code = run_checks(args.tests, args.full)
    if code != 0 or not args.commit:
        return code
    return guarded_commit(args.commit, args.paths, args.trailer)


if __name__ == "__main__":
    raise SystemExit(main())
