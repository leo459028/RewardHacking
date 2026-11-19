"""Execution gate (v1.6.2 D14): nothing really runs unless every condition holds. It always fails closed.

Conditions: the feature flag is on, ``SANDBOX_BACKEND=docker``, the settings are valid, the backend meets the minimum
isolation, and the latest conformance run passed, matches the current configuration fingerprint and is recent.

The API and the Dashboard have no Docker access, so they compute the fingerprint with the Docker server version stored
on the conformance run. ``sandbox-runner`` passes the live version, so a Docker upgrade closes the gate until the suite
has passed again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox import SandboxConformanceRun
from app.services.sandbox import conformance, docker_config
from app.services.sandbox.base import IsolationLevel
from app.services.sandbox.policy import high_risk_relaxed, required_isolation
from app.services.sandbox.registry import DOCKER, configured_backend

EXECUTION_DISABLED = "execution_disabled"
BACKEND_NOT_DOCKER = "backend_not_docker"
ISOLATION_BELOW_MINIMUM = "isolation_below_minimum"
CONFORMANCE_MISSING = "conformance_missing"
CONFORMANCE_FAILED = "conformance_failed"
FINGERPRINT_MISMATCH = "fingerprint_mismatch"
CONFORMANCE_STALE = "conformance_stale"
NOT_CONFORMANT = "sandbox_not_conformant"  # error code of work refused because the gate is closed


@dataclass(frozen=True)
class GateStatus:
    ready: bool
    reasons: tuple[str, ...]
    fingerprint: str = ""
    latest_run_id: str = ""
    relaxed_high_risk: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "reasons": list(self.reasons),
            "fingerprint": self.fingerprint,
            "latest_run_id": self.latest_run_id,
            "relaxed_high_risk": self.relaxed_high_risk,
            **self.details,
        }


def config_fingerprint(settings: Any, *, docker_server_version: str) -> str:
    """D14: changes whenever anything that the conformance result depends on changes."""
    caps = docker_config.capabilities(settings)
    canonical = json.dumps(
        {
            "backend_id": caps.backend_id,
            "backend_version": caps.backend_version,
            "runtime": docker_config.runtime_of(settings),
            "docker_server_version": docker_server_version,
            "conformance_image": str(getattr(settings, "sandbox_conformance_image", "")),
            "sandbox_images": docker_config.sandbox_images(settings),
            "docker_user": docker_config.user_of(settings),
            "max_limits": caps.max_limits,
            "cases": list(conformance.ALL_CASE_NAMES),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def latest_conformance(session: AsyncSession) -> SandboxConformanceRun | None:
    stmt = select(SandboxConformanceRun).order_by(SandboxConformanceRun.created_at.desc(), SandboxConformanceRun.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def evaluate(
    session: AsyncSession, settings: Any, *, docker_server_version: str | None = None, now: datetime | None = None
) -> GateStatus:
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    if not getattr(settings, "sandbox_execution_enabled", False):
        reasons.append(EXECUTION_DISABLED)
    relaxed = high_risk_relaxed(settings)
    if configured_backend(settings) != DOCKER:
        return GateStatus(False, tuple([*reasons, BACKEND_NOT_DOCKER]), relaxed_high_risk=relaxed)

    problems = docker_config.config_problems(settings)
    reasons.extend(problems)
    if "runtime_invalid" in problems or "limits_invalid" in problems:
        return GateStatus(False, tuple(reasons), relaxed_high_risk=relaxed)

    actual = docker_config.capabilities(settings).isolation_level
    minimum = required_isolation(0.0, None, settings)
    if actual < minimum:
        reasons.append(ISOLATION_BELOW_MINIMUM)

    latest = await latest_conformance(session)
    details: dict[str, Any] = {"isolation_level": actual.label, "min_isolation": IsolationLevel(minimum).label}
    if latest is None:
        return GateStatus(False, tuple([*reasons, CONFORMANCE_MISSING]), relaxed_high_risk=relaxed, details=details)
    version = docker_server_version if docker_server_version is not None else latest.docker_server_version
    fingerprint = config_fingerprint(settings, docker_server_version=version)
    if not latest.passed:
        reasons.append(CONFORMANCE_FAILED)
    if latest.config_fingerprint != fingerprint:
        reasons.append(FINGERPRINT_MISMATCH)
    max_age = timedelta(hours=int(getattr(settings, "sandbox_conformance_max_age_hours", 168)))
    if now - _aware(latest.created_at) > max_age:
        reasons.append(CONFORMANCE_STALE)
    return GateStatus(
        not reasons, tuple(reasons), fingerprint=fingerprint, latest_run_id=latest.run_id, relaxed_high_risk=relaxed, details=details
    )
