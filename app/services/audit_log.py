from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.approval_event import ApprovalEvent
from app.models.agent_run import AgentRun
from app.models.evaluation_result import EvaluationResultRecord
from app.models.finops import BudgetViolation, CostEvent
from app.models.governance_event import GovernanceEvent
from app.models.incident import Incident
from app.models.tenant import Tenant, TenantApiKey
from app.models.tool_execution import ToolExecution
from app.models.worker_attempt import WorkerAttempt
from app.observability.metrics import INCIDENTS_TOTAL
from app.schemas.agent import AgentRequest
from app.schemas.audit import ApprovalDecisionRequest
from app.services.control_plane import sync_execution_plan_on_approval
from app.services.orchestrator import OrchestrationResult


@dataclass(frozen=True)
class DashboardSummary:
    total_runs: int
    completed: int
    escalated: int
    denied: int
    queued: int
    running: int
    awaiting_approval: int
    failed: int
    average_risk_score: float
    recent_incidents: list[Incident]


@dataclass(frozen=True)
class ObservabilitySummary:
    worker: dict[str, int]
    incidents: dict[str, int]
    approvals: dict[str, int]
    evaluator: dict[str, int]
    slo: dict[str, int]
    finops: dict[str, int] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class GovernanceSummary:
    tenant_id: str
    total_runs: int
    awaiting_approval: int
    denied: int
    quota_limited: int
    average_risk_score: float
    recent_events: list[GovernanceEvent]


def _requires_review(status: str, summary: dict[str, Any]) -> bool:
    if status in {"denied", "escalated", "awaiting_approval", "failed"}:
        return True
    event_payload = (
        summary.get("evaluation_event", {})
        .get("payload", {})
        if isinstance(summary, dict)
        else {}
    )
    return event_payload.get("verdict") == "review"


def _incident_payload(result: OrchestrationResult) -> tuple[str, str, str] | None:
    status = result.response.status
    if status == "denied":
        return ("high", "policy_denied", result.response.decision_reason)
    if status == "escalated":
        return ("medium", "policy_escalated", result.response.decision_reason)
    if status == "awaiting_approval":
        return ("medium", "approval_required", result.response.decision_reason)
    if status == "failed":
        return ("high", "worker_failed", result.response.decision_reason)

    incident_event = result.evaluation_summary.get("incident_event")
    if isinstance(incident_event, dict):
        payload = incident_event.get("payload", {})
        return (
            payload.get("severity", "medium"),
            payload.get("code", "eval_review_required"),
            payload.get("detail", "Evaluation requires review."),
        )
    return None


def _add_incident(
    session: AsyncSession,
    *,
    run_id: str,
    severity: str,
    code: str,
    detail: str,
) -> None:
    session.add(
        Incident(
            run_id=run_id,
            severity=severity,
            code=code,
            detail=detail,
        )
    )
    INCIDENTS_TOTAL.labels(severity=severity, code=code).inc()


def _approval_expiry(request: AgentRequest) -> datetime:
    ttl = int(request.metadata.get("approval_ttl_seconds", get_settings().approval_ttl_seconds))
    return datetime.now(timezone.utc) + timedelta(seconds=ttl)


def _sync_approval_summary(
    summary: dict[str, Any],
    *,
    status: str,
    reviewer: str = "",
    reason: str = "",
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    approval = dict(summary.get("approval", {}))
    approval.update(
        {
            "status": status,
            "reviewer": reviewer,
            "reason": reason or approval.get("reason", ""),
        }
    )
    if expires_at is not None:
        approval["expires_at"] = expires_at.isoformat()
        approval["expired"] = expires_at <= datetime.now(timezone.utc)
    summary["approval"] = approval
    return summary


def _append_event(summary: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    events = list(summary.get("event_timeline", []))
    events.append(event)
    summary["event_timeline"] = events
    return summary


async def _persist_approval_required(
    session: AsyncSession,
    request: AgentRequest,
    result: OrchestrationResult,
    run_summary: dict[str, Any],
) -> None:
    if result.response.status != "awaiting_approval":
        return
    expires_at = _approval_expiry(request)
    _sync_approval_summary(
        run_summary,
        status="pending",
        reason=result.response.decision_reason,
        expires_at=expires_at,
    )
    session.add(
        ApprovalEvent(
            run_id=result.response.run_id,
            status="pending",
            reviewer="",
            reason=result.response.decision_reason,
            expires_at=expires_at,
            expired=expires_at <= datetime.now(timezone.utc),
        )
    )


async def persist_worker_attempt(
    session: AsyncSession,
    *,
    run_id: str,
    attempt_number: int,
    status: str,
    failure_reason: str = "",
    next_retry_at: datetime | None = None,
) -> WorkerAttempt:
    attempt = WorkerAttempt(
        run_id=run_id,
        attempt_number=attempt_number,
        status=status,
        failure_reason=failure_reason,
        next_retry_at=next_retry_at,
    )
    session.add(attempt)
    await session.flush()
    return attempt


async def worker_attempt_count(session: AsyncSession, run_id: str) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(WorkerAttempt).where(WorkerAttempt.run_id == run_id)
        )
        or 0
    )


async def list_worker_attempts(
    session: AsyncSession,
    *,
    run_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[WorkerAttempt]:
    stmt = select(WorkerAttempt).order_by(WorkerAttempt.created_at.desc()).limit(limit)
    if run_id:
        stmt = stmt.where(WorkerAttempt.run_id == run_id)
    if status:
        stmt = stmt.where(WorkerAttempt.status == status)
    return list((await session.scalars(stmt)).all())


async def persist_tool_executions(
    session: AsyncSession,
    *,
    run_id: str,
    summary: dict[str, Any],
) -> None:
    transcript = (
        summary.get("execution_event", {})
        .get("payload", {})
        .get("tool_transcript", [])
        if isinstance(summary, dict)
        else []
    )
    for entry in transcript:
        session.add(
            ToolExecution(
                run_id=run_id,
                tool_name=str(entry.get("tool_name", "")),
                status=str(entry.get("status", "")),
                detail=str(entry.get("detail", "")),
                sandbox_policy=dict(entry.get("sandbox_policy", {})),
                artifact_refs=list(entry.get("artifact_refs", [])),
            )
        )


async def list_tool_executions(
    session: AsyncSession,
    *,
    run_id: str,
    limit: int = 100,
) -> list[ToolExecution]:
    stmt = (
        select(ToolExecution)
        .where(ToolExecution.run_id == run_id)
        .order_by(ToolExecution.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def persist_evaluation_result(
    session: AsyncSession,
    *,
    request: AgentRequest,
    result: OrchestrationResult,
) -> None:
    payload = result.evaluation_summary.get("evaluation_event", {}).get("payload", {})
    if not payload:
        return
    session.add(
        EvaluationResultRecord(
            run_id=result.response.run_id,
            task_type=request.task_type,
            verdict=str(payload.get("verdict", "")),
            final_score=float(payload.get("final_score", 0.0)),
            rubric=dict(payload.get("rubric", {"task_type": request.task_type})),
            judge_signal=dict(payload.get("judge_signal", {"enabled": False})),
            explanation=str(payload.get("explanation", "")),
        )
    )


async def list_evaluation_results(
    session: AsyncSession,
    *,
    task_type: str | None = None,
    verdict: str | None = None,
    tenant_id: str | None = None,
    limit: int = 100,
) -> list[EvaluationResultRecord]:
    stmt = select(EvaluationResultRecord).order_by(EvaluationResultRecord.created_at.desc()).limit(limit)
    if task_type:
        stmt = stmt.where(EvaluationResultRecord.task_type == task_type)
    if verdict:
        stmt = stmt.where(EvaluationResultRecord.verdict == verdict)
    if tenant_id:
        stmt = stmt.join(AgentRun, AgentRun.run_id == EvaluationResultRecord.run_id).where(
            AgentRun.tenant_id == tenant_id
        )
    return list((await session.scalars(stmt)).all())


async def persist_governance_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str = "",
    run_id: str = "",
    event_type: str,
    decision: str,
    reason: str,
    limit_value: int = 0,
    used: int = 0,
    remaining: int = 0,
    details: dict[str, Any] | None = None,
) -> GovernanceEvent:
    event = GovernanceEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        event_type=event_type,
        decision=decision,
        reason=reason,
        limit_value=limit_value,
        used=used,
        remaining=remaining,
        details=details or {},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


async def list_governance_events(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> list[GovernanceEvent]:
    stmt = select(GovernanceEvent).order_by(GovernanceEvent.created_at.desc()).limit(limit)
    if tenant_id:
        stmt = stmt.where(GovernanceEvent.tenant_id == tenant_id)
    if event_type:
        stmt = stmt.where(GovernanceEvent.event_type == event_type)
    return list((await session.scalars(stmt)).all())


async def governance_summary(
    session: AsyncSession,
    *,
    tenant_id: str = "default",
) -> GovernanceSummary:
    total_runs = (
        await session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.tenant_id == tenant_id)
        )
        or 0
    )
    average_risk_score = (
        await session.scalar(
            select(func.avg(AgentRun.risk_score)).where(AgentRun.tenant_id == tenant_id)
        )
        or 0.0
    )

    async def count_status(status: str) -> int:
        return (
            await session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.tenant_id == tenant_id, AgentRun.status == status)
            )
            or 0
        )

    quota_limited = (
        await session.scalar(
            select(func.count())
            .select_from(GovernanceEvent)
            .where(
                GovernanceEvent.tenant_id == tenant_id,
                GovernanceEvent.event_type.in_(
                    ["request_quota_exceeded", "high_risk_quota_exceeded"]
                ),
            )
        )
        or 0
    )

    return GovernanceSummary(
        tenant_id=tenant_id,
        total_runs=total_runs,
        awaiting_approval=await count_status("awaiting_approval"),
        denied=await count_status("denied"),
        quota_limited=quota_limited,
        average_risk_score=round(float(average_risk_score), 3),
        recent_events=await list_governance_events(session, tenant_id=tenant_id, limit=10),
    )


async def create_tenant(
    session: AsyncSession,
    *,
    tenant_id: str,
    display_name: str,
    request_limit_per_minute: int,
    high_risk_limit_per_hour: int,
) -> Tenant:
    existing = await get_tenant(session, tenant_id)
    if existing is not None:
        return existing
    tenant = Tenant(
        tenant_id=tenant_id,
        display_name=display_name,
        status="active",
        request_limit_per_minute=request_limit_per_minute,
        high_risk_limit_per_hour=high_risk_limit_per_hour,
    )
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def update_tenant(
    session: AsyncSession,
    tenant: Tenant,
    *,
    display_name: str | None = None,
    status: str | None = None,
    request_limit_per_minute: int | None = None,
    high_risk_limit_per_hour: int | None = None,
) -> Tenant:
    if display_name is not None:
        tenant.display_name = display_name
    if status is not None:
        tenant.status = status
    if request_limit_per_minute is not None:
        tenant.request_limit_per_minute = request_limit_per_minute
    if high_risk_limit_per_hour is not None:
        tenant.high_risk_limit_per_hour = high_risk_limit_per_hour
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def get_tenant(session: AsyncSession, tenant_id: str) -> Tenant | None:
    return await session.scalar(select(Tenant).where(Tenant.tenant_id == tenant_id))


async def list_tenants(session: AsyncSession, *, limit: int = 100) -> list[Tenant]:
    stmt = select(Tenant).order_by(Tenant.created_at.asc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def create_tenant_api_key(
    session: AsyncSession,
    *,
    tenant_id: str,
    key_id: str,
    name: str,
    key_prefix: str,
    key_hash: str,
    role: str,
) -> TenantApiKey:
    api_key = TenantApiKey(
        tenant_id=tenant_id,
        key_id=key_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        role=role,
        status="active",
        revoked=False,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return api_key


async def list_tenant_api_keys(
    session: AsyncSession,
    *,
    tenant_id: str,
    limit: int = 100,
) -> list[TenantApiKey]:
    stmt = (
        select(TenantApiKey)
        .where(TenantApiKey.tenant_id == tenant_id)
        .order_by(TenantApiKey.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def get_tenant_api_key(
    session: AsyncSession,
    *,
    tenant_id: str,
    key_id: str,
) -> TenantApiKey | None:
    return await session.scalar(
        select(TenantApiKey).where(
            TenantApiKey.tenant_id == tenant_id,
            TenantApiKey.key_id == key_id,
        )
    )


async def revoke_tenant_api_key(
    session: AsyncSession,
    api_key: TenantApiKey,
) -> TenantApiKey:
    api_key.status = "revoked"
    api_key.revoked = True
    api_key.revoked_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(api_key)
    return api_key


async def persist_orchestration_result(
    session: AsyncSession,
    request: AgentRequest,
    result: OrchestrationResult,
) -> AgentRun:
    response = result.response
    summary = dict(result.evaluation_summary)
    await _persist_approval_required(session, request, result, summary)
    run = AgentRun(
        run_id=response.run_id,
        tenant_id=request.tenant_id or "default",
        user_id=request.user_id,
        task_type=request.task_type,
        prompt=request.prompt,
        goal=request.goal,
        status=response.status,
        decision_reason=response.decision_reason,
        risk_score=response.risk_score,
        policy_flags=response.policy_flags,
        request_payload=request.model_dump(),
        execution_output=result.execution_output,
        evaluation_summary=summary,
        result={
            "response": response.model_dump(),
            "execution_output": result.execution_output,
            "evaluation_summary": summary,
        },
        requires_human_review=_requires_review(response.status, summary),
    )
    session.add(run)

    incident = _incident_payload(result)
    if incident is not None:
        severity, code, detail = incident
        _add_incident(session, run_id=response.run_id, severity=severity, code=code, detail=detail)
    await persist_tool_executions(session, run_id=response.run_id, summary=summary)
    await persist_evaluation_result(session, request=request, result=result)

    await session.commit()
    await session.refresh(run)
    return run


async def update_orchestration_result(
    session: AsyncSession,
    run: AgentRun,
    request: AgentRequest,
    result: OrchestrationResult,
) -> AgentRun:
    response = result.response
    summary = dict(result.evaluation_summary)
    await _persist_approval_required(session, request, result, summary)
    run.status = response.status
    run.tenant_id = request.tenant_id or run.tenant_id or "default"
    run.decision_reason = response.decision_reason
    run.risk_score = response.risk_score
    run.policy_flags = response.policy_flags
    run.request_payload = request.model_dump()
    run.execution_output = result.execution_output
    run.evaluation_summary = summary
    run.result = {
        "response": response.model_dump(),
        "execution_output": result.execution_output,
        "evaluation_summary": summary,
    }
    run.requires_human_review = _requires_review(response.status, summary)

    incident = _incident_payload(result)
    if incident is not None:
        severity, code, detail = incident
        _add_incident(session, run_id=response.run_id, severity=severity, code=code, detail=detail)
    await persist_tool_executions(session, run_id=response.run_id, summary=summary)
    await persist_evaluation_result(session, request=request, result=result)

    await session.commit()
    await session.refresh(run)
    return run


async def list_runs(
    session: AsyncSession,
    *,
    status: str | None = None,
    task_type: str | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AgentRun]:
    stmt = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit).offset(offset)
    if status:
        stmt = stmt.where(AgentRun.status == status)
    if task_type:
        stmt = stmt.where(AgentRun.task_type == task_type)
    if tenant_id:
        stmt = stmt.where(AgentRun.tenant_id == tenant_id)
    return list((await session.scalars(stmt)).all())


async def get_run(session: AsyncSession, run_id: str) -> AgentRun | None:
    return await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))


async def list_incidents(
    session: AsyncSession,
    *,
    severity: str | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Incident]:
    stmt = select(Incident).order_by(Incident.created_at.desc()).limit(limit).offset(offset)
    if severity:
        stmt = stmt.where(Incident.severity == severity)
    if tenant_id:
        stmt = stmt.join(AgentRun, AgentRun.run_id == Incident.run_id).where(AgentRun.tenant_id == tenant_id)
    return list((await session.scalars(stmt)).all())


async def incidents_for_run(session: AsyncSession, run_id: str) -> list[Incident]:
    stmt = select(Incident).where(Incident.run_id == run_id).order_by(Incident.created_at.desc())
    return list((await session.scalars(stmt)).all())


async def list_approvals(
    session: AsyncSession,
    *,
    status: str | None = None,
    reviewer: str | None = None,
    expired: bool | None = None,
    tenant_id: str | None = None,
    limit: int = 100,
) -> list[AgentRun]:
    stmt = (
        select(AgentRun)
        .join(ApprovalEvent, ApprovalEvent.run_id == AgentRun.run_id)
        .where(AgentRun.status == "awaiting_approval")
        .order_by(ApprovalEvent.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(ApprovalEvent.status == status)
    if reviewer:
        stmt = stmt.where(ApprovalEvent.reviewer == reviewer)
    if expired is not None:
        now = datetime.now(timezone.utc)
        stmt = stmt.where((ApprovalEvent.expired.is_(expired)) | (ApprovalEvent.expires_at <= now if expired else ApprovalEvent.expires_at > now))
    if tenant_id:
        stmt = stmt.where(AgentRun.tenant_id == tenant_id)
    return list((await session.scalars(stmt)).unique().all())


async def apply_approval_decision(
    session: AsyncSession,
    run_id: str,
    decision: ApprovalDecisionRequest,
) -> AgentRun | None:
    run = await get_run(session, run_id)
    if run is None:
        return None

    summary = dict(run.evaluation_summary or {})
    approval = dict(summary.get("approval", {}))
    approval.update(
        {
            "status": decision.decision,
            "reviewer": decision.reviewer,
            "reason": decision.reason,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    summary["approval"] = approval
    session.add(
        ApprovalEvent(
            run_id=run.run_id,
            status=decision.decision,
            reviewer=decision.reviewer,
            reason=decision.reason,
            expired=bool(approval.get("expired", False)),
        )
    )

    events = list(summary.get("event_timeline", []))
    events.append(
        {
            "event_type": "approval.decision",
            "status": decision.decision,
            "reviewer": decision.reviewer,
            "ts": approval["decided_at"],
        }
    )
    summary["event_timeline"] = events

    if decision.decision == "approve_once":
        request_payload = dict(run.request_payload or {})
        constraints = dict(request_payload.get("constraints", {}))
        constraints["requires_human_approval"] = True
        request_payload["constraints"] = constraints
        run.request_payload = request_payload
        run.status = "queued"
        run.decision_reason = f"Approved once by {decision.reviewer}: {decision.reason}"
        run.requires_human_review = False
        summary["run_state"] = {
            "status": "queued",
            "updated_at": approval["decided_at"],
        }
    elif decision.decision == "deny":
        run.status = "denied"
        run.decision_reason = f"Denied by {decision.reviewer}: {decision.reason}"
        run.requires_human_review = False
        summary["run_state"] = {
            "status": "denied",
            "updated_at": approval["decided_at"],
        }
        _add_incident(
            session,
            run_id=run.run_id,
            severity="high",
            code="approval_denied",
            detail=run.decision_reason,
        )
    elif decision.decision == "request_changes":
        run.status = "awaiting_approval"
        run.decision_reason = f"Changes requested by {decision.reviewer}: {decision.reason}"
        run.requires_human_review = True
        summary["run_state"] = {
            "status": "awaiting_approval",
            "updated_at": approval["decided_at"],
        }
    else:
        raise ValueError("decision must be approve_once, deny, or request_changes")

    run.evaluation_summary = summary
    await sync_execution_plan_on_approval(
        session,
        run=run,
        decision=decision.decision,
        reviewer=decision.reviewer,
        reason=decision.reason,
    )
    summary = dict(run.evaluation_summary or {})
    run.result = {
        "response": {
            "run_id": run.run_id,
            "status": run.status,
            "decision_reason": run.decision_reason,
            "risk_score": run.risk_score,
            "policy_flags": run.policy_flags,
        },
        "execution_output": run.execution_output,
        "evaluation_summary": summary,
    }
    await session.commit()
    await session.refresh(run)
    return run


async def expire_stale_approvals(session: AsyncSession) -> list[str]:
    now = datetime.now(timezone.utc)
    stmt = select(ApprovalEvent).where(
        ApprovalEvent.status == "pending",
        (ApprovalEvent.expired.is_(True)) | (ApprovalEvent.expires_at <= now),
    )
    stale_events = list((await session.scalars(stmt)).all())
    expired_run_ids: list[str] = []

    for event in stale_events:
        event.expired = True
        event.status = "expired"
        if not event.reviewer:
            event.reviewer = "system_ttl_sweeper"
        run = await get_run(session, event.run_id)
        if run is not None and run.status == "awaiting_approval":
            reason = "Approval TTL expired without reviewer decision."
            run.status = "denied"
            run.decision_reason = reason
            run.requires_human_review = False
            flags = list(run.policy_flags or [])
            if "approval_ttl_expired" not in flags:
                flags.append("approval_ttl_expired")
            run.policy_flags = flags

            summary = dict(run.evaluation_summary or {})
            approval = dict(summary.get("approval", {}))
            approval.update(
                {
                    "status": "expired",
                    "expired": True,
                    "reviewer": "system_ttl_sweeper",
                    "reason": reason,
                    "decided_at": now.isoformat(),
                }
            )
            summary["approval"] = approval
            summary["run_state"] = {"status": "denied", "updated_at": now.isoformat()}
            run.evaluation_summary = summary

            await sync_execution_plan_on_approval(
                session,
                run=run,
                decision="deny",
                reviewer="system_ttl_sweeper",
                reason=reason,
            )
            summary = dict(run.evaluation_summary or {})
            run.result = {
                "response": {
                    "run_id": run.run_id,
                    "status": run.status,
                    "decision_reason": run.decision_reason,
                    "risk_score": run.risk_score,
                    "policy_flags": run.policy_flags,
                },
                "execution_output": run.execution_output,
                "evaluation_summary": summary,
            }
            if run.run_id not in expired_run_ids:
                expired_run_ids.append(run.run_id)

    await session.commit()
    return expired_run_ids


async def requeue_dlq_run(session: AsyncSession, run_id: str) -> AgentRun | None:
    run = await get_run(session, run_id)
    if run is None:
        return None
    stmt = select(WorkerAttempt).where(
        WorkerAttempt.run_id == run_id,
        WorkerAttempt.status == "dlq",
    )
    attempts = list((await session.scalars(stmt)).all())
    for attempt in attempts:
        attempt.status = "requeued"

    run.status = "queued"
    run.decision_reason = "Requeued from worker DLQ by operator."
    flags = [f for f in list(run.policy_flags or []) if f != "worker_failed"]
    if "dlq_requeued" not in flags:
        flags.append("dlq_requeued")
    run.policy_flags = flags
    summary = dict(run.evaluation_summary or {})
    summary["run_state"] = {"status": "queued", "updated_at": datetime.now(timezone.utc).isoformat()}
    run.evaluation_summary = summary
    run.result = {
        "response": {
            "run_id": run.run_id,
            "status": run.status,
            "decision_reason": run.decision_reason,
            "risk_score": run.risk_score,
            "policy_flags": run.policy_flags,
        },
        "execution_output": run.execution_output,
        "evaluation_summary": summary,
    }
    await session.commit()
    await session.refresh(run)
    return run


async def resolve_dlq_run(
    session: AsyncSession,
    run_id: str,
    *,
    reason: str = "Resolved by operator",
) -> list[WorkerAttempt]:
    stmt = select(WorkerAttempt).where(
        WorkerAttempt.run_id == run_id,
        WorkerAttempt.status == "dlq",
    )
    attempts = list((await session.scalars(stmt)).all())
    for attempt in attempts:
        attempt.status = "dlq_resolved"
        base_reason = (attempt.failure_reason or "").strip()
        attempt.failure_reason = f"{base_reason} | resolved: {reason}".strip(" |")
    await session.commit()
    return attempts


async def dashboard_summary(session: AsyncSession) -> DashboardSummary:
    total_runs = await session.scalar(select(func.count()).select_from(AgentRun)) or 0
    average_risk_score = await session.scalar(select(func.avg(AgentRun.risk_score))) or 0.0

    async def count_status(status: str) -> int:
        return (
            await session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.status == status)
            )
            or 0
        )

    return DashboardSummary(
        total_runs=total_runs,
        completed=await count_status("completed"),
        escalated=await count_status("escalated"),
        denied=await count_status("denied"),
        queued=await count_status("queued"),
        running=await count_status("running"),
        awaiting_approval=await count_status("awaiting_approval"),
        failed=await count_status("failed"),
        average_risk_score=round(float(average_risk_score), 3),
        recent_incidents=await list_incidents(session, limit=5),
    )


async def observability_summary(session: AsyncSession) -> ObservabilitySummary:
    async def count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for condition in where:
            stmt = stmt.where(condition)
        return await session.scalar(stmt) or 0

    settings = get_settings()
    return ObservabilitySummary(
        worker={
            "completed": await count(WorkerAttempt, WorkerAttempt.status == "completed"),
            "retry_scheduled": await count(WorkerAttempt, WorkerAttempt.status == "retry_scheduled"),
            "dlq": await count(WorkerAttempt, WorkerAttempt.status == "dlq"),
        },
        incidents={
            "total": await count(Incident),
            "high": await count(Incident, Incident.severity == "high"),
            "medium": await count(Incident, Incident.severity == "medium"),
        },
        approvals={
            "pending": await count(ApprovalEvent, ApprovalEvent.status == "pending"),
            "request_changes": await count(ApprovalEvent, ApprovalEvent.status == "request_changes"),
            "expired": await count(ApprovalEvent, ApprovalEvent.expired.is_(True)),
        },
        evaluator={
            "pass": await count(EvaluationResultRecord, EvaluationResultRecord.verdict == "pass"),
            "review": await count(EvaluationResultRecord, EvaluationResultRecord.verdict == "review"),
        },
        slo={
            "worker_completion_latency_target_seconds": settings.worker_completion_latency_target_seconds,
            "incident_response_target_seconds": settings.incident_response_target_seconds,
        },
        finops={
            "cost_events": await count(CostEvent),
            "open_violations": await count(BudgetViolation, BudgetViolation.resolution_status == "open"),
            "budget_blocked_runs": await count(
                Incident,
                Incident.code.in_(["finops_budget_exceeded", "finops_budget_approval_required"]),
            ),
        },
    )
