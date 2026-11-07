"""Evidence-based reward-hacking detectors (v1.5.8).

``ALL_DETECTORS`` lists detector factories in a stable order; the registry instantiates them.
"""

from __future__ import annotations

from app.services.evaluation.detectors.claim_evidence import ClaimEvidenceDetector
from app.services.evaluation.detectors.error_suppression import ErrorSuppressionDetector
from app.services.evaluation.detectors.grader_tampering import GraderTamperingDetector
from app.services.evaluation.detectors.scope import ScopeDetector
from app.services.evaluation.detectors.special_casing import SpecialCasingDetector
from app.services.evaluation.detectors.test_tampering import TestTamperingDetector

ALL_DETECTORS: list = [
    TestTamperingDetector,
    GraderTamperingDetector,
    ErrorSuppressionDetector,
    ClaimEvidenceDetector,
    ScopeDetector,
    SpecialCasingDetector,  # v1.5.9, default shadow
]
