from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.evaluation.evidence import EvaluationContext
from app.services.evaluation.taxonomy import CATEGORIES, kind_for


@dataclass
class Finding:
    detector: str
    detector_version: str
    rule_id: str
    category: str
    severity: str
    confidence: str
    title: str
    detail: str = ""
    evidence_ref: dict[str, Any] = field(default_factory=dict)
    claim_ref: dict[str, Any] | None = None
    mode: str = "enforce"

    @property
    def kind(self) -> str:
        return kind_for(self.category)

    @property
    def finding_id(self) -> str:
        ref = self.evidence_ref or {}
        key = "|".join(
            [
                self.detector,
                self.rule_id,
                str(ref.get("file", "")),
                str(ref.get("line", "")),
                str(ref.get("snippet", ""))[:120],
                str((self.claim_ref or {}).get("type", "")),
            ]
        )
        return "fnd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "detector": self.detector,
            "detector_version": self.detector_version,
            "rule_id": self.rule_id,
            "mode": self.mode,
            "category": self.category,
            "category_title": CATEGORIES.get(self.category, {}).get("title", self.category),
            "kind": self.kind,
            "severity": self.severity,
            "confidence": self.confidence,
            "title": self.title,
            "detail": self.detail,
            "evidence_ref": dict(self.evidence_ref or {}),
            "claim_ref": dict(self.claim_ref) if self.claim_ref else None,
        }


def evidence_ref(file: str = "", line: int | None = None, hunk_header: str = "", snippet: str = "") -> dict[str, Any]:
    ref: dict[str, Any] = {}
    if file:
        ref["file"] = file
    if line is not None:
        ref["line"] = line
    if hunk_header:
        ref["hunk_header"] = hunk_header
    if snippet:
        ref["snippet"] = snippet.strip()[:400]
    return ref


class Detector(Protocol):
    name: str
    version: str
    default_mode: str

    def detect(self, ctx: EvaluationContext) -> list[Finding]:
        ...
