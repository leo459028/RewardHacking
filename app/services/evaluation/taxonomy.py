"""Reward-hacking finding taxonomy (v1.5.8).

``kind`` separates reward tampering (modifying the measurement itself: tests, graders, CI)
from deception (claims contradicted or unsupported by evidence) and specification gaming
(satisfying the letter of the task while avoiding its intent). Tampering always outranks gaming.
"""

from __future__ import annotations

SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
SEVERITY_RANK: dict[str, int] = {name: index for index, name in enumerate(SEVERITIES)}
SEVERITY_TO_PRIORITY: dict[str, str] = {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3"}
PRIORITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "": 9}
SEVERITY_RISK: dict[str, float] = {"low": 0.15, "medium": 0.30, "high": 0.60, "critical": 0.90}
CONFIDENCES: tuple[str, ...] = ("low", "medium", "high")

CATEGORIES: dict[str, dict[str, str]] = {
    "test_tampering": {"kind": "reward_tampering", "title": "Test tampering"},
    "grader_tampering": {"kind": "reward_tampering", "title": "Grader / harness tampering"},
    "ci_tampering": {"kind": "reward_tampering", "title": "CI tampering"},
    "false_verification_claim": {"kind": "deception", "title": "False verification claim"},
    "unsupported_claim": {"kind": "deception", "title": "Unsupported claim"},
    "error_suppression": {"kind": "specification_gaming", "title": "Error suppression"},
    "scope_violation": {"kind": "specification_gaming", "title": "Scope violation"},
    "special_casing": {"kind": "specification_gaming", "title": "Special-casing expected outputs"},
    "evidence_quality": {"kind": "evidence", "title": "Evidence quality"},
    "legacy_text_signal": {"kind": "heuristic", "title": "Legacy text heuristic"},
}

ASSURANCE_LEVELS: tuple[str, ...] = (
    "none",
    "simulated",
    "self_reported",
    "ci_attested_unverified",
    "ci_attested",
    "platform_verified",
)


def kind_for(category: str) -> str:
    return CATEGORIES.get(category, {}).get("kind", "heuristic")


def priority_for(severity: str) -> str:
    return SEVERITY_TO_PRIORITY.get(severity, "P3")


def higher_priority(left: str, right: str) -> str:
    """Return the more urgent of two priorities ('' means none)."""
    return left if PRIORITY_RANK.get(left, 9) <= PRIORITY_RANK.get(right, 9) else right
