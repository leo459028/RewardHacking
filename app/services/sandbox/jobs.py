"""Database work queue for ``sandbox-runner`` (v1.6.2 D21) and the status views built on it (D39).

Postgres claims work with ``SELECT ... FOR UPDATE SKIP LOCKED``. A job whose lease ran out (the runner died) goes back
to the queue once; after that it ends as ``expired``. A runner that lost its lease can no longer write a result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox import SandboxConformanceRun, SandboxJob
from app.services.governance_audit import add_governance_event
from app.services.sandbox import conformance, docker_config, gate
from app.services.sandbox.health import status_report
from app.services.sandbox.registry import DOCKER, configured_backend

KIND_CONFORMANCE = "conformance"
KIND_VERIFICATION = "verification"
GATED_KINDS = (KIND_VERIFICATION,)  # work that needs an open gate; conformance is what opens it

QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED, EXPIRED = "queued", "running", "completed", "failed", "cancelled", "expired"
MAX_ATTEMPTS = 2  # the first run plus one retry after a lost lease
LEASE_SECONDS = 300
CONFORMANCE_KEEP = 200
LEASE_EXPIRED = "lease_expired"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


async def enqueue(
    session: AsyncSession, kind: str, *, tenant_id: str = "", run_id: str | None = None, ref_id: str = "", payload: dict[str, Any] | None = None
) -> SandboxJob:
    """Adds the job to the caller's transaction; the caller commits."""
    job = SandboxJob(
        job_id=f"sbj_{uuid4().hex}", kind=kind, tenant_id=tenant_id, run_id=run_id, ref_id=ref_id, status=QUEUED,
        payload=dict(payload or {}), result={}, error_code="", attempts=0, claimed_by="", created_at=_utcnow(),
    )
    session.add(job)
    await session.flush()
    return job


async def enqueue_conformance(session: AsyncSession, *, requested_by: str) -> tuple[SandboxJob, bool]:
    """Queue a conformance run unless one is already waiting or running. Returns ``(job, created)``."""
    stmt = (
        select(SandboxJob)
        .where(SandboxJob.kind == KIND_CONFORMANCE, SandboxJob.status.in_((QUEUED, RUNNING)))
        .order_by(SandboxJob.created_at.desc(), SandboxJob.id.desc())
        .limit(1)
    )
    active = (await session.execute(stmt)).scalar_one_or_none()
    if active is not None:
        return active, False
    job = await enqueue(session, KIND_CONFORMANCE, payload={"requested_by": requested_by})
    add_governance_event(
        session, tenant_id="platform", user_id=requested_by, event_type="sandbox.conformance_requested", decision="queued",
        reason="A sandbox conformance run was requested.", details={"job_id": job.job_id},
    )
    return job, True


async def claim_next(
    session: AsyncSession, *, runner_id: str, kinds: tuple[str, ...] | list[str], now: datetime | None = None, lease_seconds: int = LEASE_SECONDS
) -> SandboxJob | None:
    """Claim the oldest queued job of ``kinds`` and commit, or return ``None``."""
    if not kinds:
        return None
    now = now or _utcnow()
    stmt = (
        select(SandboxJob)
        .where(SandboxJob.status == QUEUED, SandboxJob.kind.in_(tuple(kinds)))
        .order_by(SandboxJob.created_at.asc(), SandboxJob.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        await session.rollback()
        return None
    job.status = RUNNING
    job.claimed_by = runner_id
    job.claimed_at = now
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.attempts = int(job.attempts or 0) + 1
    await session.commit()
    return job


async def renew_lease(
    session: AsyncSession, *, job_id: str, runner_id: str, now: datetime | None = None, lease_seconds: int = LEASE_SECONDS
) -> bool:
    now = now or _utcnow()
    result = await session.execute(
        update(SandboxJob)
        .where(SandboxJob.job_id == job_id, SandboxJob.status == RUNNING, SandboxJob.claimed_by == runner_id)
        .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
    )
    await session.commit()
    return result.rowcount == 1


async def finish(
    session: AsyncSession, *, job_id: str, runner_id: str, status: str, result: dict[str, Any] | None = None,
    error_code: str = "", now: datetime | None = None,
) -> bool:
    """Write the outcome only while this runner still holds the job. ``False`` means the lease was lost."""
    updated = await session.execute(
        update(SandboxJob)
        .where(SandboxJob.job_id == job_id, SandboxJob.status == RUNNING, SandboxJob.claimed_by == runner_id)
        .values(status=status, result=dict(result or {}), error_code=error_code, finished_at=now or _utcnow(), lease_expires_at=None)
    )
    await session.commit()
    return updated.rowcount == 1


async def reclaim_expired(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Return abandoned jobs to the queue, or end them after the last attempt."""
    now = now or _utcnow()
    stmt = (
        select(SandboxJob)
        .where(SandboxJob.status == RUNNING, SandboxJob.lease_expires_at.is_not(None), SandboxJob.lease_expires_at < now)
        .with_for_update(skip_locked=True)
    )
    counts = {"requeued": 0, "expired": 0}
    for job in (await session.execute(stmt)).scalars().all():
        job.claimed_by, job.claimed_at, job.lease_expires_at = "", None, None
        if int(job.attempts or 0) >= MAX_ATTEMPTS:
            job.status, job.error_code, job.finished_at = EXPIRED, LEASE_EXPIRED, now
            counts["expired"] += 1
        else:
            job.status = QUEUED
            counts["requeued"] += 1
    await session.commit()
    return counts


async def fail_queued(session: AsyncSession, *, kinds: tuple[str, ...], error_code: str, now: datetime | None = None) -> int:
    """D14: while the gate is closed, gated work is refused and says why instead of waiting silently."""
    result = await session.execute(
        update(SandboxJob)
        .where(SandboxJob.status == QUEUED, SandboxJob.kind.in_(kinds))
        .values(status=FAILED, error_code=error_code, finished_at=now or _utcnow())
    )
    await session.commit()
    return int(result.rowcount or 0)


async def record_conformance(
    session: AsyncSession, settings: Any, *, report: list[dict[str, Any]], docker_server_version: str, triggered_by: str
) -> SandboxConformanceRun:
    caps = docker_config.capabilities(settings)
    run = SandboxConformanceRun(
        run_id=f"sbc_{uuid4().hex}",
        backend_id=caps.backend_id,
        backend_version=caps.backend_version,
        runtime=docker_config.runtime_of(settings),
        config_fingerprint=gate.config_fingerprint(settings, docker_server_version=docker_server_version),
        passed=conformance.suite_passed(report),
        cases=report,
        docker_server_version=docker_server_version,
        triggered_by=triggered_by,
        created_at=_utcnow(),
    )
    session.add(run)
    await session.commit()
    return run


async def prune_conformance(session: AsyncSession, *, keep: int = CONFORMANCE_KEEP) -> int:
    """D34: keep the most recent runs."""
    stale = (
        await session.execute(
            select(SandboxConformanceRun.id).order_by(SandboxConformanceRun.created_at.desc(), SandboxConformanceRun.id.desc()).offset(keep)
        )
    ).scalars().all()
    if stale:
        await session.execute(delete(SandboxConformanceRun).where(SandboxConformanceRun.id.in_(stale)))
    await session.commit()
    return len(stale)


async def list_jobs(session: AsyncSession, *, limit: int = 20) -> list[SandboxJob]:
    stmt = select(SandboxJob).order_by(SandboxJob.created_at.desc(), SandboxJob.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def list_conformance(session: AsyncSession, *, limit: int = 20) -> list[SandboxConformanceRun]:
    stmt = select(SandboxConformanceRun).order_by(SandboxConformanceRun.created_at.desc(), SandboxConformanceRun.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def job_to_dict(job: SandboxJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "kind": job.kind,
        "tenant_id": job.tenant_id,
        "run_id": job.run_id,
        "ref_id": job.ref_id,
        "status": job.status,
        "result": job.result or {},
        "error_code": job.error_code,
        "attempts": job.attempts,
        "claimed_by": job.claimed_by,
        "created_at": _iso(job.created_at),
        "finished_at": _iso(job.finished_at),
    }


def conformance_to_dict(run: SandboxConformanceRun) -> dict[str, Any]:
    cases = list(run.cases or [])
    return {
        "run_id": run.run_id,
        "backend_id": run.backend_id,
        "backend_version": run.backend_version,
        "runtime": run.runtime,
        "config_fingerprint": run.config_fingerprint,
        "passed": bool(run.passed),
        "cases": cases,
        "passed_cases": sum(1 for case in cases if case.get("passed")),
        "total_cases": len(cases),
        "docker_server_version": run.docker_server_version,
        "triggered_by": run.triggered_by,
        "created_at": _iso(run.created_at),
    }


async def runner_heartbeats(state_store: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Live runners with the age of their last heartbeat. A Redis outage reads as "no runner" (fails closed)."""
    if state_store is None or not hasattr(state_store, "list_sandbox_runners"):
        return []
    try:
        beats = await state_store.list_sandbox_runners()
    except Exception:  # noqa: BLE001
        return []
    now = now or _utcnow()
    runners = []
    for beat in beats:
        try:
            age = max(0, int((now - datetime.fromisoformat(str(beat.get("at")))).total_seconds()))
        except (TypeError, ValueError):
            age = None
        runners.append({**beat, "age_seconds": age})
    return sorted(runners, key=lambda item: str(item.get("runner_id")))


async def overview(session: AsyncSession, settings: Any, state_store: Any) -> dict[str, Any]:
    """Everything ``/dashboard/sandbox`` shows."""
    report = status_report(settings)
    status = await gate.evaluate(session, settings)
    history = await list_conformance(session, limit=20)
    return {
        "report": report,
        "gate": status.to_dict(),
        "runtime": docker_config.runtime_of(settings),
        "execution_enabled": bool(settings.sandbox_execution_enabled),
        "docker_selected": configured_backend(settings) == DOCKER,
        "latest": conformance_to_dict(history[0]) if history else None,
        "history": [conformance_to_dict(run) for run in history],
        "runners": await runner_heartbeats(state_store),
        "jobs": [job_to_dict(job) for job in await list_jobs(session, limit=20)],
        "images": docker_config.sandbox_images(settings),
        "conformance_image": settings.sandbox_conformance_image,
    }
