"""Build a bounded, re-redacted ``JudgeInput`` from stored outcome evidence (v1.5.10, D7).

Evidence stored in ``run_outcomes.evidence`` is already redacted at ingestion; it is redacted again
here before anything can leave the process. Submitter identity, API key IDs and honeypot identity are
never included. Over-long material is truncated in the order diff → test report → final message → lint.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.evaluation.evidence import redact_text, redact_value
from app.services.evaluation.judges.base import JudgeInput
from app.services.evaluation.judges.injection import scan_for_injection

TRUNCATION_ORDER: tuple[str, ...] = ("diff", "test_report", "final_message", "lint_report")
_FINDING_KEYS = ("finding_id", "rule_id", "detector", "category", "severity", "confidence", "mode", "title")


def _test_report_text(report: Any) -> str:
    if not isinstance(report, dict) or not report:
        return ""
    if report.get("content"):
        return str(report.get("content"))
    summary = report.get("summary")
    if isinstance(summary, dict):
        return json.dumps({"format": report.get("format", "summary"), "summary": summary}, sort_keys=True)
    return ""


def _lint_text(lint: Any) -> str:
    if not isinstance(lint, dict) or not lint:
        return ""
    head = {key: lint.get(key) for key in ("tool", "errors", "warnings") if key in lint}
    content = str(lint.get("content") or "")
    return json.dumps(head, sort_keys=True) + ("\n" + content if content else "")


def _finding_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = {key: item.get(key) for key in _FINDING_KEYS if item.get(key) not in (None, "")}
    ref = item.get("evidence_ref")
    if isinstance(ref, dict):
        if ref.get("file"):
            summary["file"] = redact_text(str(ref["file"]))[:300]
        if ref.get("line") is not None:
            summary["line"] = ref.get("line")
    return summary


def build_judge_input(
    *,
    evidence: dict[str, Any] | None,
    task_type: str,
    goal: str,
    metadata: dict[str, Any] | None,
    findings: list[dict[str, Any]] | None,
    max_chars: int,
) -> JudgeInput:
    safe = redact_value(dict(evidence or {}))
    meta = dict(metadata or {})
    scope = meta.get("declared_scope") if isinstance(meta.get("declared_scope"), list) else []
    fields: dict[str, str] = {
        "diff": str(safe.get("diff") or ""),
        "test_report": _test_report_text(safe.get("test_report")),
        "final_message": str(safe.get("final_message") or ""),
        "lint_report": _lint_text(safe.get("lint_report")),
    }
    # Scan before truncation so a marker hidden at the end of a long diff still counts.
    transcript_text = json.dumps(safe.get("tool_transcript") or [], ensure_ascii=False, default=str)
    markers = scan_for_injection(*fields.values(), transcript_text, str(goal or ""))

    budget = max(int(max_chars), 1)
    fixed = len(redact_text(str(goal or ""))) + sum(len(str(item)) for item in scope)
    total = fixed + sum(len(value) for value in fields.values())
    truncated: dict[str, int] = {}
    excess = total - budget
    for name in TRUNCATION_ORDER:
        if excess <= 0:
            break
        original = len(fields[name])
        if not original:
            continue
        keep = max(original - excess, 0)
        fields[name] = fields[name][:keep] + ("\n[truncated]" if keep < original else "")
        truncated[name] = original
        excess -= original - keep

    return JudgeInput(
        task_type=str(task_type or "code_fix"),
        goal=redact_text(str(goal or ""))[:2000],
        declared_scope=[redact_text(str(item))[:300] for item in scope][:50],
        diff=fields["diff"],
        test_report=fields["test_report"],
        lint_report=fields["lint_report"],
        final_message=fields["final_message"],
        findings=[_finding_summary(item) for item in list(findings or []) if item.get("mode", "enforce") != "disabled"][:50],
        agent_model_name=str(meta.get("model_name") or "")[:128],
        truncated=truncated,
        injection_markers=markers,
    )
