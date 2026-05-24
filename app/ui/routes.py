from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_event_publisher, get_orchestrator, get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.audit import ApprovalDecisionRequest
from app.services.events import EventPublisher
from app.schemas.agent import AgentConstraints, AgentRequest
from app.services.audit_log import (
    apply_approval_decision,
    dashboard_summary,
    get_run,
    incidents_for_run,
    list_approvals,
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

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/ui/templates")


def _json_pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


templates.env.filters["json_pretty"] = _json_pretty


@router.get("/", include_in_schema=False)
async def root():
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard">'
        '<a href="/dashboard">Dashboard</a>'
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    summary = await dashboard_summary(session)
    runs = await list_runs(session, limit=8)
    incidents = await list_incidents(session, limit=5)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "summary": summary,
            "runs": runs,
            "incidents": incidents,
            "active": "dashboard",
        },
    )


@router.get("/dashboard/runs", response_class=HTMLResponse)
async def dashboard_runs(
    request: Request,
    status: str | None = None,
    task_type: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    runs = await list_runs(session, status=status or None, task_type=task_type or None, limit=100)
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "runs": runs,
            "status": status or "",
            "task_type": task_type or "",
            "active": "runs",
        },
    )


@router.get("/dashboard/runs/{run_id}", response_class=HTMLResponse)
async def dashboard_run_detail(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    incidents = await incidents_for_run(session, run_id)
    attempts = await list_worker_attempts(session, run_id=run_id)
    tool_executions = await list_tool_executions(session, run_id=run_id)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "incidents": incidents,
            "attempts": attempts,
            "tool_executions": tool_executions,
            "active": "runs",
        },
    )


@router.get("/dashboard/approvals", response_class=HTMLResponse)
async def dashboard_approvals(request: Request, session: AsyncSession = Depends(get_session)):
    approvals = await list_approvals(session)
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "approvals": approvals,
            "active": "approvals",
        },
    )


@router.get("/dashboard/worker", response_class=HTMLResponse)
async def dashboard_worker(request: Request, session: AsyncSession = Depends(get_session)):
    attempts = await list_worker_attempts(session)
    dlq = await list_worker_attempts(session, status="dlq")
    return templates.TemplateResponse(
        request,
        "worker.html",
        {
            "attempts": attempts,
            "dlq": dlq,
            "active": "worker",
        },
    )


@router.get("/dashboard/observability", response_class=HTMLResponse)
async def dashboard_observability(request: Request, session: AsyncSession = Depends(get_session)):
    summary = await observability_summary(session)
    return templates.TemplateResponse(
        request,
        "observability.html",
        {
            "summary": summary,
            "active": "observability",
        },
    )


@router.post("/dashboard/approvals/{run_id}/decision", response_class=HTMLResponse)
async def dashboard_approval_decision(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    form = await request.form()
    decision = ApprovalDecisionRequest(
        decision=str(form.get("decision", "")),
        reviewer=str(form.get("reviewer", "dashboard-operator")),
        reason=str(form.get("reason", "Dashboard decision")),
    )
    run = await apply_approval_decision(session, run_id, decision)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    await state_store.set_run_state(run.run_id, run.status)
    if run.status == "queued":
        event = make_event("agent.requested", run.run_id, {"request": run.request_payload})
        await event_publisher.publish(get_settings().kafka_request_topic, run.run_id, event)
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/approvals">'
        '<a href="/dashboard/approvals">Back to approvals</a>'
    )


@router.get("/dashboard/playground", response_class=HTMLResponse)
async def playground(request: Request):
    return templates.TemplateResponse(
        request,
        "playground.html",
        {
            "active": "playground",
            "submitted": False,
            "error": "",
            "result": None,
        },
    )


@router.post("/dashboard/playground", response_class=HTMLResponse)
async def submit_playground(
    request: Request,
    session: AsyncSession = Depends(get_session),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    form = await request.form()
    allowed_tools = [
        tool.strip()
        for tool in str(form.get("allowed_tools", "")).split(",")
        if tool.strip()
    ]
    try:
        agent_request = AgentRequest(
            user_id=str(form.get("user_id", "demo-user")),
            task_type=str(form.get("task_type", "analysis")),
            prompt=str(form.get("prompt", "")),
            goal=str(form.get("goal", "")),
            allowed_tools=allowed_tools,
            constraints=AgentConstraints(
                no_external_network=form.get("no_external_network") == "on",
                requires_human_approval=form.get("requires_human_approval") == "on",
                max_tool_calls=int(str(form.get("max_tool_calls", "8"))),
                allow_destructive_actions=form.get("allow_destructive_actions") == "on",
                allow_pii_access=form.get("allow_pii_access") == "on",
            ),
            metadata={"source": "dashboard_playground"},
        )
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
        error = ""
    except (ValueError, ValidationError) as exc:
        agent_request = None
        result = None
        error = str(exc)

    return templates.TemplateResponse(
        request,
        "playground.html",
        {
            "active": "playground",
            "submitted": result is not None,
            "error": error,
            "result": result,
            "form": form,
        },
    )
