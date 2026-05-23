from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.incident import Incident
from app.schemas.agent import AgentRequest
from app.services.orchestrator import OrchestrationResult


@dataclass(frozen=True)
class DashboardSummary:
    total_runs: int
    completed: int
    escalated: int
    denied: int
    queued: int
    average_risk_score: float
    recent_incidents: list[Incident]


def _requires_review(status: str, summary: dict[str, Any]) -> bool:
    if status in {"denied", "escalated", "queued"}:
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
        average_risk_score=round(float(average_risk_score), 3),
        recent_incidents=await list_incidents(session, limit=5),
    )
