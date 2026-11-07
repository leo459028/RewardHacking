"""EvaluationEngine: runs the legacy heuristic and evidence detectors, then aggregates."""

from __future__ import annotations

from typing import Any

from app.schemas.evaluation import EvaluationResult
from app.services.evaluation import registry
from app.services.evaluation.aggregator import aggregate
from app.services.evaluation.claims import extract_claims
from app.services.evaluation.detectors import legacy_text
from app.services.evaluation.detectors.base import Detector, Finding
from app.services.evaluation.evidence import EvaluationContext


class EvaluationEngine:
    def __init__(self, *, detectors: list[Detector] | None = None, mode_overrides: dict[str, str] | None = None):
        self._detectors = detectors
        self._mode_overrides = mode_overrides

    def _resolve(self) -> tuple[list[Detector], dict[str, str]]:
        detectors = self._detectors if self._detectors is not None else registry.builtin_detectors()
        overrides = self._mode_overrides
        if overrides is None:
            from app.core.config import get_settings

            overrides = dict(get_settings().evaluation_detector_modes or {})
        return detectors, registry.resolve_modes(detectors, overrides)

    def version(self) -> str:
        detectors, modes = self._resolve()
        return registry.evaluator_version(detectors, modes)

    def evaluate_context(self, ctx: EvaluationContext) -> EvaluationResult:
        detectors, modes = self._resolve()
        legacy = legacy_text.compute(ctx)
        findings: list[Finding] = list(legacy.findings)
        for detector in detectors:
            mode = modes.get(detector.name, detector.default_mode)
            if mode == "disabled":
                continue
            for item in detector.detect(ctx):
                item.mode = mode
                findings.append(item)
        claims: list[dict[str, Any]] = [claim.to_dict() for claim in extract_claims(ctx.final_message)] if ctx.has_outcome else []
        return aggregate(
            ctx,
            findings,
            legacy,
            evaluator_version=registry.evaluator_version(detectors, modes),
            claims=claims,
        )
