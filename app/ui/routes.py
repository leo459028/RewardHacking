from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_control_plane_planner,
    get_event_publisher,
    get_orchestrator,
    get_state_store,
)
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.audit import ApprovalDecisionRequest
from app.services.events import EventPublisher
from app.schemas.agent import AgentConstraints, AgentRequest
from app.services.audit_log import (
    apply_approval_decision,
    dashboard_summary,
    get_run,
    governance_summary,
    incidents_for_run,
    list_approvals,
    list_governance_events,
    list_incidents,
    list_runs,
    list_tenant_api_keys,
    list_tenants,
    list_tool_executions,
    list_worker_attempts,
    observability_summary,
    persist_orchestration_result,
)
from app.services.orchestrator import AgentOrchestrator
from app.services.operations import (
    compute_operations_readiness,
    list_operations_evidence,
    list_release_gate_items,
    list_release_gates,
    persist_operations_evidence,
    persist_release_gate,
)
from app.services.control_plane import (
    ControlPlanePlanner,
    execution_plan_step_summaries,
    get_execution_plan_for_run,
    latest_execution_plans,
    list_execution_plan_steps,
    list_tool_definitions,
    list_workflow_definitions,
    persist_execution_plan,
)
from app.services.checkpoint import (
    list_replay_attempts,
    list_run_checkpoints,
    persist_run_checkpoint,
)
from app.services.finops import (
    get_pricing_item,
    list_budget_violations,
    list_pricing_items,
    record_submit_usage,
    run_cost_summary,
    tenant_cost_summary,
    update_pricing_item,
    disable_pricing_item,
)
from app.models.finops import PricingChangeLog, PricingImportBatch, PricingSourceDocument
from app.services.orchestrator import OrchestrationResult
from app.schemas.finops import PricingCatalogUpdateRequest
from app.services.pricing_import import apply_pricing_seed, dry_run_pricing_seed, load_pricing_seed
from app.services.run_state import RunStateStore
from app.workers.producer import make_event

security = HTTPBasic(auto_error=False)


def dashboard_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    settings = get_settings()
    if not settings.dashboard_auth_enabled:
        return
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "dashboard_auth_required", "message": "Dashboard auth is required."},
            headers={"WWW-Authenticate": "Basic"},
        )
    valid_user = secrets.compare_digest(credentials.username, settings.dashboard_username)
    valid_password = secrets.compare_digest(credentials.password, settings.dashboard_password)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_dashboard_credentials", "message": "Dashboard credentials are invalid."},
            headers={"WWW-Authenticate": "Basic"},
        )


router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth)])
templates = Jinja2Templates(directory="app/ui/templates")


def _json_pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


templates.env.filters["json_pretty"] = _json_pretty


async def _attach_execution_plan(
    session: AsyncSession,
    *,
    planner: ControlPlanePlanner,
    agent_request: AgentRequest,
    result: OrchestrationResult,
) -> None:
    plan = planner.plan(
        request=agent_request,
        run_id=result.response.run_id,
        policy_snapshot=dict(result.evaluation_summary.get("policy", {})),
        trace_id=str(agent_request.metadata.get("trace_id", "")),
        workflows=await list_workflow_definitions(session),
        tools=await list_tool_definitions(session),
    )
    result.evaluation_summary["execution_plan"] = plan.model_dump()
    if result.response.status == "queued" and plan.status != "planned":
        result.response.status = "awaiting_approval"
        result.response.decision_reason = "Control plane execution plan requires approval."
        if "control_plane_plan_requires_approval" not in result.response.policy_flags:
            result.response.policy_flags.append("control_plane_plan_requires_approval")
        result.evaluation_summary["run_state"] = {"status": "awaiting_approval"}
        result.evaluation_summary["approval"] = {
            "status": "pending",
            "reason": result.response.decision_reason,
        }
    await persist_execution_plan(session, plan)


@router.get("/", include_in_schema=False)
async def root():
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard">'
        '<a href="/dashboard">Dashboard</a>'
    )


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return HTMLResponse("", status_code=204)


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
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    runs = await list_runs(
        session,
        status=status or None,
        task_type=task_type or None,
        tenant_id=tenant_id or None,
        limit=100,
    )
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "runs": runs,
            "status": status or "",
            "task_type": task_type or "",
            "tenant_id": tenant_id or "",
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
    execution_plan = await get_execution_plan_for_run(session, run_id)
    execution_plan_steps = await list_execution_plan_steps(session, run_id)
    checkpoints = await list_run_checkpoints(session, run_id=run_id)
    replay_attempts = await list_replay_attempts(session, run_id=run_id)
    cost_summary = await run_cost_summary(session, run_id=run_id)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "incidents": incidents,
            "attempts": attempts,
            "tool_executions": tool_executions,
            "execution_plan": execution_plan,
            "execution_plan_steps": execution_plan_steps,
            "checkpoints": checkpoints,
            "replay_attempts": replay_attempts,
            "cost_summary": cost_summary,
            "active": "runs",
        },
    )


@router.get("/dashboard/control-plane", response_class=HTMLResponse)
async def dashboard_control_plane(request: Request, session: AsyncSession = Depends(get_session)):
    workflows = await list_workflow_definitions(session)
    tools = await list_tool_definitions(session)
    plans = await latest_execution_plans(session, limit=25)
    step_summaries = await execution_plan_step_summaries(session, [plan.run_id for plan in plans])
    return templates.TemplateResponse(
        request,
        "control_plane.html",
        {
            "workflows": workflows,
            "tools": tools,
            "plans": plans,
            "step_summaries": step_summaries,
            "active": "control-plane",
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


@router.get("/dashboard/governance", response_class=HTMLResponse)
async def dashboard_governance(
    request: Request,
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    runs = await list_runs(session, tenant_id=tenant_id or None, limit=100)
    tenants = await list_tenants(session)
    tenant_ids = [tenant.tenant_id for tenant in tenants]
    if not tenant_ids:
        tenant_ids = sorted({run.tenant_id for run in await list_runs(session, limit=200)})
    selected_tenant = tenant_id or (tenant_ids[0] if tenant_ids else "default")
    summary = await governance_summary(session, tenant_id=selected_tenant)
    events = await list_governance_events(session, tenant_id=tenant_id or None, limit=25)
    api_keys = await list_tenant_api_keys(session, tenant_id=selected_tenant)
    return templates.TemplateResponse(
        request,
        "governance.html",
        {
            "summary": summary,
            "events": events,
            "runs": runs,
            "tenants": tenants,
            "api_keys": api_keys,
            "tenant_ids": tenant_ids,
            "tenant_id": tenant_id or "",
            "active": "governance",
        },
    )


@router.get("/dashboard/costs", response_class=HTMLResponse)
async def dashboard_costs(
    request: Request,
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    selected_tenant = tenant_id or "default"
    summary = await tenant_cost_summary(session, tenant_id=selected_tenant)
    pricing = await list_pricing_items(session)
    violations = await list_budget_violations(session, tenant_id=selected_tenant, limit=25)
    return templates.TemplateResponse(
        request,
        "costs.html",
        {
            "summary": summary,
            "pricing": pricing,
            "violations": violations,
            "tenant_id": selected_tenant,
            "active": "costs",
        },
    )


@router.get("/dashboard/admin/finops/pricing", response_class=HTMLResponse)
async def dashboard_admin_finops_pricing(
    request: Request,
    provider: str | None = None,
    model: str | None = None,
    resource_type: str | None = None,
    pricing_mode: str | None = None,
    token_type: str | None = None,
    active_only: bool = False,
    scope_type: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    pricing = await list_pricing_items(
        session,
        active_only=active_only,
        provider=provider or None,
        model_name=model or None,
        resource_type=resource_type or None,
        pricing_mode=pricing_mode or None,
        token_type=token_type or None,
        scope_type=scope_type or None,
    )
    return templates.TemplateResponse(
        request,
        "admin_finops_pricing.html",
        {
            "pricing": pricing,
            "filters": {
                "provider": provider or "",
                "model": model or "",
                "resource_type": resource_type or "",
                "pricing_mode": pricing_mode or "",
                "token_type": token_type or "",
                "active_only": active_only,
                "scope_type": scope_type or "",
            },
            "active": "admin-finops",
        },
    )


@router.get("/dashboard/admin/finops/pricing/import", response_class=HTMLResponse)
async def dashboard_admin_finops_import(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    batches = list(
        (
            await session.scalars(
                select(PricingImportBatch).order_by(PricingImportBatch.created_at.desc())
            )
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "admin_finops_import.html",
        {
            "file_path": "app/seeds/model_pricing_seed.yaml",
            "report": None,
            "batches": batches,
            "active": "admin-finops",
        },
    )


@router.post("/dashboard/admin/finops/pricing/import", response_class=HTMLResponse)
async def dashboard_admin_finops_import_post(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    file_path = str(form.get("file_path", "app/seeds/model_pricing_seed.yaml"))
    mode = str(form.get("mode", "dry_run"))
    seed = load_pricing_seed(file_path)
    if mode == "apply":
        report = await apply_pricing_seed(session, seed, created_by="dashboard")
    else:
        report = await dry_run_pricing_seed(session, seed, created_by="dashboard")
    batches = list(
        (
            await session.scalars(
                select(PricingImportBatch).order_by(PricingImportBatch.created_at.desc())
            )
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "admin_finops_import.html",
        {
            "file_path": file_path,
            "report": report,
            "batches": batches,
            "active": "admin-finops",
        },
    )


@router.get("/dashboard/admin/finops/pricing/sources", response_class=HTMLResponse)
async def dashboard_admin_finops_sources(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    sources = list(
        (
            await session.scalars(
                select(PricingSourceDocument).order_by(PricingSourceDocument.created_at.desc())
            )
        ).all()
    )
    changes = list(
        (
            await session.scalars(
                select(PricingChangeLog).order_by(PricingChangeLog.changed_at.desc())
            )
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "admin_finops_sources.html",
        {
            "sources": sources,
            "changes": changes,
            "active": "admin-finops",
        },
    )


@router.get("/dashboard/admin/finops/pricing/{pricing_id}", response_class=HTMLResponse)
async def dashboard_admin_finops_pricing_detail(
    request: Request,
    pricing_id: str,
    session: AsyncSession = Depends(get_session),
):
    item = await get_pricing_item(session, pricing_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pricing item not found.")
    changes = list(
        (
            await session.scalars(
                select(PricingChangeLog)
                .where(
                    (PricingChangeLog.pricing_id_old == pricing_id)
                    | (PricingChangeLog.pricing_id_new == pricing_id)
                )
                .order_by(PricingChangeLog.changed_at.desc())
            )
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "admin_finops_pricing_detail.html",
        {
            "item": item,
            "changes": changes,
            "active": "admin-finops",
        },
    )


@router.post("/dashboard/admin/finops/pricing/{pricing_id}/edit", response_class=HTMLResponse)
async def dashboard_admin_finops_pricing_edit(
    request: Request,
    pricing_id: str,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    await update_pricing_item(
        session,
        pricing_id,
        PricingCatalogUpdateRequest(
            unit_price=str(form.get("unit_price", "")),
            currency=str(form.get("currency", "USD")),
            effective_from=str(form.get("effective_from", "")) or None,
            effective_to=str(form.get("effective_to", "")) or None,
            is_active=str(form.get("is_active", "")) == "on",
            change_reason=str(form.get("change_reason", "")),
            updated_by="dashboard",
        ),
    )
    return RedirectResponse(f"/dashboard/admin/finops/pricing/{pricing_id}", status_code=303)


@router.post("/dashboard/admin/finops/pricing/{pricing_id}/disable", response_class=HTMLResponse)
async def dashboard_admin_finops_pricing_disable(
    pricing_id: str,
    session: AsyncSession = Depends(get_session),
):
    await disable_pricing_item(session, pricing_id, updated_by="dashboard")
    return RedirectResponse(f"/dashboard/admin/finops/pricing/{pricing_id}", status_code=303)


@router.get("/dashboard/operations", response_class=HTMLResponse)
async def dashboard_operations(request: Request, session: AsyncSession = Depends(get_session)):
    readiness = await compute_operations_readiness(session, settings=get_settings())
    evidence = await list_operations_evidence(session, limit=25)
    gates = await list_release_gates(session, limit=10)
    gate_items = {}
    for gate in gates:
        gate_items[gate.gate_id] = await list_release_gate_items(session, gate.gate_id)
    return templates.TemplateResponse(
        request,
        "operations.html",
        {
            "readiness": readiness,
            "evidence": evidence,
            "gates": gates,
            "gate_items": gate_items,
            "active": "operations",
        },
    )


@router.post("/dashboard/operations/evidence", response_class=HTMLResponse)
async def dashboard_operations_evidence(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    await persist_operations_evidence(
        session,
        evidence_type=str(form.get("evidence_type", "")),
        status=str(form.get("status", "pass")),
        summary=str(form.get("summary", "")),
        reference_url=str(form.get("reference_url", "")),
        metadata={"source": "dashboard"},
        created_by="dashboard",
    )
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/operations">'
        '<a href="/dashboard/operations">Back to operations</a>'
    )


@router.post("/dashboard/operations/release-gates", response_class=HTMLResponse)
async def dashboard_operations_release_gate(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    readiness = await compute_operations_readiness(session, settings=get_settings())
    await persist_release_gate(
        session,
        release_version=str(form.get("release_version", "v1.5.2")),
        created_by=str(form.get("created_by", "dashboard")),
        trace_id=str(getattr(request.state, "trace_id", "")),
        readiness=readiness,
    )
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/operations">'
        '<a href="/dashboard/operations">Back to operations</a>'
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
    planner: ControlPlanePlanner = Depends(get_control_plane_planner),
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
            tenant_id=str(form.get("tenant_id", "default")) or "default",
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
            metadata={
                "source": "dashboard_playground",
                "trace_id": str(getattr(request.state, "trace_id", "")),
            },
        )
        disabled_tools = list((await state_store.get_open_breakers(agent_request.allowed_tools)).keys())
        result = orchestrator.submit(agent_request, disabled_tools=disabled_tools)
        await _attach_execution_plan(
            session,
            planner=planner,
            agent_request=agent_request,
            result=result,
        )
        await persist_orchestration_result(session, agent_request, result)
        run = await get_run(session, result.response.run_id)
        plan = await get_execution_plan_for_run(session, result.response.run_id)
        if run is not None:
            await persist_run_checkpoint(
                session,
                run=run,
                plan=plan,
                steps=await list_execution_plan_steps(session, result.response.run_id),
                checkpoint_type="submit",
                resume_cursor="worker_queue" if run.status == "queued" else "approval_queue",
                created_by="dashboard",
            )
            await record_submit_usage(session, run=run, plan=plan)
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
