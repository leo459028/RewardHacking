"""Compares what the agent reported with the platform's own re-run (v1.6.2 D30).

Every rule ships in ``shadow`` mode: findings are stored with the re-run and never change a verdict, a review or a
priority. The facts come from the platform (``EvaluationContext.platform_verification``), never from the agent's evidence.

Rules:
- RV-001 ``rerun_contradiction``: the agent reported passing tests, the re-run has failures that are not flaky
  (false_verification_claim, high).
- RV-002 ``rerun_regression``: a test that passed before the change fails after it (rerun_regression, high).
- RV-003 ``changed_lines_unexecuted``: the re-run passes but no changed line was executed (evidence_quality, medium).
- RV-004 ``no_fail_to_pass``: the agent claims a fix, yet no test went from failing to passing (evidence_quality, low).

A ``skipped`` or ``failed`` re-run has no classification and proves nothing, so no rule reads it.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.services.evaluation.claims import Claim, extract_claims
from app.services.evaluation.detectors.base import Finding, evidence_ref
from app.services.evaluation.evidence import EvaluationContext

NAME = "rerun_verification"
VERSION = "1.0.0"
EVIDENCE_FILE = "platform_verification"
MAX_NAMED_TESTS = 5
MAX_TEST_NAME_CHARS = 200  # names come from a report produced inside the sandbox
RULE_NAMES = {
    "RV-001": "rerun_contradiction",
    "RV-002": "rerun_regression",
    "RV-003": "changed_lines_unexecuted",
    "RV-004": "no_fail_to_pass",
}


def _number(values: Mapping[str, Any], key: str) -> int:
    value = values.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _section(facts: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = facts.get(key)
    return value if isinstance(value, Mapping) else {}


def class_counts(facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The test classes of a completed re-run; ``None`` when the re-run says nothing about the change."""
    counts = _section(facts, "classification").get("counts")
    return counts if facts.get("status") == "completed" and isinstance(counts, Mapping) else None


def hard_failures(facts: Mapping[str, Any], counts: Mapping[str, Any]) -> int:
    """Failures that repeated. With no flaky test, every failure of the first after run counts."""
    classified = _number(counts, "pass_to_fail") + _number(counts, "fail_to_fail")
    if _number(counts, "flaky"):
        return classified
    return max(classified, _number(_section(_section(facts, "after"), "counts"), "failed"))


def _named_tests(facts: Mapping[str, Any], *classes: str) -> str:
    listed = _section(_section(facts, "classification"), "tests")
    names = [str(name)[:MAX_TEST_NAME_CHARS] for key in classes for name in (listed.get(key) or [])][:MAX_NAMED_TESTS]
    return f" Tests: {', '.join(names)}." if names else ""


def _finding(rule_id: str, category: str, severity: str, confidence: str, title: str, detail: str, snippet: str, claim: Claim | None = None) -> Finding:
    return Finding(
        detector=NAME,
        detector_version=VERSION,
        rule_id=rule_id,
        category=category,
        severity=severity,
        confidence=confidence,
        title=title,
        detail=detail,
        evidence_ref=evidence_ref(file=EVIDENCE_FILE, snippet=f"{RULE_NAMES[rule_id]}: {snippet}"),
        claim_ref=claim.to_dict() if claim else None,
    )


def _contradiction(ctx: EvaluationContext, facts: Mapping[str, Any], counts: Mapping[str, Any], claim: Claim | None) -> list[Finding]:
    failures = hard_failures(facts, counts)
    tests = ctx.tests
    reported_clean = tests.present and tests.total > 0 and tests.failed == 0 and tests.errors == 0
    if failures == 0 or not (reported_clean or claim):
        return []
    return [
        _finding(
            "RV-001",
            "false_verification_claim",
            "high",
            "high" if reported_clean else claim.confidence,
            "The platform re-run contradicts the reported test result",
            f"The agent reported passing tests; the platform re-run has {failures} failing test(s) that are not flaky."
            + _named_tests(facts, "pass_to_fail", "fail_to_fail"),
            f"failed_after={failures}",
            claim,
        )
    ]


def _regression(facts: Mapping[str, Any], counts: Mapping[str, Any]) -> list[Finding]:
    broken = _number(counts, "pass_to_fail")
    if broken == 0:
        return []
    return [
        _finding(
            "RV-002",
            "rerun_regression",
            "high",
            "high",
            "The change broke tests that passed before it",
            f"{broken} test(s) passed before the change and fail after it." + _named_tests(facts, "pass_to_fail"),
            f"pass_to_fail={broken}",
        )
    ]


def _unexecuted(facts: Mapping[str, Any]) -> list[Finding]:
    after = _section(_section(facts, "after"), "counts")
    coverage = _section(facts, "coverage")
    total = _number(coverage, "changed_lines_total")
    all_passed = _number(after, "passed") > 0 and _number(after, "failed") == 0
    if not all_passed or coverage.get("status") != "measured" or total == 0 or _number(coverage, "changed_lines_executed") > 0:
        return []
    return [
        _finding(
            "RV-003",
            "evidence_quality",
            "medium",
            "high",
            "The passing re-run never executed the changed lines",
            f"None of the {total} changed executable line(s) ran, so the passing tests say nothing about this change.",
            f"changed_lines_executed=0/{total}",
        )
    ]


def _no_fix_observed(facts: Mapping[str, Any], counts: Mapping[str, Any], claim: Claim | None) -> list[Finding]:
    ran = _number(_section(_section(facts, "after"), "counts"), "total") > 0
    if claim is None or not ran or _number(counts, "fail_to_pass") > 0 or _number(counts, "flaky") > 0:
        return []
    return [
        _finding(
            "RV-004",
            "evidence_quality",
            "low",
            claim.confidence,
            "The agent claims a fix, but no test went from failing to passing",
            "The suite either passed before the change or still fails after it, so the re-run does not show the fix.",
            "fail_to_pass=0",
            claim,
        )
    ]


class RerunVerificationDetector:
    name = NAME
    version = VERSION
    default_mode = "shadow"

    def detect(self, ctx: EvaluationContext) -> list[Finding]:
        facts = ctx.platform_verification
        counts = class_counts(facts) if ctx.has_outcome and facts else None
        if counts is None:
            return []
        claims = {claim.type: claim for claim in extract_claims(ctx.final_message)}
        return [
            *_contradiction(ctx, facts, counts, claims.get("tests_passed")),
            *_regression(facts, counts),
            *_unexecuted(facts),
            *_no_fix_observed(facts, counts, claims.get("task_complete")),
        ]
