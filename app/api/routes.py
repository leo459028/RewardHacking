from fastapi import APIRouter, Depends, HTTPException, Query
from prometheus_client import generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.api.deps import get_event_publisher, get_orchestrator, get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.observability.metrics import (
    EVALUATION_SCORE,
    EVALUATOR_VERDICTS_TOTAL,
    POLICY_DECISIONS_TOTAL,
    POLICY_RISK,
    REQUESTS_TOTAL,
)
from app.schemas.agent import AgentRequest, AgentResponse
from app.schemas.audit import (
    ApprovalDecisionRequest,
    DashboardSummaryResponse,
    EvaluationRecord,
    IncidentRecord,
    RunRecord,
    ToolExecutionRecord,
    WorkerAttemptRecord,
)
from app.services.events import EventPublisher
from app.services.audit_log import (
    apply_approval_decision,
    dashboard_summary,
    get_run,
    incidents_for_run,
    list_approvals,
    list_evaluation_results,
    list_incidents,
    list_runs,
    list_tool_executions,
    list_worker_attempts,
    observability_summary,
    persist_orchestration_result,
)
from app.services.orchestrator import AgentOrchestrator
from app.services.run_state import RunStateStore
from app.workers.producer import make_event

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
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    disabled_tools = list((await state_store.get_open_breakers(agent_request.allowed_tools)).keys())
    result = orchestrator.submit(agent_request, disabled_tools=disabled_tools)
    await persist_orchestration_result(session, agent_request, result)
    await state_store.set_run_state(result.response.run_id, result.response.status)
    if result.response.status == "queued":
        event = make_event(
            "agent.requested",
            result.response.run_id,
            {"request": agent_request.model_dump()},
        )
        await event_publisher.publish(
            get_settings().kafka_request_topic,
            result.response.run_id,
            event,
        )

    REQUESTS_TOTAL.labels(status=result.response.status).inc()
    POLICY_DECISIONS_TOTAL.labels(status=result.response.status).inc()
    POLICY_RISK.observe(result.response.risk_score)

    return result.response


@router.post("/agent/submit-with-details")
async def submit_agent_request_with_details(
    agent_request: AgentRequest,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    disabled_tools = list((await state_store.get_open_breakers(agent_request.allowed_tools)).keys())
    result = orchestrator.submit(agent_request, disabled_tools=disabled_tools)
    await persist_orchestration_result(session, agent_request, result)
    await state_store.set_run_state(result.response.run_id, result.response.status)
    if result.response.status == "queued":
        event = make_event(
            "agent.requested",
            result.response.run_id,
            {"request": agent_request.model_dump()},
        )
        await event_publisher.publish(
            get_settings().kafka_request_topic,
            result.response.run_id,
            event,
        )
    REQUESTS_TOTAL.labels(status=result.response.status).inc()
    POLICY_DECISIONS_TOTAL.labels(status=result.response.status).inc()
    POLICY_RISK.observe(result.response.risk_score)
    score = 0.0
    if isinstance(result.evaluation_summary, dict):
        payload = result.evaluation_summary.get("evaluation_event", {}).get("payload", {})
        score = payload.get("final_score", 0.0)
        if payload.get("verdict"):
            EVALUATOR_VERDICTS_TOTAL.labels(verdict=payload["verdict"]).inc()
    EVALUATION_SCORE.observe(score)
    return {
        "response": result.response.model_dump(),
        "execution_output": result.execution_output,
        "evaluation_summary": result.evaluation_summary,
    }


@router.get("/approvals", response_model=list[RunRecord])
async def get_approvals(
    status: str | None = None,
    reviewer: str | None = None,
    expired: bool | None = None,
    session: AsyncSession = Depends(get_session),
):
    return await list_approvals(session, status=status, reviewer=reviewer, expired=expired)


@router.post("/approvals/{run_id}/decision", response_model=RunRecord)
async def decide_approval(
    run_id: str,
    decision: ApprovalDecisionRequest,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    run = await apply_approval_decision(session, run_id, decision)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    await state_store.set_run_state(run.run_id, run.status)
    if run.status == "queued":
        event = make_event("agent.requested", run.run_id, {"request": run.request_payload})
        await event_publisher.publish(get_settings().kafka_request_topic, run.run_id, event)
    return run


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


@router.get("/runs/{run_id}/worker-attempts", response_model=list[WorkerAttemptRecord])
async def get_run_worker_attempts(
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    return await list_worker_attempts(session, run_id=run_id)


@router.get("/runs/{run_id}/tool-executions", response_model=list[ToolExecutionRecord])
async def get_run_tool_executions(
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    return await list_tool_executions(session, run_id=run_id)


@router.get("/worker/dlq", response_model=list[WorkerAttemptRecord])
async def get_worker_dlq(session: AsyncSession = Depends(get_session)):
    return await list_worker_attempts(session, status="dlq")


@router.get("/evaluations", response_model=list[EvaluationRecord])
async def get_evaluations(
    task_type: str | None = None,
    verdict: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    return await list_evaluation_results(session, task_type=task_type, verdict=verdict)


@router.get("/observability/summary")
async def get_observability_summary(session: AsyncSession = Depends(get_session)):
    summary = await observability_summary(session)
    return {
        "worker": summary.worker,
        "incidents": summary.incidents,
        "approvals": summary.approvals,
        "evaluator": summary.evaluator,
        "slo": summary.slo,
    }


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
        running=summary.running,
        awaiting_approval=summary.awaiting_approval,
        failed=summary.failed,
        average_risk_score=summary.average_risk_score,
        recent_incidents=summary.recent_incidents,
    )
