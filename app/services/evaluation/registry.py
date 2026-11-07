"""Detector registry, mode resolution (enforce / shadow / disabled) and ``evaluator_version``."""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.evaluation.detectors import legacy_text
from app.services.evaluation.detectors.base import Detector
from app.services.evaluation.detectors.evidence_quality import EvidenceQualityDetector

ENGINE_VERSION = "v1.5.8"
MODES = ("enforce", "shadow", "disabled")


def builtin_detectors() -> list[Detector]:
    from app.services.evaluation.detectors import ALL_DETECTORS

    return [factory() for factory in ALL_DETECTORS] + [EvidenceQualityDetector()]


def known_detector_names() -> set[str]:
    return {detector.name for detector in builtin_detectors()} | {legacy_text.NAME}


def resolve_modes(detectors: list[Detector], overrides: dict[str, str] | None) -> dict[str, str]:
    overrides = dict(overrides or {})
    modes: dict[str, str] = {}
    for detector in detectors:
        mode = str(overrides.get(detector.name, detector.default_mode)).lower()
        modes[detector.name] = mode if mode in MODES else detector.default_mode
    return modes


def unknown_detector_overrides(overrides: dict[str, Any] | None) -> list[str]:
    known = known_detector_names()
    return sorted(name for name in (overrides or {}) if name not in known)


def evaluator_version(detectors: list[Detector], modes: dict[str, str]) -> str:
    parts = sorted(f"{d.name}@{d.version}:{modes.get(d.name, d.default_mode)}" for d in detectors)
    parts.append(f"{legacy_text.NAME}@{legacy_text.VERSION}:enforce")
    digest = hashlib.sha256(";".join(sorted(parts)).encode("utf-8")).hexdigest()[:8]
    return f"{ENGINE_VERSION}:{digest}"
