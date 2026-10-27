from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.models.approval_event import ApprovalEvent
from app.models.finops import BudgetViolation, PricingCatalog
from app.models.incident import Incident
from app.models.operations import OperationsEvidence, ReleaseGateItem, ReleaseGateRun
from app.models.tenant import TenantApiKey
from app.models.worker_attempt import WorkerAttempt
from app.services.finops import ensure_default_pricing


@dataclass(frozen=True)
class OperationsCheck:
    check_key: str
    status: str
    title: str
    detail: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class OperationsReadiness:
    status: str
    summary: str
    checks: list[OperationsCheck]


def summarize_status(checks: list[OperationsCheck]) -> str:
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def _status_summary(status: str) -> str:
    if status == "pass":
        return "Release gate prerequisites are currently satisfied."
    if status == "warn":
        return "Release gate has warnings that should be reviewed before release."
    return "Release gate has blocking failures."


async def latest_evidence(
    session: AsyncSession,
    *,
    evidence_type: str,
) -> OperationsEvidence | None:
    return await session.scalar(
        select(OperationsEvidence)
        .where(OperationsEvidence.evidence_type == evidence_type)
        .order_by(OperationsEvidence.created_at.desc())
        .limit(1)
    )


def _fresh_evidence_check(
    evidence: OperationsEvidence | None,
    *,
    check_key: str,
    title: str,
    max_age_days: int,
    missing_status: str = "warn",
) -> OperationsCheck:
    if evidence is None:
        return OperationsCheck(
            check_key=check_key,
            status=missing_status,
            title=title,
            detail="No operations evidence has been recorded yet.",
            metadata={},
        )
    created_at = evidence.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - created_at).days
    if evidence.status == "fail":
        status = "fail"
    elif age_days > max_age_days:
        status = "warn"
    else:
        status = evidence.status if evidence.status in {"pass", "warn"} else "warn"
    return OperationsCheck(
        check_key=check_key,
        status=status,
        title=title,
        detail=evidence.summary,
        metadata={"age_days": age_days, "reference_url": evidence.reference_url},
    )


async def compute_operations_readiness(
    session: AsyncSession,
    *,
    settings: Settings,
) -> OperationsReadiness:
    now = datetime.now(timezone.utc)
    queued_cutoff = now - timedelta(minutes=settings.release_gate_max_queued_age_minutes)
    stale_queued = (
        await session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.status == "queued", AgentRun.created_at < queued_cutoff)
        )
        or 0
    )
    dlq_count = (
        await session.scalar(
            select(func.count()).select_from(WorkerAttempt).where(WorkerAttempt.status == "dlq")
        )
        or 0
    )
    blocking_incidents = (
        await session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.severity.in_(["high", "critical"]))
        )
        or 0
    )
    expired_approvals = (
        await session.scalar(
            select(func.count())
            .select_from(ApprovalEvent)
            .where(
                ApprovalEvent.status == "pending",
                (ApprovalEvent.expired.is_(True)) | (ApprovalEvent.expires_at < now),
            )
        )
        or 0
    )
    active_keys = (
        await session.scalar(
            select(func.count())
            .select_from(TenantApiKey)
            .where(TenantApiKey.status == "active", TenantApiKey.revoked.is_(False))
        )
        or 0
    )
    total_pricing_rows = (
        await session.scalar(select(func.count()).select_from(PricingCatalog)) or 0
    )
    if total_pricing_rows == 0:
        await ensure_default_pricing(session)
    active_pricing = (
        await session.scalar(
            select(func.count())
            .select_from(PricingCatalog)
            .where(PricingCatalog.is_active.is_(True))
        )
        or 0
    )
    open_critical_violations = (
        await session.scalar(
            select(func.count())
            .select_from(BudgetViolation)
            .where(
                BudgetViolation.resolution_status == "open",
                BudgetViolation.severity.in_(["high", "critical"]),
            )
        )
        or 0
    )
    violation_status = (
        ("fail" if settings.app_env.lower() == "prod" else "warn")
        if open_critical_violations
        else "pass"
    )

    checks = [
        OperationsCheck(
            check_key="audit_retention_config",
            status="pass" if settings.audit_retention_days > 0 else "fail",
            title="Audit retention configured",
            detail=f"Audit retention is set to {settings.audit_retention_days} days.",
            metadata={"audit_retention_days": settings.audit_retention_days},
        ),
        OperationsCheck(
            check_key="stale_queued_runs",
            status="fail" if stale_queued else "pass",
            title="No stale queued runs",
            detail=f"{stale_queued} queued runs are older than the release gate threshold.",
            metadata={"count": stale_queued},
        ),
        OperationsCheck(
            check_key="worker_dlq",
            status="fail" if dlq_count else "pass",
            title="Worker DLQ is empty",
            detail=f"{dlq_count} worker attempts are in DLQ.",
            metadata={"count": dlq_count},
        ),
        OperationsCheck(
            check_key="blocking_incidents",
            status="fail" if blocking_incidents else "pass",
            title="No high severity incidents",
            detail=f"{blocking_incidents} high or critical incidents are open in audit history.",
            metadata={"count": blocking_incidents},
        ),
        OperationsCheck(
            check_key="expired_approvals",
            status="warn" if expired_approvals else "pass",
            title="No expired approvals",
            detail=f"{expired_approvals} approval records are expired.",
            metadata={"count": expired_approvals},
        ),
        OperationsCheck(
            check_key="tenant_api_keys",
            status="pass" if active_keys else "warn",
            title="Tenant API keys available",
            detail=f"{active_keys} active DB-managed tenant API keys are available.",
            metadata={"active_keys": active_keys},
        ),
        OperationsCheck(
            check_key="finops_pricing_catalog_ready",
            status="pass" if active_pricing > 0 else "warn",
            title="FinOps pricing catalog ready",
            detail=f"{active_pricing} active pricing catalog rows are available.",
            metadata={"active_pricing": active_pricing},
        ),
        OperationsCheck(
            check_key="finops_open_critical_violations",
            status=violation_status,
            title="No open critical budget violations",
            detail=f"{open_critical_violations} high or critical FinOps budget violations are open.",
            metadata={"open_critical_violations": open_critical_violations},
        ),
    ]

    if settings.app_env.lower() == "prod" and settings.release_gate_fail_on_bootstrap_keys_in_prod:
        checks.append(
            OperationsCheck(
                check_key="bootstrap_keys_in_prod",
                status="fail" if settings.api_keys else "pass",
                title="No bootstrap API keys in production",
                detail="APP_ENV=prod should not rely on API_KEYS bootstrap fallback.",
                metadata={"api_keys_configured": bool(settings.api_keys)},
            )
        )

    backup = await latest_evidence(session, evidence_type="backup_restore_drill")
    secret = await latest_evidence(session, evidence_type="secret_rotation")
    safety = await latest_evidence(session, evidence_type="safety_regression")
    safety_missing_status = "fail" if settings.app_env.lower() == "prod" else "warn"
    checks.extend(
        [
            _fresh_evidence_check(
                backup,
                check_key="backup_restore_evidence",
                title="Recent backup/restore evidence",
                max_age_days=settings.operations_evidence_max_age_days,
            ),
            _fresh_evidence_check(
                secret,
                check_key="secret_rotation_evidence",
                title="Recent secret rotation evidence",
                max_age_days=settings.secret_rotation_max_age_days,
            ),
            _fresh_evidence_check(
                safety,
                check_key="safety_regression_evidence",
                title="Recent safety regression evidence",
                max_age_days=settings.operations_evidence_max_age_days,
                missing_status=safety_missing_status,
            ),
        ]
    )
    status = summarize_status(checks)
    return OperationsReadiness(status=status, summary=_status_summary(status), checks=checks)


async def persist_operations_evidence(
    session: AsyncSession,
    *,
    evidence_type: str,
    status: str,
    summary: str,
    reference_url: str,
    metadata: dict[str, Any],
    created_by: str,
) -> OperationsEvidence:
    evidence = OperationsEvidence(
        evidence_type=evidence_type,
        status=status,
        summary=summary,
        reference_url=reference_url,
        metadata_payload=metadata,
        created_by=created_by,
    )
    session.add(evidence)
    await session.commit()
    await session.refresh(evidence)
    return evidence


async def list_operations_evidence(
    session: AsyncSession,
    *,
    evidence_type: str | None = None,
    limit: int = 100,
) -> list[OperationsEvidence]:
    stmt = select(OperationsEvidence).order_by(OperationsEvidence.created_at.desc()).limit(limit)
    if evidence_type:
        stmt = stmt.where(OperationsEvidence.evidence_type == evidence_type)
    return list((await session.scalars(stmt)).all())


async def persist_release_gate(
    session: AsyncSession,
    *,
    release_version: str,
    created_by: str,
    trace_id: str,
    readiness: OperationsReadiness,
) -> ReleaseGateRun:
    gate = ReleaseGateRun(
        gate_id=f"gate_{uuid.uuid4().hex[:12]}",
        release_version=release_version,
        status=readiness.status,
        summary=readiness.summary,
        created_by=created_by,
        trace_id=trace_id,
    )
    session.add(gate)
    await session.flush()
    for check in readiness.checks:
        session.add(
            ReleaseGateItem(
                gate_id=gate.gate_id,
                check_key=check.check_key,
                status=check.status,
                title=check.title,
                detail=check.detail,
                metadata_payload=check.metadata,
            )
        )
    await session.commit()
    await session.refresh(gate)
    return gate


async def list_release_gates(session: AsyncSession, *, limit: int = 50) -> list[ReleaseGateRun]:
    stmt = select(ReleaseGateRun).order_by(ReleaseGateRun.created_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def get_release_gate(session: AsyncSession, gate_id: str) -> ReleaseGateRun | None:
    return await session.scalar(select(ReleaseGateRun).where(ReleaseGateRun.gate_id == gate_id))


async def list_release_gate_items(session: AsyncSession, gate_id: str) -> list[ReleaseGateItem]:
    stmt = (
        select(ReleaseGateItem)
        .where(ReleaseGateItem.gate_id == gate_id)
        .order_by(ReleaseGateItem.id.asc())
    )
    return list((await session.scalars(stmt)).all())
