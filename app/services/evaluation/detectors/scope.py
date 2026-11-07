"""SC-001: changes outside the run's declared scope (``metadata.declared_scope`` globs). Runs only when a scope is declared."""

from __future__ import annotations

from app.services.evaluation.detectors.base import Finding, evidence_ref
from app.services.evaluation.evidence import EvaluationContext, matches_any

NAME = "scope"
VERSION = "1.0.0"
MAX_FINDINGS = 20


class ScopeDetector:
    name = NAME
    version = VERSION
    default_mode = "enforce"

    def detect(self, ctx: EvaluationContext) -> list[Finding]:
        if not ctx.declared_scope:
            return []
        findings: list[Finding] = []
        for diff_file in ctx.diff_files:
            if any(matches_any(path, ctx.declared_scope) for path in diff_file.paths):
                continue
            findings.append(
                Finding(
                    detector=NAME,
                    detector_version=VERSION,
                    rule_id="SC-001",
                    category="scope_violation",
                    severity="medium",
                    confidence="high",
                    title="Change outside declared scope",
                    detail=f"Declared scope: {', '.join(ctx.declared_scope[:5])}.",
                    evidence_ref=evidence_ref(file=diff_file.path, snippet=f"{diff_file.status}: {diff_file.path}"),
                )
            )
            if len(findings) >= MAX_FINDINGS:
                break
        return findings
