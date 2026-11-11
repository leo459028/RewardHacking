from __future__ import annotations

import json
import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
from app.services.auth import generate_api_key, hash_api_key
from app.services.audit_log import (
    ApprovalStateError,
    apply_approval_decision,
    create_tenant,
    create_tenant_api_key,
    dashboard_summary,
    expire_stale_approvals,
    get_run,
    get_tenant,
    get_tenant_api_key,
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
    persist_governance_event,
    persist_orchestration_result,
    requeue_dlq_run,
    resolve_dlq_run,
    revoke_tenant_api_key,
    update_tenant,
)
from app.schemas.control_plane import McpServerCreateRequest
from app.services.mcp_gateway import (
    get_mcp_server_map,
    list_mcp_servers,
    persist_preflight_audit as persist_mcp_preflight_audit,
    preflight as mcp_preflight,
    upsert_mcp_server,
)
from app.services.orchestrator import AgentOrchestrator
from app.services.quota import (
    check_high_risk_quota,
    check_request_quota,
    check_tool_quotas,
    check_user_request_quota,
)
from app.services.control_plane import (
    ControlPlanePlanner,
    create_tool_definition,
    create_workflow_definition,
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
    persist_replay_attempt,
    persist_run_checkpoint,
    resume_run_from_checkpoint,
)
from app.services.finops import (
    acknowledge_budget_violation,
    acquire_run_budget_lease,
    create_budget_policy,
    create_cost_adjustment,
    disable_budget_policy,
    disable_pricing_item,
    export_tenant_cost_ledger,
    format_decimal_plain,
    get_budget_policy,
    get_budget_violation,
    get_pricing_item,
    list_budget_policies,
    list_budget_violations,
    list_pricing_items,
    list_tenant_token_leases,
    record_checkpoint_usage,
    record_replay_usage,
    record_submit_usage,
    release_run_budget_lease,
    render_cost_ledger_csv,
    resolve_budget_violation,
    run_cost_summary,
    tenant_cost_summary,
    update_pricing_item,
)
from app.models.finops import PricingChangeLog, PricingImportBatch, PricingSourceDocument
from app.services.orchestrator import OrchestrationResult
from app.schemas.finops import (
    BudgetPolicyCreateRequest,
    CostAdjustmentCreateRequest,
    PricingCatalogUpdateRequest,
)
from app.services.pricing_import import apply_pricing_seed, dry_run_pricing_seed, load_pricing_seed
from app.services.run_state import RunStateStore
from app.services.access.permissions import PLATFORM_VIEW
from app.services.access.principal import Principal
from app.ui.security import (
    actor_for,
    dashboard_principal,
    ensure_permission,
    ensure_tenant_access,
    install_template_globals,
    needs,
    principal_of,
    scoped_tenant,
)
from app.workers.producer import make_event


async def dashboard_auth(principal: Principal = Depends(dashboard_principal)) -> Principal:
    """v1.6.1: kept under its old name for the routers that import it; the identity logic lives in
    ``app.ui.security.dashboard_principal`` (HTTP Basic Auth was removed, plan D9 / Q1)."""
    return principal


router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth)])
templates = Jinja2Templates(directory="app/ui/templates")


def _json_pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


templates.env.filters["json_pretty"] = _json_pretty
templates.env.filters["decimal_plain"] = format_decimal_plain
install_template_globals(templates)


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


@router.get("/", include_in_schema=False, dependencies=needs("dashboard.view"))
async def root():
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard">'
        '<a href="/dashboard">Dashboard</a>'
    )


@router.get("/favicon.ico", include_in_schema=False, dependencies=needs("dashboard.view"))
async def favicon():
    return HTMLResponse("", status_code=204)


@router.get("/dashboard", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="overview"))
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    own_tenant = await scoped_tenant(request, session, None)
    summary = await dashboard_summary(session, tenant_id=own_tenant)
    runs = await list_runs(session, tenant_id=own_tenant, limit=8)
    incidents = await list_incidents(session, tenant_id=own_tenant, limit=5)
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


@router.get("/dashboard/runs", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="runs"))
async def dashboard_runs(
    request: Request,
    status: str | None = None,
    task_type: str | None = None,
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    tenant_id = await scoped_tenant(request, session, tenant_id)
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


@router.get("/dashboard/runs/{run_id}", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="run_detail"))
async def dashboard_run_detail(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    from app.core import tracing
    from app.services import retention

    run = await get_run(session, run_id)
    if run is None:
        # v1.6.0 D17: a purged run renders its tombstone with 200 (the API answers 410 run_purged).
        tombstone = await retention.tombstone_for(session, run_id)
        if tombstone is None:
            if not principal_of(request).is_platform:
                await ensure_tenant_access(request, session, None)  # no existence oracle for tenant-bound users
            raise HTTPException(status_code=404, detail="Run not found.")
        await ensure_tenant_access(request, session, tombstone.tenant_id)
        return templates.TemplateResponse(
            request, "run_purged.html", {"tombstone": retention.tombstone_summary(tombstone), "active": "runs"}
        )
    await ensure_tenant_access(request, session, run.tenant_id)
    incidents = await incidents_for_run(session, run_id)
    attempts = await list_worker_attempts(session, run_id=run_id)
    tool_executions = await list_tool_executions(session, run_id=run_id)
    execution_plan = await get_execution_plan_for_run(session, run_id)
    execution_plan_steps = await list_execution_plan_steps(session, run_id)
    checkpoints = await list_run_checkpoints(session, run_id=run_id)
    replay_attempts = await list_replay_attempts(session, run_id=run_id)
    cost_summary = await run_cost_summary(session, run_id=run_id)
    from app.services.outcomes import list_outcomes
    from app.services.review_queue import list_reviews

    outcomes = await list_outcomes(session, run_id)
    run_reviews = await list_reviews(session, run_id=run_id, limit=20)
    executor_eval = dict((run.evaluation_summary or {}).get("evaluation_event", {}).get("payload", {}) or {})
    latest_outcome = outcomes[-1] if outcomes else None
    trace_id = (execution_plan.trace_id if execution_plan else "") or (run.request_payload or {}).get("metadata", {}).get("trace_id", "")
    legal_holds = [retention.hold_to_dict(hold) for hold in await retention.active_holds_for_run(session, run.run_id, run.tenant_id)]
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "outcomes": outcomes,
            "latest_outcome": latest_outcome,
            "run_reviews": run_reviews,
            "executor_eval": executor_eval,
            "outcome_max_submissions": get_settings().evaluation_outcome_max_submissions,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
            "incidents": incidents,
            "attempts": attempts,
            "tool_executions": tool_executions,
            "execution_plan": execution_plan,
            "execution_plan_steps": execution_plan_steps,
            "checkpoints": checkpoints,
            "replay_attempts": replay_attempts,
            "cost_summary": cost_summary,
            "trace_id": trace_id,
            "trace_ui_url": tracing.trace_ui_url(get_settings(), trace_id),
            "legal_holds": legal_holds,
            "active": "runs",
        },
    )


@router.get("/dashboard/control-plane", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="control_plane"))
async def dashboard_control_plane(
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    workflows = await list_workflow_definitions(session)
    tools = await list_tool_definitions(session)
    plans = await latest_execution_plans(session, limit=25)
    step_summaries = await execution_plan_step_summaries(session, [plan.run_id for plan in plans])
    open_breakers = await state_store.list_open_breakers()
    mcp_servers = await list_mcp_servers(session, get_settings())
    open_mcp_breakers = await state_store.list_open_mcp_breakers()
    return templates.TemplateResponse(
        request,
        "control_plane.html",
        {
            "mcp_servers": mcp_servers,
            "open_mcp_breakers": open_mcp_breakers,
            "control_plane_error": str(request.query_params.get("error", ""))[:300],
            "workflows": workflows,
            "tools": tools,
            "plans": plans,
            "step_summaries": step_summaries,
            "open_breakers": open_breakers,
            "active": "control-plane",
        },
    )


@router.post("/dashboard/control-plane/workflows", response_class=HTMLResponse, dependencies=needs("control_plane.manage", view="control_plane"))
async def dashboard_control_plane_workflow_upsert(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    workflow_id = str(form.get("workflow_id", "")).strip() or "custom-workflow"
    version = str(form.get("version", "1.0")).strip() or "1.0"
    task_type = str(form.get("task_type", "analysis")).strip() or "analysis"
    if task_type not in {"code_fix", "analysis", "data_query", "workflow", "other"}:
        task_type = "other"
    display_name = str(form.get("display_name", "")).strip() or workflow_id
    status = str(form.get("status", "active")).strip() or "active"
    raw_steps = str(form.get("steps", "policy_check,tool_execution,evaluation"))
    steps = [s.strip() for s in raw_steps.split(",") if s.strip()] or [
        "policy_check",
        "tool_execution",
        "evaluation",
    ]
    await create_workflow_definition(
        session,
        workflow_id=workflow_id,
        version=version,
        task_type=task_type,
        display_name=display_name,
        status=status,
        definition={"steps": steps},
    )
    return RedirectResponse("/dashboard/control-plane", status_code=303)


@router.post("/dashboard/control-plane/tools", response_class=HTMLResponse, dependencies=needs("control_plane.manage", view="control_plane"))
async def dashboard_control_plane_tool_upsert(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    tool_name = str(form.get("tool_name", "")).strip() or "custom_tool"
    display_name = str(form.get("display_name", "")).strip() or tool_name
    status = str(form.get("status", "active")).strip() or "active"
    raw_task_types = str(form.get("task_types", "analysis"))
    task_types = [t.strip() for t in raw_task_types.split(",") if t.strip()] or ["analysis"]
    risk_level = str(form.get("risk_level", "low")).strip() or "low"
    requires_approval = str(form.get("requires_approval", "")).lower() in {"on", "true", "1", "yes"}
    quota_str = str(form.get("quota_per_minute", "")).strip()
    metadata: dict[str, object] = {"source": "dashboard"}
    mcp_server_id = str(form.get("mcp_server_id", "")).strip()
    if mcp_server_id:
        metadata["mcp_server_id"] = mcp_server_id
    required_args = [a.strip() for a in str(form.get("required_args", "")).split(",") if a.strip()]
    sensitive_args = [a.strip() for a in str(form.get("sensitive_args", "")).split(",") if a.strip()]
    if required_args or sensitive_args:
        metadata["input_schema"] = {
            "type": "object",
            "required": required_args,
            "properties": {name: {"type": "string", "maxLength": 4096} for name in dict.fromkeys(required_args + sensitive_args)},
            "additionalProperties": True,
        }
    if sensitive_args:
        metadata["sensitive_fields"] = sensitive_args
    if quota_str:
        try:
            q_val = int(quota_str)
            if q_val > 0:
                metadata["quota_per_minute"] = q_val
        except ValueError:
            pass
    try:
        await create_tool_definition(
            session,
            tool_name=tool_name,
            display_name=display_name,
            status=status,
            task_types=task_types,
            requires_approval=requires_approval,
            risk_level=risk_level,
            metadata=metadata,
        )
    except ValueError as exc:
        return RedirectResponse(f"/dashboard/control-plane?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/dashboard/control-plane", status_code=303)


@router.post("/dashboard/control-plane/mcp-servers", response_class=HTMLResponse, dependencies=needs("control_plane.manage", view="control_plane"))
async def dashboard_control_plane_mcp_server_upsert(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    raw_quota = str(form.get("quota_per_minute", "60")).strip() or "60"
    allowed_tenants = [t.strip() for t in str(form.get("allowed_tenants", "")).split(",") if t.strip()]
    try:
        payload = McpServerCreateRequest(
            mcp_server_id=str(form.get("mcp_server_id", "")).strip(),
            display_name=str(form.get("display_name", "")).strip(),
            transport=str(form.get("transport", "stdio")).strip() or "stdio",
            endpoint=str(form.get("endpoint", "local://stdio")).strip() or "local://stdio",
            trust_tier=str(form.get("trust_tier", "verified")).strip() or "verified",
            status=str(form.get("status", "active")).strip() or "active",
            requires_approval=str(form.get("requires_approval", "")).lower() in {"on", "true", "1", "yes"},
            risk_level=str(form.get("risk_level", "low")).strip() or "low",
            quota_per_minute=int(raw_quota),
            allowed_tenants=allowed_tenants,
            metadata={"source": "dashboard"},
        )
    except (ValueError, ValidationError) as exc:
        message = str(exc).splitlines()[0] if str(exc) else "Invalid MCP server payload."
        return RedirectResponse(f"/dashboard/control-plane?error={quote(message)}", status_code=303)
    server = await upsert_mcp_server(session, payload.model_dump(), get_settings())
    await persist_governance_event(
        session,
        tenant_id="default",
        user_id=actor_for(request, "dashboard-admin"),
        event_type="mcp_server.upserted",
        decision="recorded",
        reason=f"MCP server {server['mcp_server_id']} upserted from Control Plane Dashboard.",
        details={"mcp_server_id": server["mcp_server_id"], "trust_tier": server["trust_tier"], "source": "dashboard"},
    )
    return RedirectResponse("/dashboard/control-plane", status_code=303)


@router.post("/dashboard/control-plane/mcp-servers/{mcp_server_id}/breaker/open", response_class=HTMLResponse, dependencies=needs("breakers.manage", view="control_plane"))
async def dashboard_control_plane_mcp_breaker_open(
    mcp_server_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    form = await request.form()
    reason = str(form.get("reason", "")).strip() or "Manual MCP circuit breaker trip from Control Plane Dashboard"
    server_map = await get_mcp_server_map(session, get_settings())
    if mcp_server_id not in server_map:
        return RedirectResponse(f"/dashboard/control-plane?error={quote('MCP server not found.')}", status_code=303)
    await state_store.open_mcp_breaker(mcp_server_id, reason=reason, ttl_seconds=get_settings().tool_breaker_ttl_seconds)
    await persist_governance_event(
        session,
        tenant_id="default",
        user_id=actor_for(request, "dashboard-admin"),
        event_type="mcp_breaker.opened",
        decision="tripped",
        reason=reason,
        details={"mcp_server_id": mcp_server_id, "source": "dashboard"},
    )
    return RedirectResponse("/dashboard/control-plane", status_code=303)


@router.post("/dashboard/control-plane/mcp-servers/{mcp_server_id}/breaker/close", response_class=HTMLResponse, dependencies=needs("breakers.manage", view="control_plane"))
async def dashboard_control_plane_mcp_breaker_close(
    mcp_server_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    form = await request.form()
    reason = str(form.get("reason", "")).strip() or "Manual MCP circuit breaker reset from Control Plane Dashboard"
    await state_store.close_mcp_breaker(mcp_server_id)
    await persist_governance_event(
        session,
        tenant_id="default",
        user_id=actor_for(request, "dashboard-admin"),
        event_type="mcp_breaker.closed",
        decision="reset",
        reason=reason,
        details={"mcp_server_id": mcp_server_id, "source": "dashboard"},
    )
    return RedirectResponse("/dashboard/control-plane", status_code=303)


@router.post("/dashboard/control-plane/tools/{tool_name}/breaker/open", response_class=HTMLResponse, dependencies=needs("breakers.manage", view="control_plane"))
async def dashboard_control_plane_breaker_open(
    tool_name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    form = await request.form()
    reason = str(form.get("reason", "Manual circuit breaker trip from Control Plane Dashboard")).strip() or "Manual circuit breaker trip"
    settings = get_settings()
    await state_store.open_breaker(
        tool_name,
        reason=reason,
        ttl_seconds=settings.tool_breaker_ttl_seconds,
    )
    await persist_governance_event(
        session,
        tenant_id="default",
        user_id=actor_for(request, "dashboard-admin"),
        event_type="tool_breaker.opened",
        decision="tripped",
        reason=reason,
        details={"tool_name": tool_name, "source": "dashboard"},
    )
    return RedirectResponse("/dashboard/control-plane", status_code=303)


@router.post("/dashboard/control-plane/tools/{tool_name}/breaker/close", response_class=HTMLResponse, dependencies=needs("breakers.manage", view="control_plane"))
async def dashboard_control_plane_breaker_close(
    tool_name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    form = await request.form()
    reason = str(form.get("reason", "Manual circuit breaker reset from Control Plane Dashboard")).strip() or "Manual circuit breaker reset"
    await state_store.close_breaker(tool_name)
    await persist_governance_event(
        session,
        tenant_id="default",
        user_id=actor_for(request, "dashboard-admin"),
        event_type="tool_breaker.closed",
        decision="reset",
        reason=reason,
        details={"tool_name": tool_name, "source": "dashboard"},
    )
    return RedirectResponse("/dashboard/control-plane", status_code=303)


@router.get("/dashboard/approvals", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="approvals"))
async def dashboard_approvals(request: Request, session: AsyncSession = Depends(get_session)):
    approvals = await list_approvals(session, tenant_id=await scoped_tenant(request, session, None))
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "approvals": approvals,
            "active": "approvals",
        },
    )


@router.get("/dashboard/worker", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="worker"))
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


@router.get("/dashboard/governance", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="governance"))
async def dashboard_governance(
    request: Request,
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    from app.services.access.permissions import has_permission

    principal = principal_of(request)
    tenant_id = await scoped_tenant(request, session, tenant_id)
    runs = await list_runs(session, tenant_id=tenant_id or None, limit=100)
    tenants = await list_tenants(session)
    if not principal.is_platform:
        tenants = [tenant for tenant in tenants if tenant.tenant_id == principal.tenant_id]
    tenant_ids = [tenant.tenant_id for tenant in tenants]
    if not tenant_ids:
        tenant_ids = sorted({run.tenant_id for run in await list_runs(session, tenant_id=tenant_id or None, limit=200)})
    selected_tenant = tenant_id or (tenant_ids[0] if tenant_ids else "default")
    summary = await governance_summary(session, tenant_id=selected_tenant)
    events = await list_governance_events(session, tenant_id=tenant_id or None, limit=25)
    # API parity: listing API keys is admin only (``api_keys.view`` adds the read-only auditor, D21).
    api_keys = await list_tenant_api_keys(session, tenant_id=selected_tenant) if has_permission(principal.role, "api_keys.view") else []
    from app.services.review_queue import get_evaluation_policy, policy_to_dict
    from app.ui.routers.reviews import list_evaluation_policies

    evaluation_policies = [
        policy for policy in await list_evaluation_policies(session) if principal.is_platform or policy.tenant_id == principal.tenant_id
    ]
    selected_policy = policy_to_dict(selected_tenant, await get_evaluation_policy(session, selected_tenant))
    return templates.TemplateResponse(
        request,
        "governance.html",
        {
            "evaluation_policies": evaluation_policies,
            "selected_policy": selected_policy,
            "governance_message": request.query_params.get("message", ""),
            "summary": summary,
            "events": events,
            "runs": runs,
            "tenants": tenants,
            "api_keys": api_keys,
            "tenant_ids": tenant_ids,
            "tenant_id": tenant_id or "",
            "selected_tenant": selected_tenant,
            "newly_created_key": "",
            "newly_created_key_name": "",
            "active": "governance",
        },
    )


@router.post("/dashboard/governance/tenants", response_class=HTMLResponse, dependencies=needs("tenants.manage", view=PLATFORM_VIEW))
async def dashboard_governance_tenant_upsert(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    display_name = str(form.get("display_name", "")).strip() or tenant_id
    status = str(form.get("status", "active")).strip() or "active"
    try:
        req_limit = max(1, int(str(form.get("request_limit_per_minute", "60")).strip() or "60"))
    except ValueError:
        req_limit = 60
    try:
        hr_limit = max(1, int(str(form.get("high_risk_limit_per_hour", "10")).strip() or "10"))
    except ValueError:
        hr_limit = 10
    if status != "active":
        await ensure_permission(request, session, "tenants.disable")

    existing = await get_tenant(session, tenant_id)
    if existing is None:
        await create_tenant(
            session,
            tenant_id=tenant_id,
            display_name=display_name,
            request_limit_per_minute=req_limit,
            high_risk_limit_per_hour=hr_limit,
        )
        if status != "active":
            created_tenant = await get_tenant(session, tenant_id)
            await update_tenant(
                session,
                created_tenant,
                display_name=display_name,
                status=status,
                request_limit_per_minute=req_limit,
                high_risk_limit_per_hour=hr_limit,
            )
        await persist_governance_event(
            session,
            tenant_id=tenant_id,
            user_id=actor_for(request, "dashboard-admin"),
            event_type="tenant.created",
            decision="created",
            reason=f"Tenant {tenant_id} created from Governance Dashboard.",
            limit_value=req_limit,
            used=0,
            remaining=req_limit,
            details={"high_risk_limit_per_hour": hr_limit, "status": status},
        )
    else:
        await update_tenant(
            session,
            existing,
            display_name=display_name,
            status=status,
            request_limit_per_minute=req_limit,
            high_risk_limit_per_hour=hr_limit,
        )
        await persist_governance_event(
            session,
            tenant_id=tenant_id,
            user_id=actor_for(request, "dashboard-admin"),
            event_type="tenant.updated",
            decision="updated",
            reason=f"Tenant {tenant_id} updated from Governance Dashboard.",
            limit_value=req_limit,
            used=0,
            remaining=req_limit,
            details={"high_risk_limit_per_hour": hr_limit, "status": status},
        )
    return RedirectResponse(f"/dashboard/governance?tenant_id={tenant_id}", status_code=303)


async def _issue_tenant_api_key_and_render(
    request: Request,
    session: AsyncSession,
    *,
    tenant_id: str,
    name: str,
    role: str,
) -> HTMLResponse:
    settings = get_settings()
    existing_tenant = await get_tenant(session, tenant_id)
    if existing_tenant is None:
        await create_tenant(
            session,
            tenant_id=tenant_id,
            display_name=tenant_id,
            request_limit_per_minute=settings.tenant_request_limit_per_minute,
            high_risk_limit_per_hour=settings.tenant_high_risk_limit_per_hour,
        )
    raw_key = generate_api_key()
    key_prefix = raw_key[:8]
    key_hash = hash_api_key(raw_key)
    key_id = f"key_{uuid.uuid4().hex[:12]}"
    record = await create_tenant_api_key(
        session,
        tenant_id=tenant_id,
        key_id=key_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        role=role,
    )
    await persist_governance_event(
        session,
        tenant_id=tenant_id,
        user_id=actor_for(request, "dashboard-admin"),
        event_type="api_key.created",
        decision="created",
        reason=f"API key {name} ({role}) issued for tenant {tenant_id}.",
        details={"key_id": record.key_id, "role": role, "key_prefix": key_prefix},
    )
    runs = await list_runs(session, tenant_id=tenant_id, limit=100)
    tenants = await list_tenants(session)
    tenant_ids = [t.tenant_id for t in tenants]
    summary = await governance_summary(session, tenant_id=tenant_id)
    events = await list_governance_events(session, tenant_id=tenant_id, limit=25)
    api_keys = await list_tenant_api_keys(session, tenant_id=tenant_id)
    from app.services.review_queue import get_evaluation_policy, policy_to_dict
    from app.ui.routers.reviews import list_evaluation_policies

    response = templates.TemplateResponse(
        request,
        "governance.html",
        {
            "evaluation_policies": await list_evaluation_policies(session),
            "selected_policy": policy_to_dict(tenant_id, await get_evaluation_policy(session, tenant_id)),
            "governance_message": "",
            "summary": summary,
            "events": events,
            "runs": runs,
            "tenants": tenants,
            "api_keys": api_keys,
            "tenant_ids": tenant_ids,
            "tenant_id": tenant_id,
            "selected_tenant": tenant_id,
            "newly_created_key": raw_key,
            "newly_created_key_name": name,
            "active": "governance",
        },
        status_code=200,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/dashboard/governance/api-keys", response_class=HTMLResponse, dependencies=needs("api_keys.manage", view=PLATFORM_VIEW))
async def dashboard_governance_api_key_create_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    name = str(form.get("name", "dashboard-key")).strip() or "dashboard-key"
    role = str(form.get("role", "operator")).strip() or "operator"
    if role not in {"admin", "operator", "viewer", "finops_admin", "reviewer", "auditor"}:
        role = "operator"
    return await _issue_tenant_api_key_and_render(
        request,
        session,
        tenant_id=tenant_id,
        name=name,
        role=role,
    )


@router.post("/dashboard/governance/tenants/{tenant_id}/api-keys", response_class=HTMLResponse, dependencies=needs("api_keys.manage", view=PLATFORM_VIEW))
async def dashboard_governance_api_key_create(
    tenant_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    eff_tenant = str(form.get("tenant_id", "")).strip() or tenant_id or "default"
    name = str(form.get("name", "dashboard-key")).strip() or "dashboard-key"
    role = str(form.get("role", "operator")).strip() or "operator"
    if role not in {"admin", "operator", "viewer", "finops_admin", "reviewer", "auditor"}:
        role = "operator"
    return await _issue_tenant_api_key_and_render(
        request,
        session,
        tenant_id=eff_tenant,
        name=name,
        role=role,
    )


@router.post("/dashboard/governance/tenants/{tenant_id}/api-keys/{key_id}/revoke", response_class=HTMLResponse, dependencies=needs("api_keys.manage", view=PLATFORM_VIEW))
async def dashboard_governance_api_key_revoke(
    request: Request,
    tenant_id: str,
    key_id: str,
    session: AsyncSession = Depends(get_session),
):
    api_key = await get_tenant_api_key(session, tenant_id=tenant_id, key_id=key_id)
    if api_key is not None:
        revoked = await revoke_tenant_api_key(session, api_key)
        await persist_governance_event(
            session,
            tenant_id=tenant_id,
            user_id=actor_for(request, "dashboard-admin"),
            event_type="api_key.revoked",
            decision="revoked",
            reason=f"API key {revoked.name} (id={revoked.key_id}) revoked from Governance Dashboard.",
            details={"key_id": revoked.key_id, "key_prefix": revoked.key_prefix},
        )
    return RedirectResponse(f"/dashboard/governance?tenant_id={tenant_id}", status_code=303)


@router.get("/dashboard/costs", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="costs"))
async def dashboard_costs(
    request: Request,
    tenant_id: str | None = None,
    project_id: str | None = None,
    cost_center: str | None = None,
    resolution_status: str | None = None,
    mcp_server_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    selected_tenant = await scoped_tenant(request, session, tenant_id) or "default"
    summary = await tenant_cost_summary(
        session,
        tenant_id=selected_tenant,
        project_id=project_id or None,
        cost_center=cost_center or None,
        mcp_server_id=mcp_server_id or None,
    )
    leases = await list_tenant_token_leases(session, state_store, tenant_id=selected_tenant)
    pricing = await list_pricing_items(session)
    violations = await list_budget_violations(
        session,
        tenant_id=selected_tenant,
        resolution_status=resolution_status or None,
        limit=25,
    )
    budgets = await list_budget_policies(session, tenant_id=selected_tenant)
    return templates.TemplateResponse(
        request,
        "costs.html",
        {
            "summary": summary,
            "leases": leases,
            "pricing": pricing,
            "violations": violations,
            "budgets": budgets,
            "tenant_id": selected_tenant,
            "project_id": project_id or "",
            "cost_center": cost_center or "",
            "mcp_server_id": mcp_server_id or "",
            "resolution_status": resolution_status or "",
            "active": "costs",
        },
    )


@router.post("/dashboard/costs/leases/{run_id}/release", response_class=HTMLResponse, dependencies=needs("finops.manage"))
async def dashboard_costs_lease_release(
    run_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    form = await request.form()
    tenant_id = await scoped_tenant(request, session, str(form.get("tenant_id", "")), permission="finops.manage") or "default"
    reason = str(form.get("reason", "Manual lease release from Costs Dashboard")).strip() or "Manual lease release"
    run = await get_run(session, run_id)
    if run is not None:
        await ensure_tenant_access(request, session, run.tenant_id, permission="finops.manage")
    if run is not None and run.status == "running":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "run_currently_running",
                "message": "Cannot manually release budget lease while run is actively running.",
            },
        )
    released = await release_run_budget_lease(
        session,
        state_store,
        run_id=run_id,
        tenant_id=run.tenant_id if run is not None else tenant_id,
        reason=reason,
        actor=actor_for(request, "dashboard-finops-admin"),
        record_governance_event=True,
    )
    await session.commit()
    eff_tenant = released.tenant_id if released is not None else tenant_id
    return RedirectResponse(f"/dashboard/costs?tenant_id={eff_tenant}", status_code=303)


@router.get("/dashboard/costs/export", dependencies=needs("finops.export", view="costs"))
async def dashboard_costs_export(
    request: Request,
    tenant_id: str = "default",
    format: str = Query(default="csv", pattern="^(json|csv)$"),
    project_id: str | None = None,
    cost_center: str | None = None,
    cost_type: str | None = None,
    provider: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    settled_only: bool = False,
    mcp_server_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    selected_tenant = await scoped_tenant(request, session, tenant_id, permission="finops.export") or "default"
    export_data = await export_tenant_cost_ledger(
        session,
        tenant_id=selected_tenant,
        project_id=project_id or None,
        cost_center=cost_center or None,
        cost_type=cost_type or None,
        provider=provider or None,
        start_date=start_date or None,
        end_date=end_date or None,
        settled_only=settled_only,
        export_format=format,
        mcp_server_id=mcp_server_id or None,
    )
    if format == "csv":
        csv_text = render_cost_ledger_csv(export_data)
        safe_tenant = re.sub(r"[^A-Za-z0-9._-]", "_", selected_tenant)
        headers = {
            "Content-Disposition": f'attachment; filename="finops_ledger_{safe_tenant}.csv"',
        }
        if export_data.truncated:
            headers["X-Export-Truncated"] = "true"
        return Response(content=csv_text, media_type="text/csv; charset=utf-8", headers=headers)
    return export_data


@router.post("/dashboard/costs/adjustments", response_class=HTMLResponse, dependencies=needs("finops.manage"))
async def dashboard_costs_adjustment_create(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    tenant_id = await scoped_tenant(request, session, str(form.get("tenant_id", "")), permission="finops.manage") or "default"
    amount_str = str(form.get("amount", "0")).strip() or "0"
    currency_str = str(form.get("currency", "USD")).strip().upper() or "USD"
    tax_rate_str = str(form.get("tax_rate", "")).strip()
    fx_rate_str = str(form.get("fx_rate", "")).strip()
    payload = CostAdjustmentCreateRequest(
        tenant_id=tenant_id,
        org_id=str(form.get("org_id", "")).strip(),
        project_id=str(form.get("project_id", "")).strip(),
        cost_center=str(form.get("cost_center", "")).strip(),
        environment=str(form.get("environment", "prod")).strip() or "prod",
        run_id=str(form.get("run_id", "")).strip(),
        provider=str(form.get("provider", "internal")).strip() or "internal",
        model_name=str(form.get("model_name", "")).strip(),
        amount=amount_str,
        currency=currency_str,
        tax_rate=tax_rate_str if tax_rate_str else None,
        fx_rate=fx_rate_str if fx_rate_str else None,
        provider_invoice_id=str(form.get("provider_invoice_id", "")).strip(),
        reverses_cost_id=str(form.get("reverses_cost_id", "")).strip() or None,
        reason=str(form.get("reason", "Manual adjustment from dashboard")).strip()
        or "Manual adjustment from dashboard",
        created_by=actor_for(request, "dashboard-finops-admin"),
    )
    await create_cost_adjustment(session, payload, actor=actor_for(request, "dashboard-finops-admin"))
    return RedirectResponse(f"/dashboard/costs?tenant_id={tenant_id}", status_code=303)


@router.post("/dashboard/costs/violations/{violation_id}/acknowledge", response_class=HTMLResponse, dependencies=needs("finops.manage"))
async def dashboard_acknowledge_violation(
    request: Request,
    violation_id: str,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    violation = await get_budget_violation(session, violation_id)
    if violation is not None:
        await ensure_tenant_access(request, session, violation.tenant_id, permission="finops.manage")
    notes = str(form.get("notes", "Acknowledged from dashboard"))
    resolved_by = actor_for(request, str(form.get("resolved_by", "dashboard-operator")))
    await acknowledge_budget_violation(
        session,
        violation_id,
        resolved_by=resolved_by,
        notes=notes,
    )
    return RedirectResponse("/dashboard/costs", status_code=303)


@router.post("/dashboard/costs/violations/{violation_id}/resolve", response_class=HTMLResponse, dependencies=needs("finops.manage"))
async def dashboard_resolve_violation(
    request: Request,
    violation_id: str,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    resolution_status = str(form.get("resolution_status", "resolved"))
    if resolution_status not in {"resolved", "ignored"}:
        resolution_status = "resolved"
    violation = await get_budget_violation(session, violation_id)
    if violation is not None:
        await ensure_tenant_access(request, session, violation.tenant_id, permission="finops.manage")
    notes = str(form.get("notes", f"Marked {resolution_status} from dashboard"))
    resolved_by = actor_for(request, str(form.get("resolved_by", "dashboard-finops-admin")))
    await resolve_budget_violation(
        session,
        violation_id,
        resolution_status=resolution_status,
        resolved_by=resolved_by,
        notes=notes,
    )
    return RedirectResponse("/dashboard/costs", status_code=303)


@router.get("/dashboard/admin/finops/budgets", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="budgets"))
async def dashboard_admin_finops_budgets(
    request: Request,
    tenant_id: str | None = None,
    active_only: bool = False,
    session: AsyncSession = Depends(get_session),
):
    tenant_id = await scoped_tenant(request, session, tenant_id)
    budgets = await list_budget_policies(
        session,
        tenant_id=tenant_id or None,
        active_only=active_only,
    )
    violations = await list_budget_violations(
        session,
        tenant_id=tenant_id or None,
        limit=25,
    )
    return templates.TemplateResponse(
        request,
        "admin_finops_budgets.html",
        {
            "budgets": budgets,
            "violations": violations,
            "tenant_id": tenant_id or "",
            "active_only": active_only,
            "active": "admin-finops",
        },
    )


@router.post("/dashboard/admin/finops/budgets", response_class=HTMLResponse, dependencies=needs("finops.manage"))
async def dashboard_admin_finops_budgets_create(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    soft_actions = [
        item.strip()
        for item in str(form.get("actions_on_soft_limit", "warn,record_violation")).split(",")
        if item.strip()
    ]
    hard_actions = [
        item.strip()
        for item in str(form.get("actions_on_hard_limit", "record_violation,fail_closed")).split(",")
        if item.strip()
    ]
    soft_val = str(form.get("soft_limit_amount", "")).strip()
    hard_val = str(form.get("hard_limit_amount", "")).strip()
    fallback_tier = str(form.get("fallback_model_tier", "")).strip() or None
    budget_tenant = await scoped_tenant(request, session, str(form.get("tenant_id", "")), permission="finops.manage") or "default"
    await create_budget_policy(
        session,
        BudgetPolicyCreateRequest(
            name=str(form.get("name", "Dashboard Budget Policy")),
            tenant_id=budget_tenant,
            scope_type=str(form.get("scope_type", "tenant")) or "tenant",
            scope_id=str(form.get("scope_id", "default")) or "default",
            period=str(form.get("period", "monthly")) or "monthly",
            soft_limit_amount=soft_val if soft_val else None,
            hard_limit_amount=hard_val if hard_val else "0",
            currency=str(form.get("currency", "USD")) or "USD",
            actions_on_soft_limit=soft_actions,
            actions_on_hard_limit=hard_actions,
            fallback_model_tier=fallback_tier,
            include_dev_environment=True,
            is_active=True,
        ),
        supersede_existing=True,
    )
    return RedirectResponse("/dashboard/admin/finops/budgets", status_code=303)


@router.post("/dashboard/admin/finops/budgets/{budget_id}/disable", response_class=HTMLResponse, dependencies=needs("finops.manage"))
async def dashboard_admin_finops_budgets_disable(
    request: Request,
    budget_id: str,
    session: AsyncSession = Depends(get_session),
):
    budget = await get_budget_policy(session, budget_id)
    if budget is not None:
        await ensure_tenant_access(request, session, budget.tenant_id, permission="finops.manage")
    await disable_budget_policy(session, budget_id)
    return RedirectResponse("/dashboard/admin/finops/budgets", status_code=303)


@router.get("/dashboard/admin/finops/pricing", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="pricing"))
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


@router.get("/dashboard/admin/finops/pricing/import", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="pricing"))
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


@router.post("/dashboard/admin/finops/pricing/import", response_class=HTMLResponse, dependencies=needs("pricing.manage", view="pricing"))
async def dashboard_admin_finops_import_post(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    file_path = str(form.get("file_path", "app/seeds/model_pricing_seed.yaml"))
    mode = str(form.get("mode", "dry_run"))
    strict = str(form.get("strict", "")) == "on"
    allow_warnings = str(form.get("allow_warnings", "")) == "on" if strict else True
    if mode == "apply":
        await ensure_permission(request, session, "pricing.import_apply")
    seed = load_pricing_seed(file_path)
    if mode == "apply":
        report = await apply_pricing_seed(
            session,
            seed,
            created_by=actor_for(request, "dashboard"),
            strict=strict,
            allow_warnings=allow_warnings,
        )
    else:
        report = await dry_run_pricing_seed(
            session,
            seed,
            created_by=actor_for(request, "dashboard"),
            strict=strict,
            allow_warnings=allow_warnings,
        )
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


@router.get("/dashboard/admin/finops/pricing/sources", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="pricing"))
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


@router.get("/dashboard/admin/finops/pricing/{pricing_id}", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="pricing"))
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


@router.post("/dashboard/admin/finops/pricing/{pricing_id}/edit", response_class=HTMLResponse, dependencies=needs("pricing.manage", view="pricing"))
async def dashboard_admin_finops_pricing_edit(
    request: Request,
    pricing_id: str,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    existing = await get_pricing_item(session, pricing_id)
    merged_meta = dict((existing.metadata_payload if existing is not None else {}) or {})
    if "tax_rate" in form:
        tax_rate_val = str(form.get("tax_rate", "")).strip()
        if tax_rate_val:
            merged_meta["tax_rate"] = tax_rate_val
        else:
            merged_meta.pop("tax_rate", None)
    if "fx_rate" in form:
        fx_rate_val = str(form.get("fx_rate", "")).strip()
        if fx_rate_val:
            merged_meta["fx_rate"] = fx_rate_val
        else:
            merged_meta.pop("fx_rate", None)
    updated = await update_pricing_item(
        session,
        pricing_id,
        PricingCatalogUpdateRequest(
            unit_price=str(form.get("unit_price", "")),
            currency=str(form.get("currency", "USD")),
            tax_included=str(form.get("tax_included", "")) == "on",
            effective_from=str(form.get("effective_from", "")) or None,
            effective_to=str(form.get("effective_to", "")) or None,
            is_active=str(form.get("is_active", "")) == "on",
            change_reason=str(form.get("change_reason", "")),
            updated_by=actor_for(request, "dashboard"),
            supersede_existing=str(form.get("supersede_existing", "")) == "on",
            metadata=merged_meta,
        ),
        updated_by=actor_for(request, "dashboard"),
    )
    target_id = updated.pricing_id if updated is not None else pricing_id
    return RedirectResponse(f"/dashboard/admin/finops/pricing/{target_id}", status_code=303)


@router.post("/dashboard/admin/finops/pricing/{pricing_id}/disable", response_class=HTMLResponse, dependencies=needs("pricing.manage", view="pricing"))
async def dashboard_admin_finops_pricing_disable(
    request: Request,
    pricing_id: str,
    session: AsyncSession = Depends(get_session),
):
    await disable_pricing_item(session, pricing_id, updated_by=actor_for(request, "dashboard"))
    return RedirectResponse(f"/dashboard/admin/finops/pricing/{pricing_id}", status_code=303)


@router.post("/dashboard/runs/{run_id}/replay", response_class=HTMLResponse, dependencies=needs("runs.replay"))
async def dashboard_run_replay(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    await ensure_tenant_access(request, session, run.tenant_id, permission="runs.replay")
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Execution plan not found.")
    steps = await list_execution_plan_steps(session, run_id)
    attempt = await persist_replay_attempt(
        session,
        run=run,
        plan=plan,
        steps=steps,
        mode="dry_run",
        created_by=actor_for(request, "dashboard-operator"),
    )
    await record_replay_usage(session, run=run, plan=plan, attempt=attempt)
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0; url=/dashboard/runs/{run_id}">'
        f'<a href="/dashboard/runs/{run_id}">Back to run detail</a>'
    )


@router.post("/dashboard/runs/{run_id}/resume", response_class=HTMLResponse, dependencies=needs("runs.resume"))
async def dashboard_run_resume(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    form = await request.form()
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    await ensure_tenant_access(request, session, run.tenant_id, permission="runs.resume")
    plan = await get_execution_plan_for_run(session, run_id)
    steps = await list_execution_plan_steps(session, run_id)
    checkpoint = await resume_run_from_checkpoint(
        session,
        run=run,
        plan=plan,
        steps=steps,
        resume_reason=str(form.get("resume_reason", "Resumed from dashboard")),
        resume_cursor=str(form.get("resume_cursor", "worker_queue")),
        created_by=actor_for(request, "dashboard-operator"),
    )
    await session.refresh(run)
    await record_checkpoint_usage(session, run=run, plan=plan, checkpoint=checkpoint)
    await acquire_run_budget_lease(session, state_store, run=run, plan=plan)
    await session.commit()
    await state_store.set_run_state(run.run_id, run.status)
    event = make_event("agent.requested", run.run_id, {"request": run.request_payload})
    await event_publisher.publish(get_settings().kafka_request_topic, run.run_id, event)
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0; url=/dashboard/runs/{run_id}">'
        f'<a href="/dashboard/runs/{run_id}">Back to run detail</a>'
    )


@router.post("/dashboard/worker/dlq/{run_id}/requeue", response_class=HTMLResponse, dependencies=needs("worker.manage", view="worker"))
async def dashboard_worker_dlq_requeue(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    run = await requeue_dlq_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    await acquire_run_budget_lease(
        session,
        state_store,
        run=run,
        plan=await get_execution_plan_for_run(session, run.run_id),
    )
    await session.commit()
    await state_store.set_run_state(run.run_id, run.status)
    event = make_event("agent.requested", run.run_id, {"request": run.request_payload})
    await event_publisher.publish(get_settings().kafka_request_topic, run.run_id, event)
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/worker">'
        '<a href="/dashboard/worker">Back to worker</a>'
    )


@router.post("/dashboard/worker/dlq/{run_id}/resolve", response_class=HTMLResponse, dependencies=needs("worker.manage", view="worker"))
async def dashboard_worker_dlq_resolve(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    reason = str(form.get("reason", "Resolved from dashboard"))
    await resolve_dlq_run(session, run_id, reason=reason)
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/worker">'
        '<a href="/dashboard/worker">Back to worker</a>'
    )


@router.post("/dashboard/approvals/expire-stale", response_class=HTMLResponse, dependencies=needs("approvals.expire", view=PLATFORM_VIEW))
async def dashboard_expire_stale_approvals(
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    expired_run_ids = await expire_stale_approvals(session)
    for rid in expired_run_ids:
        exp_run = await get_run(session, rid)
        await state_store.release_token_lease(
            exp_run.tenant_id if exp_run else "default",
            rid,
            status="released",
            reason="Approval TTL expired without reviewer decision.",
        )
        await state_store.set_run_state(rid, "denied")
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/approvals">'
        '<a href="/dashboard/approvals">Back to approvals</a>'
    )


@router.post("/dashboard/approvals/{run_id}/decision", response_class=HTMLResponse, dependencies=needs("approvals.decide"))
async def dashboard_approval_decision(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    form = await request.form()
    target = await get_run(session, run_id)
    if target is not None:
        await ensure_tenant_access(request, session, target.tenant_id, permission="approvals.decide")
    decision = ApprovalDecisionRequest(
        decision=str(form.get("decision", "")),
        reviewer=actor_for(request, str(form.get("reviewer", "dashboard-operator"))),
        reason=str(form.get("reason", "Dashboard decision")),
    )
    try:
        run = await apply_approval_decision(session, run_id, decision)
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc), "status": exc.status}) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if decision.decision == "deny":
        await state_store.release_token_lease(
            run.tenant_id,
            run.run_id,
            status="released",
            reason=run.decision_reason,
        )
    elif decision.decision == "approve_once":
        await acquire_run_budget_lease(
            session,
            state_store,
            run=run,
            plan=await get_execution_plan_for_run(session, run.run_id),
        )
        await session.commit()
    await state_store.set_run_state(run.run_id, run.status)
    if run.status == "queued":
        event = make_event("agent.requested", run.run_id, {"request": run.request_payload})
        await event_publisher.publish(get_settings().kafka_request_topic, run.run_id, event)
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard/approvals">'
        '<a href="/dashboard/approvals">Back to approvals</a>'
    )


@router.get("/dashboard/playground", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="playground"))
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


@router.post("/dashboard/playground", response_class=HTMLResponse, dependencies=needs("runs.submit", view="playground"))
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
        raw_tool_args = str(form.get("tool_arguments", "") or "").strip()
        parsed_tool_args: dict = {}
        if raw_tool_args:
            try:
                parsed_tool_args = json.loads(raw_tool_args)
            except json.JSONDecodeError as exc:
                raise ValueError(f"tool_arguments must be valid JSON: {exc.msg}") from exc
            if not isinstance(parsed_tool_args, dict) or not all(isinstance(v, dict) for v in parsed_tool_args.values()):
                raise ValueError("tool_arguments must be a JSON object mapping tool_name to an argument object.")
        playground_tenant = await scoped_tenant(request, session, str(form.get("tenant_id", "")), permission="runs.submit")
        agent_request = AgentRequest(
            tool_arguments=parsed_tool_args,
            tenant_id=playground_tenant or "default",
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
                **(
                    {"provider": str(form.get("provider")).strip()}
                    if str(form.get("provider", "")).strip()
                    else {}
                ),
                **(
                    {"model_name": str(form.get("model_name")).strip()}
                    if str(form.get("model_name", "")).strip()
                    else {}
                ),
                **(
                    {"pricing_mode": str(form.get("pricing_mode")).strip()}
                    if str(form.get("pricing_mode", "")).strip()
                    else {}
                ),
                **(
                    {"project_id": str(form.get("project_id")).strip()}
                    if str(form.get("project_id", "")).strip()
                    else {}
                ),
                **(
                    {"cost_center": str(form.get("cost_center")).strip()}
                    if str(form.get("cost_center", "")).strip()
                    else {}
                ),
                **(
                    {"org_id": str(form.get("org_id")).strip()}
                    if str(form.get("org_id", "")).strip()
                    else {}
                ),
            },
        )
        settings = get_settings()
        tenant_row = await get_tenant(session, agent_request.tenant_id or "default")
        req_limit = tenant_row.request_limit_per_minute if tenant_row else None
        hr_limit = tenant_row.high_risk_limit_per_hour if tenant_row else None
        if settings.quota_enabled:
            q_res = await check_request_quota(
                state_store,
                settings=settings,
                tenant_id=agent_request.tenant_id or "default",
                limit=req_limit,
            )
            if not q_res.allowed:
                await persist_governance_event(
                    session,
                    tenant_id=agent_request.tenant_id or "default",
                    user_id=agent_request.user_id,
                    event_type="request_quota_exceeded",
                    decision="blocked",
                    reason="Tenant request quota exceeded.",
                    limit_value=q_res.limit,
                    used=q_res.used,
                    remaining=q_res.remaining,
                    details={"bucket": q_res.bucket, "source": "playground"},
                )
                return templates.TemplateResponse(
                    request,
                    "playground.html",
                    {
                        "active": "playground",
                        "submitted": False,
                        "error": "tenant_request_quota_exceeded: Tenant request quota exceeded.",
                        "result": None,
                        "form": form,
                    },
                )
            u_res = await check_user_request_quota(
                state_store,
                settings=settings,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
            )
            if not u_res.allowed:
                await persist_governance_event(
                    session,
                    tenant_id=agent_request.tenant_id or "default",
                    user_id=agent_request.user_id,
                    event_type="user_request_quota_exceeded",
                    decision="blocked",
                    reason="User request quota exceeded.",
                    limit_value=u_res.limit,
                    used=u_res.used,
                    remaining=u_res.remaining,
                    details={"bucket": u_res.bucket, "source": "playground"},
                )
                return templates.TemplateResponse(
                    request,
                    "playground.html",
                    {
                        "active": "playground",
                        "submitted": False,
                        "error": "user_request_quota_exceeded: User request quota exceeded.",
                        "result": None,
                        "form": form,
                    },
                )
            tool_limits: dict[str, int] = {}
            for item in await list_tool_definitions(session):
                raw_q = (item.get("metadata") or {}).get("quota_per_minute")
                if raw_q is not None:
                    try:
                        if int(raw_q) > 0:
                            tool_limits[str(item["tool_name"])] = int(raw_q)
                    except (TypeError, ValueError):
                        pass
            t_allowed, blocked_tool, t_res = await check_tool_quotas(
                state_store,
                settings=settings,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
                allowed_tools=agent_request.allowed_tools,
                tool_limits=tool_limits,
            )
            if not t_allowed and t_res is not None:
                await persist_governance_event(
                    session,
                    tenant_id=agent_request.tenant_id or "default",
                    user_id=agent_request.user_id,
                    event_type="tool_quota_exceeded",
                    decision="blocked",
                    reason=f"Tool call quota exceeded for {blocked_tool}.",
                    limit_value=t_res.limit,
                    used=t_res.used,
                    remaining=t_res.remaining,
                    details={"bucket": t_res.bucket, "tool_name": blocked_tool, "source": "playground"},
                )
                return templates.TemplateResponse(
                    request,
                    "playground.html",
                    {
                        "active": "playground",
                        "submitted": False,
                        "error": f"tool_quota_exceeded: Tool call quota exceeded for {blocked_tool}.",
                        "result": None,
                        "form": form,
                    },
                )
        mcp_result = await mcp_preflight(
            session,
            state_store,
            settings=settings,
            agent_request=agent_request,
        )
        if mcp_result.quota_error is not None:
            mcp_quota = mcp_result.quota_error["quota"]
            await persist_governance_event(
                session,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
                event_type="mcp_server_quota_exceeded",
                decision="blocked",
                reason=mcp_result.quota_error["message"],
                limit_value=mcp_quota.limit,
                used=mcp_quota.used,
                remaining=mcp_quota.remaining,
                details={
                    "bucket": mcp_quota.bucket,
                    "mcp_server_id": mcp_result.quota_error["mcp_server_id"],
                    "source": "playground",
                },
            )
            return templates.TemplateResponse(
                request,
                "playground.html",
                {
                    "active": "playground",
                    "submitted": False,
                    "error": f"mcp_server_quota_exceeded: {mcp_result.quota_error['message']}",
                    "result": None,
                    "form": form,
                },
            )
        agent_request = mcp_result.sanitized_request
        disabled_tools = list((await state_store.get_open_breakers(agent_request.allowed_tools)).keys())
        result = orchestrator.submit(agent_request, disabled_tools=disabled_tools, mcp_context=mcp_result.context)
        if settings.quota_enabled:
            hr_res = await check_high_risk_quota(
                state_store,
                settings=settings,
                request=agent_request,
                risk_score=result.response.risk_score,
                limit=hr_limit,
            )
            if hr_res is not None and not hr_res.allowed:
                result.response.status = "awaiting_approval"
                result.response.decision_reason = "Tenant high-risk quota exceeded."
                if "tenant_high_risk_quota_exceeded" not in result.response.policy_flags:
                    result.response.policy_flags.append("tenant_high_risk_quota_exceeded")
                result.evaluation_summary["run_state"] = {"status": "awaiting_approval"}
                result.evaluation_summary["approval"] = {
                    "status": "pending",
                    "reason": result.response.decision_reason,
                }
                await persist_governance_event(
                    session,
                    tenant_id=agent_request.tenant_id or "default",
                    user_id=agent_request.user_id,
                    run_id=result.response.run_id,
                    event_type="high_risk_quota_exceeded",
                    decision="approval_required",
                    reason=result.response.decision_reason,
                    limit_value=hr_res.limit,
                    used=hr_res.used,
                    remaining=hr_res.remaining,
                    details={"bucket": hr_res.bucket, "source": "playground"},
                )
        await _attach_execution_plan(
            session,
            planner=planner,
            agent_request=agent_request,
            result=result,
        )
        await persist_orchestration_result(session, agent_request, result)
        await persist_mcp_preflight_audit(
            session,
            run_id=result.response.run_id,
            request=agent_request,
            context=mcp_result.context,
        )
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
                created_by=actor_for(request, "dashboard"),
            )
            if run.status != "denied":
                await record_submit_usage(
                    session,
                    run=run,
                    plan=plan,
                    result=result,
                    state_store=state_store,
                )
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
