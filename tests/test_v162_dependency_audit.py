"""v1.6.2 P5: the pull-request dependency audit (no network: pip-audit itself is not run here)."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from scripts import dependency_audit as audit

ROOT = Path(__file__).resolve().parents[1]

TODAY = date(2026, 11, 19)
VALID = {"id": "GHSA-aaaa-bbbb-cccc", "package": "example", "reason": "no fixed release yet", "expires": "2026-12-31"}


def _ci() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]


def test_a_valid_exception_is_ignored_until_it_expires():
    result = audit.load_exceptions({"exceptions": [VALID]}, TODAY)
    assert result.ids == ("GHSA-aaaa-bbbb-cccc",) and result.problems == ()


def test_an_expired_exception_fails():
    result = audit.load_exceptions({"exceptions": [{**VALID, "expires": "2026-11-18"}]}, TODAY)
    assert result.ids == () and "expired on 2026-11-18" in result.problems[0]


@pytest.mark.parametrize("field", ["id", "package", "reason", "expires"])
def test_an_exception_without_every_field_fails(field):
    result = audit.load_exceptions({"exceptions": [{**VALID, field: " "}]}, TODAY)
    assert result.ids == () and field in result.problems[0]


def test_an_exception_with_a_bad_date_fails():
    result = audit.load_exceptions({"exceptions": [{**VALID, "expires": "next year"}]}, TODAY)
    assert result.ids == () and "YYYY-MM-DD" in result.problems[0]


def test_the_command_is_strict_and_passes_only_accepted_ids():
    command = audit.audit_command(("requirements.txt",), ("GHSA-aaaa-bbbb-cccc",))
    assert command[1:4] == ["-m", "pip_audit", "--strict"]
    assert command[command.index("--ignore-vuln") + 1] == "GHSA-aaaa-bbbb-cccc" and command.count("--ignore-vuln") == 1


def test_both_images_and_every_runtime_requirement_are_audited():
    audited = {name for _, files in audit.DEPENDENCY_SETS for name in files}
    assert audited == {"requirements.txt", "requirements-otel.txt", "requirements-sandbox.txt", "docker/sandbox-python/requirements.txt"}


def test_the_exceptions_file_starts_empty_and_is_valid():
    data = json.loads(audit.EXCEPTIONS_FILE.read_text(encoding="utf-8"))
    assert audit.load_exceptions(data, date.today()).problems == ()


def test_the_sandbox_image_installs_its_audited_pins():
    pins = (ROOT / "docker" / "sandbox-python" / "requirements.txt").read_text(encoding="utf-8")
    assert re.findall(r"^([a-z0-9-]+)==[\d.]+$", pins, re.M) == ["pytest", "coverage"]
    dockerfile = (ROOT / "docker" / "sandbox-python" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY requirements.txt" in dockerfile and "-r /tmp/sandbox-requirements.txt" in dockerfile
    assert not re.search(r"pip install[^\n]*\b(pytest|coverage)==", dockerfile)


def test_ci_blocks_on_python_advisories_and_only_reports_os_packages():
    jobs = _ci()
    steps = jobs["dependencies"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install pip-audit")
    assert re.fullmatch(r"pip install pip-audit==\d+\.\d+\.\d+", install["run"])
    run = next(step for step in steps if step.get("name") == "Audit Python dependencies")
    assert run["run"] == "python scripts/dependency_audit.py" and "continue-on-error" not in run
    report = next(step for step in jobs["image"]["steps"] if "scan-action" in step.get("uses", ""))
    assert report["continue-on-error"] is True and report["with"]["fail-build"] is False
