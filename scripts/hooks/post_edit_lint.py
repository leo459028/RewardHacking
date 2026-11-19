"""Claude Code PostToolUse hook: lint a Python file right after it is edited (v1.6.2 D50).

Reads the hook JSON from stdin. Exit 2 shows the ruff output to the model (the edit has already happened, so the model
fixes it in its next step). Anything the hook cannot check passes: a broken hook must not stop the session.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINTED_DIRS = ("app", "tests", "scripts")
TIMEOUT_SECONDS = 30
BLOCK = 2


def lint_target(file_path: str, root: Path = ROOT) -> Path | None:
    """Return the file to lint, or None when it is not a Python file under the linted directories."""
    if not file_path:
        return None
    path = Path(file_path)
    path = path if path.is_absolute() else root / path
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if path.suffix != ".py" or relative.parts[0] not in LINTED_DIRS or not path.is_file():
        return None
    return path


def main(stdin_text: str, root: Path = ROOT) -> int:
    try:
        payload = json.loads(stdin_text or "{}")
        file_path = str((payload.get("tool_input") or {}).get("file_path") or "")
    except (ValueError, AttributeError):
        return 0
    target = lint_target(file_path, root)
    if target is None:
        return 0
    try:
        result = subprocess.run(
            ["ruff", "check", "--quiet", str(target)],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"post_edit_lint: ruff exceeded {TIMEOUT_SECONDS} s, skipped", file=sys.stderr)
        return 0
    except OSError as exc:
        print(f"post_edit_lint: ruff is not available ({exc}), skipped", file=sys.stderr)
        return 0
    if result.returncode == 0:
        return 0
    print((result.stdout + result.stderr).strip(), file=sys.stderr)
    return BLOCK


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.read()))
