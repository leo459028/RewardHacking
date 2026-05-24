from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.incident import Incident
from app.schemas.agent import AgentRequest
from app.schemas.audit import ApprovalDecisionRequest
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


async def persist_orchestration_result(
    session: AsyncSession,
    request: AgentRequest,
    result: OrchestrationResult,
) -> AgentRun:
    response = result.response
    run = AgentRun(
        run_id=response.run_id,
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
        evaluation_summary=result.evaluation_summary,
        result={
            "response": response.model_dump(),
            "execution_output": result.execution_output,
            "evaluation_summary": result.evaluation_summary,
        },
        requires_human_review=_requires_review(response.status, result.evaluation_summary),
    )
    session.add(run)

    incident = _incident_payload(result)
    if incident is not None:
        severity, code, detail = incident
        session.add(
            Incident(
                run_id=response.run_id,
                severity=severity,
                code=code,
                detail=detail,
            )
        )

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
    run.status = response.status
    run.decision_reason = response.decision_reason
    run.risk_score = response.risk_score
    run.policy_flags = response.policy_flags
    run.request_payload = request.model_dump()
    run.execution_output = result.execution_output
    run.evaluation_summary = result.evaluation_summary
    run.result = {
        "response": response.model_dump(),
        "execution_output": result.execution_output,
        "evaluation_summary": result.evaluation_summary,
    }
    run.requires_human_review = _requires_review(response.status, result.evaluation_summary)

    incident = _incident_payload(result)
    if incident is not None:
        severity, code, detail = incident
        session.add(
            Incident(
                run_id=response.run_id,
                severity=severity,
                code=code,
                detail=detail,
            )
        )

    await session.commit()
    await session.refresh(run)
    return run


async def list_runs(
    session: AsyncSession,
    *,
    status: str | None = None,
    task_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AgentRun]:
    stmt = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit).offset(offset)
    if status:
        stmt = stmt.where(AgentRun.status == status)
    if task_type:
        stmt = stmt.where(AgentRun.task_type == task_type)
    return list((await session.scalars(stmt)).all())


async def get_run(session: AsyncSession, run_id: str) -> AgentRun | None:
    return await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))


async def list_incidents(
    session: AsyncSession,
    *,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Incident]:
    stmt = select(Incident).order_by(Incident.created_at.desc()).limit(limit).offset(offset)
    if severity:
        stmt = stmt.where(Incident.severity == severity)
    return list((await session.scalars(stmt)).all())


async def incidents_for_run(session: AsyncSession, run_id: str) -> list[Incident]:
    stmt = select(Incident).where(Incident.run_id == run_id).order_by(Incident.created_at.desc())
    return list((await session.scalars(stmt)).all())


async def list_approvals(session: AsyncSession, *, limit: int = 100) -> list[AgentRun]:
    return await list_runs(session, status="awaiting_approval", limit=limit)


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
        session.add(
            Incident(
                run_id=run.run_id,
                severity="high",
                code="approval_denied",
                detail=run.decision_reason,
            )
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
