"""Sandbox status for ``GET /v1/sandbox/status`` and the ``sandbox_backend_health`` operations check (D24)."""

from __future__ import annotations

from typing import Any

from app.services.sandbox import conformance
from app.services.sandbox.base import IsolationLevel
from app.services.sandbox.policy import high_risk_threshold, required_isolation
from app.services.sandbox.registry import SandboxConfigError, get_backend


def status_report(settings: Any) -> dict[str, Any]:
    """Configured backend, its capabilities and the smoke subset of the conformance suite. Never raises."""
    configured = str(getattr(settings, "sandbox_backend", "") or "")
    policy = {
        "mode": "record_only",
        "high_risk_threshold": high_risk_threshold(settings),
        "min_isolation_default": required_isolation(0.0, None, settings).label,
        "min_isolation_high_risk": required_isolation(1.0, None, settings).label,
    }
    try:
        backend = get_backend(settings)
    except SandboxConfigError as exc:
        return {
            "configured_backend": configured,
            "status": "fail",
            "capabilities": None,
            "executes_tools": False,
            "isolation_policy": policy,
            "smoke": {},
            "error": str(exc),
        }
    caps = backend.capabilities()
    smoke = conformance.run_smoke(lambda: get_backend(settings))
    return {
        "configured_backend": configured,
        "status": "pass" if smoke["passed"] else "fail",
        "capabilities": caps.to_dict(),
        # ``simulated`` means nothing really runs: no isolation guarantee and never ``platform_verified`` (D22).
        "executes_tools": caps.isolation_level > IsolationLevel.SIMULATED,
        "isolation_policy": policy,
        "smoke": smoke,
        "error": None,
    }
