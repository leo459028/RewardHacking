from fastapi import APIRouter, Depends, HTTPException, Query
from prometheus_client import generate_latest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.api.deps import get_orchestrator
from sqlalchemy import text

from app.infra.postgres import get_session
from app.observability.metrics import EVALUATION_SCORE, POLICY_RISK, REQUESTS_TOTAL
from app.schemas.agent import AgentRequest, AgentResponse
from app.schemas.audit import DashboardSummaryResponse, IncidentRecord, RunRecord
from app.services.audit_log import (
    dashboard_summary,
    get_run,
    incidents_for_run,
    list_incidents,
    list_runs,
    persist_orchestration_result,
)
from app.services.orchestrator import AgentOrchestrator

router = APIRouter(prefix="/v1", tags=["agent"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(session: AsyncSession = Depends(get_session)):
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


@router.post("/agent/submit", response_model=AgentResponse)
async def submit_agent_request(
    agent_request: AgentRequest,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    session: AsyncSession = Depends(get_session),
):
    result = orchestrator.run(agent_request)
    await persist_orchestration_result(session, agent_request, result)

    REQUESTS_TOTAL.labels(status=result.response.status).inc()
    POLICY_RISK.observe(result.response.risk_score)

    return result.response


@router.post("/agent/submit-with-details")
async def submit_agent_request_with_details(
    agent_request: AgentRequest,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    session: AsyncSession = Depends(get_session),
):
    result = orchestrator.run(agent_request)
    await persist_orchestration_result(session, agent_request, result)
    score = 0.0
    if isinstance(result.evaluation_summary, dict):
        score = (
            result.evaluation_summary.get("evaluation_event", {})
            .get("payload", {})
            .get("final_score", 0.0)
        )
    EVALUATION_SCORE.observe(score)
    return {
        "response": result.response.model_dump(),
        "execution_output": result.execution_output,
        "evaluation_summary": result.evaluation_summary,
    }


@router.get("/runs", response_model=list[RunRecord])
async def get_runs(
    status: str | None = None,
    task_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    return await list_runs(
        session,
        status=status,
        task_type=task_type,
        limit=limit,
        offset=offset,
    )


@router.get("/runs/{run_id}", response_model=RunRecord)
async def get_run_by_id(
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@router.get("/incidents", response_model=list[IncidentRecord])
async def get_incidents(
    severity: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    return await list_incidents(session, severity=severity, limit=limit, offset=offset)


@router.get("/runs/{run_id}/incidents", response_model=list[IncidentRecord])
async def get_run_incidents(
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    return await incidents_for_run(session, run_id)


@router.get("/dashboard/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(session: AsyncSession = Depends(get_session)):
    summary = await dashboard_summary(session)
    return DashboardSummaryResponse(
        total_runs=summary.total_runs,
        completed=summary.completed,
        escalated=summary.escalated,
        denied=summary.denied,
        queued=summary.queued,
        average_risk_score=summary.average_risk_score,
        recent_incidents=summary.recent_incidents,
    )
