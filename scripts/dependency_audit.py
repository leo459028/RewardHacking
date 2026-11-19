"""Audit the Python dependencies of the application and the sandbox image for known vulnerabilities (v1.6.2 P5).

Usage:
    python scripts/dependency_audit.py            # needs pip-audit and network access to resolve the requirements

Runs ``pip-audit --strict`` once per dependency set, so a package that cannot be audited also fails. A known
vulnerability fails the run unless ``scripts/dependency_audit_exceptions.json`` accepts it with a reason and an expiry
date; an expired or incomplete exception fails as well, so nothing is accepted forever. Operating-system packages are
not covered here: the CI ``image`` job reports them without blocking.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS_FILE = ROOT / "scripts" / "dependency_audit_exceptions.json"
DEPENDENCY_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("application image", ("requirements.txt", "requirements-otel.txt", "requirements-sandbox.txt")),
    ("sandbox image", ("docker/sandbox-python/requirements.txt",)),
)
REQUIRED_FIELDS = ("id", "package", "reason", "expires")


@dataclass(frozen=True)
class Exceptions:
    ids: tuple[str, ...]
    problems: tuple[str, ...]


def load_exceptions(data: dict, today: date) -> Exceptions:
    """Valid, unexpired exception IDs, and a problem line for every entry that is incomplete or expired."""
    ids: list[str] = []
    problems: list[str] = []
    for index, entry in enumerate(data.get("exceptions") or []):
        missing = [field for field in REQUIRED_FIELDS if not str((entry or {}).get(field) or "").strip()]
        if missing:
            problems.append(f"exception {index}: missing {', '.join(missing)}")
            continue
        try:
            expires = date.fromisoformat(str(entry["expires"]))
        except ValueError:
            problems.append(f"exception {entry['id']}: expires is not YYYY-MM-DD")
            continue
        if expires < today:
            problems.append(f"exception {entry['id']} ({entry['package']}) expired on {expires.isoformat()}")
            continue
        ids.append(str(entry["id"]))
    return Exceptions(tuple(ids), tuple(problems))


def audit_command(files: tuple[str, ...], ignored: tuple[str, ...]) -> list[str]:
    command = [sys.executable, "-m", "pip_audit", "--strict", "--progress-spinner", "off"]
    for name in files:
        command += ["--requirement", str(ROOT / name)]
    for vuln_id in ignored:
        command += ["--ignore-vuln", vuln_id]
    return command


def main() -> int:
    exceptions = load_exceptions(json.loads(EXCEPTIONS_FILE.read_text(encoding="utf-8")), date.today())
    for problem in exceptions.problems:
        print(f"[FAIL] {problem}")
    failed = bool(exceptions.problems)
    for label, files in DEPENDENCY_SETS:
        print(f"==> {label}: {', '.join(files)}", flush=True)
        if subprocess.run(audit_command(files, exceptions.ids), cwd=ROOT).returncode != 0:
            print(f"[FAIL] {label}: known vulnerabilities or packages that could not be audited")
            failed = True
    if exceptions.ids:
        print(f"Accepted until their expiry: {', '.join(exceptions.ids)}")
    print("Dependency audit failed." if failed else "Dependency audit passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
