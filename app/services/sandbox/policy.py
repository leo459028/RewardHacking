"""Isolation policy (v1.6.0 D21, v1.6.2 D20).

``required_isolation`` = the configured minimum for the run's risk score, raised (never lowered) by a tool
definition's ``min_isolation`` (top level or under ``metadata``). Work that really executes is refused when the
backend's isolation is below the requirement (``isolation_rejection``); there is no downgrade. The simulated backend
runs nothing, so the requirement is only recorded for it (``n/a``).
"""

from __future__ import annotations

from typing import Any

from app.services.sandbox.base import IsolationLevel

DEFAULT_MIN_ISOLATION = IsolationLevel.CONTAINER
DEFAULT_MIN_ISOLATION_HIGH_RISK = IsolationLevel.GVISOR
NOT_APPLICABLE = "n/a"
ISOLATION_INSUFFICIENT = "isolation_insufficient"


def _level(value: Any, fallback: IsolationLevel) -> IsolationLevel:
    try:
        return IsolationLevel.parse(value)
    except ValueError:
        return fallback


def high_risk_threshold(settings: Any) -> float:
    value = getattr(settings, "sandbox_high_risk_threshold", None)
    return float(value) if value is not None else float(getattr(settings, "default_risk_threshold", 0.65))


def required_isolation(risk_score: float | None, tool_def: dict | None, settings: Any) -> IsolationLevel:
    # A mistyped setting falls back to the documented default; a missing risk score is treated as high risk.
    default = _level(getattr(settings, "sandbox_min_isolation_default", None), DEFAULT_MIN_ISOLATION)
    high = max(default, _level(getattr(settings, "sandbox_min_isolation_high_risk", None), DEFAULT_MIN_ISOLATION_HIGH_RISK))
    required = high if risk_score is None or float(risk_score) >= high_risk_threshold(settings) else default
    if isinstance(tool_def, dict):
        metadata = tool_def.get("metadata") if isinstance(tool_def.get("metadata"), dict) else {}
        for override in (tool_def.get("min_isolation"), metadata.get("min_isolation")):
            if override:
                required = max(required, _level(override, IsolationLevel.MICROVM))  # unknown override -> strictest
    return required


def isolation_satisfied(actual: IsolationLevel | str, required: IsolationLevel) -> str:
    """``n/a`` for the simulated level (nothing ran), otherwise ``yes`` / ``no``."""
    actual = IsolationLevel.parse(actual)
    if actual == IsolationLevel.SIMULATED:
        return NOT_APPLICABLE
    return "yes" if actual >= required else "no"


def isolation_rejection(actual: IsolationLevel | str, risk_score: float | None, settings: Any, tool_def: dict | None = None) -> str | None:
    """D20: the refusal code when executing work needs more isolation than the backend gives, otherwise ``None``."""
    required = required_isolation(risk_score, tool_def, settings)
    return ISOLATION_INSUFFICIENT if isolation_satisfied(actual, required) == "no" else None


def high_risk_relaxed(settings: Any) -> bool:
    """True when the high-risk minimum was lowered below the default ``gvisor`` (shown on the Dashboard, D20)."""
    return required_isolation(1.0, None, settings) < DEFAULT_MIN_ISOLATION_HIGH_RISK
