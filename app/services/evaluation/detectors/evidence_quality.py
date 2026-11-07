"""Turns evidence-handling notes (truncation, dropped content, unverified attestation) into findings."""

from __future__ import annotations

from app.services.evaluation.detectors.base import Finding
from app.services.evaluation.evidence import EvaluationContext

_TITLES = {
    "evidence_truncated": "Diff evidence truncated",
    "final_message_truncated": "Final message truncated",
    "file_content_dropped": "Changed-file content dropped",
    "attestation_unverified": "CI attestation not verified",
}


class EvidenceQualityDetector:
    name = "evidence_quality"
    version = "1.0.0"
    default_mode = "enforce"

    def detect(self, ctx: EvaluationContext) -> list[Finding]:
        findings: list[Finding] = []
        for index, note in enumerate(ctx.notes):
            code = str(note.get("code", "note"))
            findings.append(
                Finding(
                    detector=self.name,
                    detector_version=self.version,
                    rule_id=f"EQ-{code}",
                    category="evidence_quality",
                    severity="low",
                    confidence="high",
                    title=_TITLES.get(code, code),
                    detail=str(note.get("detail", "")),
                    evidence_ref={"snippet": f"{code}#{index}"},
                )
            )
        return findings
