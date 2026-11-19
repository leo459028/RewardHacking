"""Sandbox status for ``GET /v1/sandbox/status`` and the ``sandbox_backend_health`` operations check (D24).

The API process has no Docker access (v1.6.2 D19). With ``SANDBOX_BACKEND=docker`` this report covers the settings
only; whether anything may run is decided by ``gate.evaluate``.
"""

from __future__ import annotations

from typing import Any

from app.services.sandbox import conformance, docker_config
from app.services.sandbox.base import IsolationLevel
from app.services.sandbox.policy import high_risk_threshold, required_isolation
from app.services.sandbox.registry import DOCKER, SandboxConfigError, configured_backend, get_backend


def _policy(settings: Any, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "high_risk_threshold": high_risk_threshold(settings),
        "min_isolation_default": required_isolation(0.0, None, settings).label,
        "min_isolation_high_risk": required_isolation(1.0, None, settings).label,
    }


def _failed(configured: str, policy: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "configured_backend": configured,
        "status": "fail",
        "capabilities": None,
        "executes_tools": False,
        "isolation_policy": policy,
        "smoke": {},
        "error": error,
    }


def status_report(settings: Any) -> dict[str, Any]:
    """Configured backend, its capabilities and the smoke subset of the conformance suite. Never raises."""
    configured = str(getattr(settings, "sandbox_backend", "") or "")
    if configured_backend(settings) == DOCKER:
        policy = _policy(settings, "enforced")
        problems = docker_config.config_problems(settings)
        if problems:
            return _failed(configured, policy, f"Invalid Docker sandbox settings: {', '.join(problems)}.")
        return {
            "configured_backend": configured,
            "status": "pass",
            "capabilities": docker_config.capabilities(settings).to_dict(),
            "executes_tools": False,  # agent-run tools stay simulated; only platform re-runs execute
            "isolation_policy": policy,
            "smoke": {"passed": True, "cases": [], "note": "conformance runs inside sandbox-runner"},
            "error": None,
        }
    policy = _policy(settings, "record_only")
    try:
        backend = get_backend(settings)
    except SandboxConfigError as exc:
        return _failed(configured, policy, str(exc))
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
