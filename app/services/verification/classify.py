"""Compare the platform's before and after test reports (v1.6.2 D26).

The test ID is ``classname::name``. A test whose result differs between the runs of the same change is ``flaky`` and is
left out of every other class. Reports come from inside the sandbox, so they are parsed as untrusted input.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Mapping, Sequence

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
CLASSES = ("fail_to_pass", "pass_to_pass", "pass_to_fail", "fail_to_fail", "new_in_after", "missing_in_after", "skipped", "flaky")
MAX_TESTS = 20000
MAX_LISTED = 200
_UNSAFE_XML = re.compile(rb"<!\s*(doctype|entity)", re.IGNORECASE)


class ReportInvalid(ValueError):
    pass


def parse_junit(content: bytes) -> dict[str, str]:
    """``{test id: passed | failed | skipped}``; an error counts as a failure."""
    if _UNSAFE_XML.search(content):
        raise ReportInvalid("unsafe_xml")
    try:
        root = ET.fromstring(content.decode("utf-8"))
    except (ET.ParseError, UnicodeDecodeError) as exc:
        raise ReportInvalid("invalid_xml") from exc
    results: dict[str, str] = {}
    for case in root.iter("testcase"):
        if len(results) >= MAX_TESTS:
            raise ReportInvalid("too_many_tests")
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None or case.find("error") is not None:
            results[test_id] = FAILED
        elif case.find("skipped") is not None:
            results.setdefault(test_id, SKIPPED)
        else:
            results.setdefault(test_id, PASSED)
    return results


def counts(report: Mapping[str, str]) -> dict[str, int]:
    values = list(report.values())
    return {"total": len(values), PASSED: values.count(PASSED), FAILED: values.count(FAILED), SKIPPED: values.count(SKIPPED)}


def has_failures(report: Mapping[str, str]) -> bool:
    return FAILED in report.values()


def flaky_tests(after_runs: Sequence[Mapping[str, str]]) -> set[str]:
    seen: dict[str, set[str]] = {}
    for run in after_runs:
        for test_id, outcome in run.items():
            seen.setdefault(test_id, set()).add(outcome)
    return {test_id for test_id, outcomes in seen.items() if {PASSED, FAILED} <= outcomes}


def _class_of(before: str | None, after: str | None) -> str:
    if before is None:
        return "new_in_after"
    if after is None:
        return "missing_in_after"
    if SKIPPED in (before, after):
        return "skipped"
    return {
        (FAILED, PASSED): "fail_to_pass",
        (PASSED, PASSED): "pass_to_pass",
        (PASSED, FAILED): "pass_to_fail",
        (FAILED, FAILED): "fail_to_fail",
    }[(before, after)]


def _broken_modules(before: Mapping[str, str], after: Mapping[str, str]) -> set[str]:
    """pytest reports a module that fails to import as one failure with an empty class name: ``::tests.test_tax``."""
    return {test_id[2:] for test_id, outcome in after.items() if test_id.startswith("::") and outcome == FAILED and test_id not in before}


def _in_module(test_id: str, modules: set[str]) -> bool:
    classname = test_id.split("::", 1)[0]
    return any(classname == module or classname.startswith(f"{module}.") for module in modules)


def classify(before: Mapping[str, str], after_runs: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """``after_runs[0]`` is the first run after the change; later entries are the flaky retries."""
    flaky = flaky_tests(after_runs)
    after = after_runs[0]
    broken = _broken_modules(before, after)
    groups: dict[str, list[str]] = {name: [] for name in CLASSES}
    groups["flaky"] = sorted(flaky)
    for test_id in sorted(set(before) | set(after)):
        if test_id in flaky:
            continue
        # A test that could not even be collected did not pass: the change broke its module.
        outcome = FAILED if test_id not in after and _in_module(test_id, broken) else after.get(test_id)
        groups[_class_of(before.get(test_id), outcome)].append(test_id)
    return {
        "counts": {name: len(items) for name, items in groups.items()},
        "tests": {name: items[:MAX_LISTED] for name, items in groups.items()},
        "listing_truncated": any(len(items) > MAX_LISTED for items in groups.values()),
        "after_attempts": len(after_runs),
    }
