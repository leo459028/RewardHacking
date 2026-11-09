"""v1.5.12 judge activation: probe records, readiness, activation cache and the startup auto-probe (plan D2–D9).

Rule of the release: **no LLM judge is used for live triage until it has passed a probe for exactly its current
configuration** (fingerprint, D5). A Dashboard profile additionally needs an explicit "activate"; a profile named by
``EVALUATION_JUDGE_PROFILE`` only needs the probe, which the API runs once in the background at startup (D2).

- ``refresh_activation_cache()`` copies ``judge_profile_states`` and ``judge_profile_configs`` into the in-process
  snapshot read by the synchronous ``resolve_active_profile()`` (D7); ``maybe_refresh()`` does so at most every
  ``EVALUATION_JUDGE_ACTIVATION_REFRESH_SECONDS`` and is called when triage is dispatched (API and worker alike).
- ``readiness()`` lists every reason a profile may not be activated (D8, D9), each with a stable code.
- ``record_probe()`` stores a probe outcome (no key, fingerprint only) and audits it (D21).
- ``startup()`` refreshes the snapshot and, when the environment-selected profile lacks a valid probe, schedules one
  probe with the fixed synthetic input (``system:startup``). Failure keeps the deterministic judge.

Nothing here touches verdicts, review priority / status or run status (D23).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.judge import JudgeProfileConfig, JudgeProfileState
from app.services.evaluation.judges import activation_state
from app.services.evaluation.judges.base import JudgeOutput
from app.services.evaluation.judges.profiles import (
    HTTP_PROVIDERS,
    JudgeProfile,
    config_fingerprint,
    get_profile,
    probe_note,
    resolve_active,
    url_is_external,
)

logger = logging.getLogger(__name__)

STARTUP_ACTOR = "system:startup"
AUDIT_TENANT = "default"
STARTUP_REFRESH_TIMEOUT_SECONDS = 5.0
# Tests inject an ``InMemoryRunStateStore`` here (tests/conftest.py); production uses Redis (D14).
_BREAKER_STORE_OVERRIDE: Any = None
# Availability failures only: a reachable model that answers badly (invalid_output) does not open the breaker.
BREAKER_FAILURE_STATUSES = frozenset({"timeout", "error"})
# Test-suite switch (tests/conftest.py sets it False): the app lifespan must not reach a real database in tests.
# The v1.5.12 tests call ``startup()`` directly with the test session factory.
RUN_ON_STARTUP = True
_STARTUP_TASKS: set[asyncio.Task] = set()

REASON_MESSAGES: dict[str, str] = {
    "missing_fields": "provider, base_url and model are required.",
    "not_probed": "Run a connection test first.",
    "probe_failed": "The last connection test failed.",
    "needs_reprobe": "The configuration or key changed since the last successful test; test again.",
    "probe_expired": "The last successful test is too old; test again.",
    "key_missing": "The API key environment variable is not set.",
    "https_required": "An external host must use HTTPS.",
    "external_not_allowed": "Sending evidence to external judges is disabled (EVALUATION_JUDGE_ALLOW_EXTERNAL=false).",
    "pricing_or_cap_missing": "An external profile needs a pricing row or a daily_token_cap.",
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _state_from_row(row: JudgeProfileState) -> activation_state.ProfileState:
    return activation_state.ProfileState(
        profile_name=row.profile_name,
        probe_status=row.probe_status or "",
        probe_error=row.probe_error or "",
        probe_fingerprint=row.probe_fingerprint or "",
        probed_at=_aware(row.probed_at),
        probe_result=dict(row.probe_result or {}),
        active=bool(row.active),
        activation_fingerprint=row.activation_fingerprint or "",
        activated_by=row.activated_by or "",
        activated_at=_aware(row.activated_at),
    )


# ---------------------------------------------------------------------------
# Activation cache (D7)
# ---------------------------------------------------------------------------


async def refresh_activation_cache(session: AsyncSession) -> activation_state.ActivationSnapshot:
    states = {row.profile_name: _state_from_row(row) for row in (await session.scalars(select(JudgeProfileState))).all()}
    configs = {row.name: (int(row.version or 1), dict(row.config or {})) for row in (await session.scalars(select(JudgeProfileConfig))).all()}
    return activation_state.install(states, configs)


async def maybe_refresh(session: AsyncSession, settings: Settings, *, force: bool = False) -> bool:
    """Refresh when the snapshot is older than the refresh interval. Never raises (a stale snapshot is safe: at
    worst a newly activated profile starts a little later, and a fingerprint change is detected without the DB)."""
    if not force and activation_state.age_seconds() < float(settings.evaluation_judge_activation_refresh_seconds):
        return False
    try:
        await refresh_activation_cache(session)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("judge activation cache refresh failed; keeping the previous snapshot", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


async def state_row(session: AsyncSession, name: str) -> JudgeProfileState | None:
    return await session.scalar(select(JudgeProfileState).where(JudgeProfileState.profile_name == name))


# ---------------------------------------------------------------------------
# Readiness (D8, D9)
# ---------------------------------------------------------------------------


def effective_external(profile: JudgeProfile, settings: Settings) -> bool:
    if profile.provider not in HTTP_PROVIDERS or not profile.base_url:
        return False
    return url_is_external(profile.base_url, profile.external, settings)


def readiness_reasons(
    profile: JudgeProfile,
    settings: Settings,
    state: activation_state.ProfileState | None,
    *,
    pricing_missing: bool,
    check_age: bool = True,
    now: datetime | None = None,
) -> list[str]:
    """Reason codes (empty = ready). ``check_age`` applies the probe max-age rule, which gates activation only."""
    if profile.provider == "deterministic":
        return []
    reasons: list[str] = []
    if profile.provider not in HTTP_PROVIDERS or not profile.base_url or not profile.model:
        reasons.append("missing_fields")
    note = probe_note(profile, settings, state)
    if note:
        reasons.append(note)
    elif check_age and state is not None:
        probed_at = _aware(state.probed_at)
        max_age = timedelta(hours=int(settings.evaluation_judge_probe_max_age_hours))
        if probed_at is None or (now or datetime.now(timezone.utc)) - probed_at > max_age:
            reasons.append("probe_expired")
    if effective_external(profile, settings):
        if not profile.api_key_env or not profile.api_key_configured(settings):
            reasons.append("key_missing")
        if not profile.base_url.lower().startswith("https://"):
            reasons.append("https_required")
        if not settings.evaluation_judge_allow_external:
            reasons.append("external_not_allowed")
        if pricing_missing and profile.daily_token_cap is None:
            reasons.append("pricing_or_cap_missing")
    return reasons


async def readiness(
    session: AsyncSession, settings: Settings, profile: JudgeProfile, *, check_age: bool = True, now: datetime | None = None
) -> dict[str, Any]:
    from app.services.judge_profiles_admin import missing_pricing

    state = activation_state.snapshot().states.get(profile.name)
    external = effective_external(profile, settings)
    pricing_missing = await missing_pricing(session, settings, profile) if external else False
    reasons = readiness_reasons(profile, settings, state, pricing_missing=pricing_missing, check_age=check_age, now=now)
    return {
        "profile": profile.name,
        "ready": not reasons,
        "reasons": [{"code": code, "message": REASON_MESSAGES.get(code, code)} for code in reasons],
        "fingerprint": config_fingerprint(profile, settings)[:12],
        "effective_external": external,
        "probe_status": state.probe_status if state else "",
        "probed_at": state.probed_at.isoformat() if state and state.probed_at else None,
    }


# ---------------------------------------------------------------------------
# Probe records (D3, D21)
# ---------------------------------------------------------------------------


async def audit(session: AsyncSession, *, event: str, profile: str, actor: str, decision: str, details: dict[str, Any]) -> None:
    """GovernanceEvent ``evaluation.judge_profile.<event>``. Callers pass names and fingerprints only, never a key."""
    from app.services.governance_audit import persist_governance_event

    await persist_governance_event(
        session,
        tenant_id=AUDIT_TENANT,
        user_id=actor[:128],
        event_type=f"evaluation.judge_profile.{event}",
        decision=decision,
        reason=f"judge profile {profile} {event}",
        details={"profile": profile, "actor": actor, **details},
    )


async def record_probe(
    session: AsyncSession, settings: Settings, profile: JudgeProfile, output: JudgeOutput, *, actor: str, effective_external_flag: bool
) -> JudgeProfileState:
    fingerprint = config_fingerprint(profile, settings)
    row = await state_row(session, profile.name)
    if row is None:
        row = JudgeProfileState(profile_name=profile.name)
        session.add(row)
    row.probe_status = output.status[:32]
    row.probe_error = (output.error or "")[:200]
    row.probe_fingerprint = fingerprint
    row.probed_at = datetime.now(timezone.utc)
    row.probe_result = {
        "latency_ms": int(output.latency_ms or 0),
        "input_tokens": int(output.input_tokens or 0),
        "output_tokens": int(output.output_tokens or 0),
        "structured_output": output.structured_output,
        "attempts": int(output.attempts or 0),
        "effective_external": bool(effective_external_flag),
        "judge_version": output.judge_version,
    }
    await session.commit()
    await audit(
        session,
        event="probed",
        profile=profile.name,
        actor=actor,
        decision="ok" if output.status == "ok" else "failed",
        details={"fingerprint": fingerprint[:12], "status": output.status, "error": (output.error or "")[:200]},
    )
    await refresh_activation_cache(session)
    return row


# ---------------------------------------------------------------------------
# Startup (D2)
# ---------------------------------------------------------------------------


def startup_probe_needed(settings: Settings) -> str:
    """Name of the environment-selected profile that should be probed now, or ""."""
    if activation_state.TRUST_ENV_PROFILE_WITHOUT_PROBE:
        return ""
    resolution = resolve_active(settings)
    if resolution.source in {"dashboard", "environment"}:
        return ""
    profile = get_profile(settings, settings.evaluation_judge_profile)
    if profile is None or profile.provider == "deterministic":
        return ""
    return profile.name


async def _startup_probe(settings: Settings, factory: Callable[[], Any], name: str) -> None:
    from app.services.judge_profiles_admin import probe_profile

    try:
        async with factory() as session:
            result = await probe_profile(session, settings, name, actor=STARTUP_ACTOR)
        if result["status"] == "ok":
            logger.info("startup probe of judge profile %s succeeded; it is now used for live triage", name)
        else:
            logger.warning("startup probe of judge profile %s failed (%s); the deterministic judge stays in use", name, result["error"])
    except Exception:  # noqa: BLE001 - never break startup because of a judge
        logger.exception("startup probe of judge profile %s failed", name)


async def startup(settings: Settings, factory: Callable[[], Any] | None = None) -> asyncio.Task | None:
    """Load the activation snapshot, then schedule the D2 auto-probe when needed. Returns the probe task (tests)."""
    if factory is None:
        from app.services.judge_background import _session_factory

        factory = _session_factory()
    if factory is None:
        return None
    try:
        async with factory() as session:
            await asyncio.wait_for(refresh_activation_cache(session), timeout=STARTUP_REFRESH_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("judge activation cache could not be loaded at startup; deterministic judge until it can", exc_info=True)
        return None
    name = startup_probe_needed(settings)
    if not name:
        return None
    task = asyncio.get_running_loop().create_task(_startup_probe(settings, factory, name))
    _STARTUP_TASKS.add(task)
    task.add_done_callback(_STARTUP_TASKS.discard)
    return task


# ---------------------------------------------------------------------------
# Circuit breaker per judge profile (D14)
# ---------------------------------------------------------------------------


def breaker_store(settings: Settings) -> Any:
    if _BREAKER_STORE_OVERRIDE is not None:
        return _BREAKER_STORE_OVERRIDE
    from app.infra.redis import get_redis
    from app.services.run_state import RedisRunStateStore

    return RedisRunStateStore(get_redis(settings))


async def breaker_state(settings: Settings, profile: str) -> dict[str, Any] | None:
    """The open breaker record, or ``None`` (closed, expired = half-open, or Redis unavailable: never block on it)."""
    try:
        return await breaker_store(settings).get_judge_breaker(profile)
    except Exception:  # noqa: BLE001
        logger.warning("judge breaker state unavailable for %s; calling the judge normally", profile, exc_info=True)
        return None


async def record_result(settings: Settings, profile: str, status: str) -> dict[str, Any] | None:
    """Count consecutive availability failures; open the breaker at the threshold. Returns the record when it opened.

    After the TTL the breaker is half-open: the next real call runs; success resets the counter, another failure
    re-opens immediately because the counter is still at or above the threshold."""
    try:
        store = breaker_store(settings)
        if status == "ok":
            await store.reset_judge_failures(profile)
            await store.close_judge_breaker(profile)
            return None
        if status not in BREAKER_FAILURE_STATUSES:
            return None
        failures = await store.record_judge_failure(profile)
        if failures < int(settings.evaluation_judge_breaker_failure_threshold):
            return None
        record = await store.open_judge_breaker(
            profile,
            reason=f"{failures} consecutive judge failures (last: {status})",
            ttl_seconds=int(settings.evaluation_judge_breaker_ttl_seconds),
            failures=failures,
        )
    except Exception:  # noqa: BLE001 - Redis unavailable: no breaker, failures still fall back
        logger.warning("judge breaker update failed for %s", profile, exc_info=True)
        return None
    await _audit_breaker_opened(profile, record)
    return record


async def _audit_breaker_opened(profile: str, record: dict[str, Any]) -> None:
    """Audited on its own session so a breaker never commits the caller's (e.g. a benchmark's) transaction."""
    from app.services.judge_background import _session_factory

    factory = _session_factory()
    if factory is None:
        return
    try:
        async with factory() as session:
            await audit(
                session,
                event="breaker_opened",
                profile=profile,
                actor="system:breaker",
                decision="opened",
                details={"failures": record.get("failures"), "ttl_seconds": record.get("ttl_seconds"), "reason": record.get("reason")},
            )
    except Exception:  # noqa: BLE001
        logger.warning("could not audit judge breaker opening for %s", profile, exc_info=True)
