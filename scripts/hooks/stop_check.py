"""Claude Code Stop hook: quick checks before a turn ends with Python changes in the tree (v1.6.2 D50).

Runs ruff on the changed Python files, ``git diff --check`` and, when ``app/`` changed, the import contracts. Exit 2
keeps the model working and shows it the output.
The full test suite is left to Stage 2 and CI. When ``stop_hook_active`` is set the hook has already asked once, so it
lets the turn end, and a check that runs out of time passes with a note on stderr.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINTED_DIRS = ("app/", "tests/", "scripts/")
TIME_BUDGET_SECONDS = 30
BLOCK = 2


class OutOfTime(Exception):
    pass


def _run(cmd: list[str], root: Path, deadline: float) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OutOfTime
    try:
        return subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=remaining
        )
    except subprocess.TimeoutExpired as exc:
        raise OutOfTime from exc


def changed_python_files(root: Path, deadline: float) -> list[str]:
    tracked = _run(["git", "diff", "--name-only", "--diff-filter=d", "-z", "HEAD"], root, deadline)
    untracked = _run(["git", "ls-files", "--others", "--exclude-standard", "-z"], root, deadline)
    names = (tracked.stdout + untracked.stdout).split("\0")
    return sorted({name for name in names if name.endswith(".py") and name.startswith(LINTED_DIRS)})


def run_checks(root: Path, deadline: float) -> list[str]:
    files = changed_python_files(root, deadline)
    if not files:
        return []
    problems = []
    ruff = _run(["ruff", "check", "--quiet", *files], root, deadline)
    if ruff.returncode != 0:
        problems.append("ruff check:\n" + (ruff.stdout + ruff.stderr).strip())
    diff = _run(["git", "diff", "--check"], root, deadline)
    if diff.returncode != 0:
        problems.append("git diff --check:\n" + (diff.stdout + diff.stderr).strip())
    if (root / "pyproject.toml").is_file() and any(name.startswith("app/") for name in files):
        contracts = _run(["lint-imports"], root, deadline)
        if contracts.returncode != 0:
            problems.append("lint-imports:\n" + (contracts.stdout + contracts.stderr).strip())
    return problems


def main(stdin_text: str, root: Path = ROOT) -> int:
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and payload.get("stop_hook_active"):
        return 0
    try:
        problems = run_checks(root, time.monotonic() + TIME_BUDGET_SECONDS)
    except OutOfTime:
        print(f"stop_check: checks exceeded {TIME_BUDGET_SECONDS} s, skipped", file=sys.stderr)
        return 0
    except OSError as exc:
        print(f"stop_check: a tool is not available ({exc}), skipped", file=sys.stderr)
        return 0
    if not problems:
        return 0
    print("\n\n".join(problems), file=sys.stderr)
    return BLOCK


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.read()))
