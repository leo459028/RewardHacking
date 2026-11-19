"""What a finished re-run may claim (v1.6.2 D29-D31).

``platform_verified`` says who produced the test report: the platform, inside the sandbox, from the tests it keeps. It
does not say the tests passed, and it is not tamper-proof. The agent's code runs in the same process as the tests, so
code written to forge the report can still do so (``docs/security.md``, Known Boundaries).

``RV-*`` findings are stored with the re-run only. They are always ``shadow`` in this release, so an operator override
cannot make a re-run change a verdict, a review or a priority.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.services.evaluation import registry
from app.services.evaluation.aggregator import sort_findings
from app.services.evaluation.detectors.rerun_verification import RerunVerificationDetector
from app.services.evaluation.engine import EvaluationEngine
from app.services.evaluation.evidence import EvaluationContext, EvidenceError, context_from_outcome
from app.services.sandbox.base import IsolationLevel

PLATFORM_VERIFIED = "platform_verified"
NO_ASSURANCE = "none"
ASSURANCE_NOTE = (
    "The platform produced this report by re-running the tests it keeps, inside the sandbox. This does not mean the "
    "agent's code could not touch the report, and it is not tamper-proof evidence."
)
ASSURANCE_NOTE_ZH = "報告由平台在 sandbox 內、用平台保存的測試重跑產生，不表示 agent 的程式碰不到報告，也不是防竄改證明。"


def assurance_level_for(*, status: str, after: Mapping[str, Any] | None, gate_ready: bool) -> str:
    """``platform_verified`` needs a completed re-run with a report and an open conformance gate. Backend and isolation
    are read from the run that produced the report, not from the configuration."""
    measured = after or {}
    if status != "completed" or not gate_ready or measured.get("backend_id") in (None, "", "simulated") or measured.get("counts") is None:
        return NO_ASSURANCE
    try:
        level = IsolationLevel.parse(measured.get("isolation_level"))
    except ValueError:
        return NO_ASSURANCE
    return PLATFORM_VERIFIED if level >= IsolationLevel.CONTAINER else NO_ASSURANCE


def evaluator_version(settings: Any) -> str:
    return EvaluationEngine(mode_overrides=dict(settings.evaluation_detector_modes or {})).version()


def _context(evidence: Mapping[str, Any], settings: Any) -> EvaluationContext:
    try:
        ctx, _redacted = context_from_outcome(
            task_type="code_fix", goal="", metadata={}, policy_flags=[], evidence=dict(evidence), protected_paths=[], settings=settings
        )
        return ctx
    except EvidenceError:  # stored evidence that no longer fits the limits still has its final message
        message = str(evidence.get("final_message") or "")
        return EvaluationContext(
            task_type="code_fix", goal="", metadata={}, result_text=message, policy_flags=[], provenance="self_reported", has_outcome=True, final_message=message
        )


def rerun_findings(evidence: Mapping[str, Any], facts: Mapping[str, Any], settings: Any) -> list[dict[str, Any]]:
    detector = RerunVerificationDetector()
    if registry.resolve_modes([detector], dict(settings.evaluation_detector_modes or {}))[detector.name] == "disabled":
        return []
    ctx = _context(evidence, settings)
    ctx.platform_verification = dict(facts)
    findings = detector.detect(ctx)
    for finding in findings:
        finding.mode = "shadow"
    return [finding.to_dict() for finding in sort_findings(findings)]
