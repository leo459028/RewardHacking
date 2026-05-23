from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_orchestrator
from app.infra.postgres import get_session
from app.schemas.agent import AgentConstraints, AgentRequest
from app.services.audit_log import (
    dashboard_summary,
    get_run,
    incidents_for_run,
    list_incidents,
    list_runs,
    persist_orchestration_result,
)
from app.services.orchestrator import AgentOrchestrator

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
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "incidents": incidents,
            "active": "runs",
        },
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
        result = orchestrator.run(agent_request)
        await persist_orchestration_result(session, agent_request, result)
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
