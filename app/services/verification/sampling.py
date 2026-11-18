"""Which outcomes are re-run (v1.6.2 D32).

``SANDBOX_VERIFY_MODE`` is ``off``, ``sampled`` or ``all``. A sampled draw is an HMAC of the run ID, so an agent cannot
predict it. Anything held back after the draw is written as a ``skipped`` record with its reason, never dropped silently.
A reviewer can ask for a re-run by hand: that ignores the sampling rate and still counts against the daily cap.

Queueing changes no verdict, review or run status (AGENTS.md 2.3).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.models.evaluation_review import RunOutcome
from app.models.sandbox import VerificationRun, VerificationSuite
from app.services.governance_audit import add_governance_event
from app.services.review_sampling import random_draw
from app.services.sandbox import gate, jobs
from app.services.verification import assurance, patching, suites
from app.services.verification.errors import VerificationError

MODE_OFF, MODE_SAMPLED, MODE_ALL = "off", "sampled", "all"
TRIGGER_SAMPLED, TRIGGER_ALL, TRIGGER_MANUAL, TRIGGER_REPLAY = "sampled", "all", "manual", "replay"
QUEUED, RUNNING, SKIPPED = "queued", "running", "skipped"
ACTIVE_STATUSES = (QUEUED, RUNNING)
DAILY_CAP, SUITE_UNAVAILABLE = "daily_cap", "suite_unavailable"
SIGNAL_USAGE = "monitor_only"

# Used only when SANDBOX_VERIFY_SAMPLE_SECRET is unset: draws stay unpredictable but change on restart.
_EPHEMERAL_SECRET = secrets.token_bytes(32)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def verify_mode(settings: Any) -> str:
    mode = str(getattr(settings, "sandbox_verify_mode", MODE_OFF) or "").strip().lower()
    return mode if mode in (MODE_SAMPLED, MODE_ALL) else MODE_OFF  # fail closed on a typo


def sample_secret(settings: Any) -> bytes:
    configured = settings.sandbox_verify_sample_secret.get_secret_value()
    return configured.encode("utf-8") if configured else _EPHEMERAL_SECRET


def is_sampled(run_id: str, settings: Any) -> bool:
    return random_draw(run_id, sample_secret(settings)) < float(settings.sandbox_verify_sample_rate)


def bound_suite_id(run: AgentRun) -> str:
    """The suite fixed when the run was submitted. The outcome has no say in it (D24)."""
    value = ((run.request_payload or {}).get("metadata") or {}).get(suites.METADATA_KEY)
    return value if isinstance(value, str) else ""


def evidence_problem(evidence: dict[str, Any]) -> str:
    if evidence.get("evidence_limits"):
        return patching.EVIDENCE_TRUNCATED
    if not evidence.get("diff") and not any(item.get("after") is not None for item in evidence.get("changed_files") or []):
        return patching.NO_APPLICABLE_CHANGES
    return ""


async def _lock_tenant_day(session: AsyncSession, tenant_id: str, day: str) -> None:
    """Concurrent outcomes of one tenant must not pass the daily cap together (Postgres only)."""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(f"verification_cap:{tenant_id}:{day}".encode()).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def used_today(session: AsyncSession, tenant_id: str, now: datetime) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.count()).select_from(VerificationRun).where(
        VerificationRun.tenant_id == tenant_id, VerificationRun.created_at >= start, VerificationRun.status != SKIPPED
    )
    return int(await session.scalar(stmt) or 0)


async def _hold_back_reason(
    session: AsyncSession, settings: Settings, *, suite: VerificationSuite | None, outcome: RunOutcome, tenant_id: str, now: datetime
) -> str:
    if suite is None or suite.status != suites.ACTIVE or suite.tenant_id != tenant_id:
        return SUITE_UNAVAILABLE
    if not (await gate.evaluate(session, settings)).ready:
        return gate.NOT_CONFORMANT
    problem = evidence_problem(dict(outcome.evidence or {}))
    if problem:
        return problem
    if await used_today(session, tenant_id, now) >= int(settings.sandbox_verify_daily_cap_per_tenant):
        return DAILY_CAP
    return ""


async def stage(
    session: AsyncSession, settings: Settings, *, run: AgentRun, outcome: RunOutcome, suite_id: str, trigger: str, requested_by: str
) -> VerificationRun:
    """Add the record, queued for the runner or skipped with its reason. The caller commits."""
    now = _now()
    await _lock_tenant_day(session, run.tenant_id, now.strftime("%Y-%m-%d"))
    suite = await suites.get_suite(session, suite_id)
    reason = await _hold_back_reason(session, settings, suite=suite, outcome=outcome, tenant_id=run.tenant_id, now=now)
    row = VerificationRun(
        verification_id=f"vrf_{uuid4().hex}", run_id=run.run_id, outcome_id=outcome.outcome_id, tenant_id=run.tenant_id, suite_id=suite_id,
        artifact_digest=suite.artifact_digest if suite is not None else "", trigger=trigger, status=SKIPPED if reason else QUEUED,
        skip_reason=reason, requested_by=requested_by, created_at=now, finished_at=now if reason else None,
        assurance_level=assurance.NO_ASSURANCE if reason else "",
    )
    session.add(row)
    if not reason:
        await jobs.enqueue(
            session, jobs.KIND_VERIFICATION, tenant_id=run.tenant_id, run_id=run.run_id, ref_id=row.verification_id,
            payload={"verification_id": row.verification_id, "risk_score": float(run.risk_score or 0.0)},
        )
    add_governance_event(
        session, tenant_id=run.tenant_id, run_id=run.run_id, user_id=requested_by[:128], event_type="sandbox.verification_requested",
        decision=row.status, reason=f"Platform re-run {row.status} ({trigger}).",
        details={"verification_id": row.verification_id, "suite_id": suite_id, "trigger": trigger, "skip_reason": reason},
    )
    return row


async def enqueue(
    session: AsyncSession, settings: Settings, *, run: AgentRun, outcome: RunOutcome, suite_id: str, trigger: str, requested_by: str
) -> VerificationRun:
    row = await stage(session, settings, run=run, outcome=outcome, suite_id=suite_id, trigger=trigger, requested_by=requested_by)
    await session.commit()
    return row


async def maybe_enqueue_for_outcome(session: AsyncSession, settings: Settings, *, run: AgentRun, outcome: RunOutcome) -> VerificationRun | None:
    """Called after the outcome is committed. ``None`` means this outcome was not selected."""
    mode = verify_mode(settings)
    suite_id = bound_suite_id(run)
    if mode == MODE_OFF or not suite_id or (mode == MODE_SAMPLED and not is_sampled(run.run_id, settings)):
        return None
    trigger = TRIGGER_ALL if mode == MODE_ALL else TRIGGER_SAMPLED
    return await enqueue(session, settings, run=run, outcome=outcome, suite_id=suite_id, trigger=trigger, requested_by="system:sampling")


async def latest_outcome(session: AsyncSession, run_id: str) -> RunOutcome | None:
    return await session.scalar(select(RunOutcome).where(RunOutcome.run_id == run_id).order_by(RunOutcome.submission_seq.desc()).limit(1))


def active_statement(outcome_id: str, *, lock: bool = False) -> Select:
    stmt = (
        select(VerificationRun)
        .where(VerificationRun.outcome_id == outcome_id, VerificationRun.status.in_(ACTIVE_STATUSES))
        .order_by(VerificationRun.id.desc())
        .limit(1)
    )
    return stmt.with_for_update() if lock else stmt


async def active_for_outcome(session: AsyncSession, outcome_id: str, *, lock: bool = False) -> VerificationRun | None:
    """``lock`` holds the record until the caller commits, so the runner cannot finish it in between."""
    return await session.scalar(active_statement(outcome_id, lock=lock))


async def has_outcome(session: AsyncSession, run_id: str) -> bool:
    return await session.scalar(select(RunOutcome.outcome_id).where(RunOutcome.run_id == run_id).limit(1)) is not None


async def request_manual(session: AsyncSession, settings: Settings, *, run: AgentRun, requested_by: str) -> tuple[VerificationRun, bool]:
    """A reviewer's request for the latest outcome. Returns ``(record, created)``; an active re-run is returned as it is."""
    suite_id = bound_suite_id(run)
    if not suite_id:
        raise VerificationError("verification_suite_missing", "This run is not bound to a verification suite.", 409)
    outcome = await latest_outcome(session, run.run_id)
    if outcome is None:
        raise VerificationError("verification_outcome_missing", "This run has no outcome evidence to re-run.", 409)
    active = await active_for_outcome(session, outcome.outcome_id)
    if active is not None:
        return active, False
    return await enqueue(session, settings, run=run, outcome=outcome, suite_id=suite_id, trigger=TRIGGER_MANUAL, requested_by=requested_by), True


async def get_verification(session: AsyncSession, verification_id: str) -> VerificationRun | None:
    return await session.scalar(select(VerificationRun).where(VerificationRun.verification_id == verification_id))


async def list_verifications(
    session: AsyncSession, *, tenant_id: str | None = None, run_id: str | None = None, limit: int = 50
) -> list[VerificationRun]:
    stmt = select(VerificationRun).order_by(VerificationRun.created_at.desc(), VerificationRun.id.desc()).limit(limit)
    if tenant_id:
        stmt = stmt.where(VerificationRun.tenant_id == tenant_id)
    if run_id:
        stmt = stmt.where(VerificationRun.run_id == run_id)
    return list((await session.scalars(stmt)).all())


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def verification_to_dict(row: VerificationRun) -> dict[str, Any]:
    return {
        "verification_id": row.verification_id,
        "run_id": row.run_id,
        "outcome_id": row.outcome_id,
        "tenant_id": row.tenant_id,
        "suite_id": row.suite_id,
        "artifact_digest": row.artifact_digest,
        "trigger": row.trigger,
        "status": row.status,
        "skip_reason": row.skip_reason,
        "before": row.before or {},
        "after": row.after or {},
        "classification": row.classification or {},
        "coverage": row.coverage or {},
        "discarded_test_changes": list(row.discarded_test_changes or []),
        "backend_id": row.backend_id,
        "backend_version": row.backend_version,
        "isolation_level": row.isolation_level,
        "runtime": row.runtime,
        "config_fingerprint": row.config_fingerprint,
        "findings": list(row.findings or []),
        "assurance_level": row.assurance_level,
        "assurance_note": assurance.ASSURANCE_NOTE if row.assurance_level == assurance.PLATFORM_VERIFIED else "",
        "evaluator_version": row.evaluator_version,
        "requested_by": row.requested_by,
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
        "signal_usage": SIGNAL_USAGE,
    }
