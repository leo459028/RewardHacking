"""Sandbox metrics read from the database at scrape time (v1.6.2).

``sandbox-runner`` accepts no outside requests, so it has no metrics endpoint. Its work is in the database, which also
gives every API replica the same numbers.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox import SandboxConformanceRun, SandboxJob, VerificationRun
from app.observability.metrics import SANDBOX_CONFORMANCE_PASSED, SANDBOX_JOBS, VERIFICATION_RUNS

logger = logging.getLogger(__name__)


async def refresh(session: AsyncSession) -> None:
    """A database problem must not take the metrics endpoint down."""
    try:
        job_rows = (await session.execute(select(SandboxJob.kind, SandboxJob.status, func.count()).group_by(SandboxJob.kind, SandboxJob.status))).all()
        run_rows = (await session.execute(select(VerificationRun.status, func.count()).group_by(VerificationRun.status))).all()
        latest = await session.scalar(
            select(SandboxConformanceRun.passed).order_by(SandboxConformanceRun.created_at.desc(), SandboxConformanceRun.id.desc()).limit(1)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sandbox metrics not refreshed: %s", type(exc).__name__)
        await session.rollback()
        return
    SANDBOX_JOBS.clear()
    VERIFICATION_RUNS.clear()
    for kind, status, count in job_rows:
        SANDBOX_JOBS.labels(kind=kind, status=status).set(count)
    for status, count in run_rows:
        VERIFICATION_RUNS.labels(status=status).set(count)
    SANDBOX_CONFORMANCE_PASSED.set(1 if latest else 0)
