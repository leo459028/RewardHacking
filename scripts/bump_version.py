"""Bump or check the release version anchors listed in AGENTS.md section 4.1 (v1.6.0 D30).

Usage:
    python scripts/bump_version.py --check          # all anchors agree? (exit 1 and list them if not)
    python scripts/bump_version.py 1.6.0            # rewrite every anchor to 1.6.0
    python scripts/bump_version.py 1.6.0 --dry-run  # show what would change

Each anchor is an exact regex with an expected match count. A missing or duplicated match is an error:
the script never guesses. Pass ``--root`` to operate on a copy of the repository (used by the tests).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_V = r"(\d+\.\d+\.\d+)"


@dataclass(frozen=True)
class Anchor:
    number: int
    path: str
    pattern: str
    count: int = 1

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.M)


# Numbers follow AGENTS.md 4.1. Anchor 5 also covers the E2E lines that embed the version in page content.
ANCHORS: tuple[Anchor, ...] = (
    Anchor(1, "pyproject.toml", rf'^version = "{_V}"$'),
    Anchor(2, "app/main.py", rf'^    version="{_V}",$'),
    Anchor(3, "scripts/security_check.py", rf"Security & Secret Scan \(v{_V}\)"),
    Anchor(4, "app/ui/routers/operations.py", rf'form\.get\("release_version", "v{_V}"\)'),
    Anchor(4, "app/ui/templates/operations.html", rf'<input name="release_version" value="v{_V}">'),
    Anchor(5, "scripts/browser_manual_test.py", rf'assert release_ver == "v{_V}"'),
    Anchor(5, "scripts/browser_manual_test.py", rf'manual_test\.md suite for v{_V}"'),
    Anchor(5, "scripts/browser_manual_test.py", rf'assert "v{_V}" in page\.content\(\)'),
    Anchor(5, "scripts/browser_manual_test.py", rf"& v{_V} Release Gate"),
    Anchor(6, "app/services/finops/core.py", rf'^FINOPS_CALCULATION_VERSION = "v{_V}"$'),
)


def scan(root: Path) -> tuple[list[tuple[Anchor, str]], list[str]]:
    """Return ``(found, errors)`` where ``found`` pairs each anchor with its current version."""
    found: list[tuple[Anchor, str]] = []
    errors: list[str] = []
    for anchor in ANCHORS:
        file = root / anchor.path
        if not file.exists():
            errors.append(f"[{anchor.number}] {anchor.path}: file not found")
            continue
        matches = anchor.regex.findall(file.read_text(encoding="utf-8"))
        if len(matches) != anchor.count:
            errors.append(f"[{anchor.number}] {anchor.path}: expected {anchor.count} match(es) of {anchor.pattern!r}, found {len(matches)}")
            continue
        found.extend((anchor, value) for value in matches)
    return found, errors


def check(root: Path) -> int:
    found, errors = scan(root)
    versions = sorted({value for _, value in found})
    for anchor, value in found:
        print(f"[{anchor.number}] {anchor.path}: {value}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    if len(versions) != 1:
        print(f"Version anchors disagree: {', '.join(versions)}", file=sys.stderr)
        return 1
    print(f"OK: all {len(found)} anchors are {versions[0]}")
    return 0


def bump(root: Path, new_version: str, *, dry_run: bool = False) -> int:
    if not VERSION_RE.match(new_version):
        print(f"Invalid version {new_version!r}; expected X.Y.Z without a leading 'v'", file=sys.stderr)
        return 2
    _, errors = scan(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    contents: dict[str, str] = {}
    for anchor in ANCHORS:
        text = contents.get(anchor.path) or (root / anchor.path).read_text(encoding="utf-8")

        def _replace(match: re.Match[str]) -> str:
            whole = match.group(0)
            start, end = match.span(1)
            offset = match.start(0)
            return whole[: start - offset] + new_version + whole[end - offset :]

        contents[anchor.path] = anchor.regex.sub(_replace, text)
    for path, text in contents.items():
        original = (root / path).read_text(encoding="utf-8")
        if original == text:
            continue
        print(f"{'would update' if dry_run else 'updated'} {path}")
        if not dry_run:
            (root / path).write_text(text, encoding="utf-8", newline="\n")
    return 0 if dry_run else check(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", nargs="?", help="new version X.Y.Z")
    parser.add_argument("--check", action="store_true", help="only verify that all anchors agree")
    parser.add_argument("--dry-run", action="store_true", help="show the files that would change")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="repository root")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.check or not args.version:
        return check(root)
    return bump(root, args.version, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
