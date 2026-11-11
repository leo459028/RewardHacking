import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from prometheus_client import generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from app.api.deps import (
    get_control_plane_planner,
    get_event_publisher,
    get_orchestrator,
    get_state_store,
)
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
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyRecord,
    ApprovalDecisionRequest,
    DashboardSummaryResponse,
    DlqResolveRequest,
    ExpireStaleApprovalsResponse,
    GovernanceEventRecord,
    GovernanceSummaryResponse,
    IncidentRecord,
    RunRecord,
    TenantCreateRequest,
    TenantRecord,
    TenantUpdateRequest,
    ToolExecutionRecord,
    WorkerAttemptRecord,
)
from app.schemas.control_plane import (
    ExecutionPlanRecord,
    ExecutionPlanStepRecord,
    McpBreakerRecord,
    McpServerCreateRequest,
    McpServerRecord,
    ReplayBundleResponse,
    RunTraceResponse,
    ToolBreakerActionRequest,
    ToolArgumentValidationRequest,
    ToolArgumentValidationResponse,
    ToolBreakerRecord,
    ToolDefinitionCreateRequest,
    ToolDefinitionRecord,
    WorkflowDefinitionCreateRequest,
    WorkflowDefinitionRecord,
)
from app.schemas.checkpoint import (
    ReplayAttemptRecord,
    ReplayRunRequest,
    ResumeRunRequest,
    ResumeRunResponse,
    RunCheckpointRecord,
)
from app.services.events import EventPublisher
from app.services.auth import (
    AuthContext,
    auth_error,
    generate_api_key,
    get_auth_context,
    hash_api_key,
    require_role,
)
from app.services.audit_log import (
    ApprovalStateError,
    apply_approval_decision,
    create_tenant,
    create_tenant_api_key,
    dashboard_summary,
    expire_stale_approvals,
    get_tenant,
    get_tenant_api_key,
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
    persist_governance_event,
    persist_orchestration_result,
    requeue_dlq_run,
    resolve_dlq_run,
    revoke_tenant_api_key,
    update_tenant,
)
from app.services.mcp_gateway import (
    get_mcp_server_map,
    list_mcp_servers,
    persist_preflight_audit as persist_mcp_preflight_audit,
    preflight as mcp_preflight,
    resolve_tool_mcp_server,
    sanitize_tool_arguments,
    upsert_mcp_server,
    validate_tool_arguments,
)
from app.services.orchestrator import AgentOrchestrator
from app.services.quota import (
    check_high_risk_quota,
    check_request_quota,
    check_tool_quotas,
    check_user_request_quota,
)
from app.services.run_state import RunStateStore
from app.services.control_plane import (
    ControlPlanePlanner,
    create_tool_definition,
    create_workflow_definition,
    get_execution_plan_for_run,
    list_execution_plan_steps,
    list_tool_definitions,
    list_workflow_definitions,
    persist_execution_plan,
    step_to_dict,
    tool_to_dict,
    workflow_to_dict,
)
from app.services.checkpoint import (
    checkpoint_to_dict,
    list_run_checkpoints,
    persist_replay_attempt,
    persist_run_checkpoint,
    replay_attempt_to_dict,
    resume_run_from_checkpoint,
)
from app.services.finops import (
    acquire_run_budget_lease,
    record_checkpoint_usage,
    record_replay_usage,
    record_submit_usage,
)
from app.services.orchestrator import OrchestrationResult
from app.workers.producer import make_event
from app.api.common import _actor, _query_tenant, _trace_id, raise_if_purged, run_missing

router = APIRouter(prefix="/v1", tags=["agent"])


def _request_with_tenant(agent_request: AgentRequest, auth_context: AuthContext) -> AgentRequest:
    if auth_context.enabled:
        tenant_id = auth_context.tenant_id or "default"
        if agent_request.tenant_id and agent_request.tenant_id != tenant_id:
            raise auth_error(403, "tenant_mismatch", "Request tenant_id does not match API key tenant.")
        return agent_request.model_copy(update={"tenant_id": tenant_id})
    return agent_request.model_copy(update={"tenant_id": agent_request.tenant_id or "default"})


def _quota_response(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": {"code": code, "message": message}},
    )


async def _tenant_quota_limits(
    session: AsyncSession,
    tenant_id: str,
) -> tuple[int | None, int | None]:
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        return None, None
    return tenant.request_limit_per_minute, tenant.high_risk_limit_per_hour


async def _tool_quota_limits(session: AsyncSession) -> dict[str, int]:
    limits: dict[str, int] = {}
    for item in await list_tool_definitions(session):
        meta = dict(item.get("metadata") or {})
        raw = meta.get("quota_per_minute")
        if raw is not None:
            try:
                val = int(raw)
                if val > 0:
                    limits[str(item["tool_name"])] = val
            except (TypeError, ValueError):
                pass
    return limits


def _workflow_record(payload: dict) -> WorkflowDefinitionRecord:
    return WorkflowDefinitionRecord(**payload)


def _tool_record(payload: dict) -> ToolDefinitionRecord:
    return ToolDefinitionRecord(
        tool_name=payload["tool_name"],
        display_name=payload["display_name"],
        status=payload["status"],
        task_types=payload["task_types"],
        requires_approval=payload["requires_approval"],
        risk_level=payload["risk_level"],
        metadata=payload.get("metadata", {}),
    )


def _execution_plan_record(plan) -> ExecutionPlanRecord:
    return ExecutionPlanRecord(
        plan_id=plan.plan_id,
        run_id=plan.run_id,
        tenant_id=plan.tenant_id,
        trace_id=plan.trace_id,
        workflow_id=plan.workflow_id,
        workflow_version=plan.workflow_version,
        status=plan.status,
        model_route=plan.model_route,
        selected_tools=plan.selected_tools,
        steps=plan.steps,
        approval_gates=plan.approval_gates,
        policy_snapshot=plan.policy_snapshot,
        trace_metadata=plan.trace_metadata,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def _execution_plan_step_record(step) -> ExecutionPlanStepRecord:
    return ExecutionPlanStepRecord(**step_to_dict(step))


def _checkpoint_record(checkpoint) -> RunCheckpointRecord:
    return RunCheckpointRecord(**checkpoint_to_dict(checkpoint))


def _replay_attempt_record(attempt) -> ReplayAttemptRecord:
    return ReplayAttemptRecord(**replay_attempt_to_dict(attempt))


def _resume_cursor_for_status(status: str) -> str:
    if status == "queued":
        return "worker_queue"
    if status == "awaiting_approval":
        return "approval_queue"
    return "completed_state"


async def _attach_execution_plan(
    session: AsyncSession,
    *,
    planner: ControlPlanePlanner,
    agent_request: AgentRequest,
    result: OrchestrationResult,
) -> None:
    workflows = await list_workflow_definitions(session)
    tools = await list_tool_definitions(session)
    plan = planner.plan(
        request=agent_request,
        run_id=result.response.run_id,
        policy_snapshot=dict(result.evaluation_summary.get("policy", {})),
        trace_id=str(agent_request.metadata.get("trace_id", "")),
        workflows=workflows,
        tools=tools,
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


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(session: AsyncSession = Depends(get_session)):
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/metrics")
async def metrics(request: Request, auth_context: AuthContext = Depends(get_auth_context)):
    require_role(auth_context, "viewer", "operator", "admin")
    # v1.6.0 D8: OpenMetrics (with trace exemplars) on request; the default text format is unchanged.
    if "application/openmetrics-text" in request.headers.get("accept", ""):
        from prometheus_client import REGISTRY
        from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST, generate_latest as generate_openmetrics

        return Response(generate_openmetrics(REGISTRY), media_type=CONTENT_TYPE_LATEST)
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


@router.get("/control-plane/workflows", response_model=list[WorkflowDefinitionRecord])
async def get_control_plane_workflows(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    return [_workflow_record(item) for item in await list_workflow_definitions(session)]


@router.post("/control-plane/workflows", response_model=WorkflowDefinitionRecord)
async def post_control_plane_workflow(
    payload: WorkflowDefinitionCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    workflow = await create_workflow_definition(
        session,
        workflow_id=payload.workflow_id,
        version=payload.version,
        task_type=payload.task_type,
        display_name=payload.display_name,
        status=payload.status,
        definition=payload.definition,
    )
    return _workflow_record(workflow_to_dict(workflow))


@router.get("/control-plane/tools", response_model=list[ToolDefinitionRecord])
async def get_control_plane_tools(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    return [_tool_record(item) for item in await list_tool_definitions(session)]


@router.post("/control-plane/tools", response_model=ToolDefinitionRecord)
async def post_control_plane_tool(
    payload: ToolDefinitionCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    tool = await create_tool_definition(
        session,
        tool_name=payload.tool_name,
        display_name=payload.display_name,
        status=payload.status,
        task_types=payload.task_types,
        requires_approval=payload.requires_approval,
        risk_level=payload.risk_level,
        metadata=payload.metadata,
    )
    return _tool_record(tool_to_dict(tool))


def _mcp_server_record(server: dict, open_breakers: dict) -> McpServerRecord:
    return McpServerRecord(**server, breaker_open=server["mcp_server_id"] in open_breakers)


@router.get("/control-plane/mcp-servers", response_model=list[McpServerRecord])
async def get_control_plane_mcp_servers(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "viewer", "operator", "admin")
    open_breakers = await state_store.list_open_mcp_breakers()
    return [_mcp_server_record(item, open_breakers) for item in await list_mcp_servers(session, get_settings())]


@router.post("/control-plane/mcp-servers", response_model=McpServerRecord)
async def post_control_plane_mcp_server(
    payload: McpServerCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "admin")
    server = await upsert_mcp_server(session, payload.model_dump(), get_settings())
    await persist_governance_event(
        session,
        tenant_id=auth_context.tenant_id or "default",
        user_id=_actor(auth_context),
        event_type="mcp_server.upserted",
        decision="recorded",
        reason=f"MCP server {server['mcp_server_id']} upserted.",
        details={
            "mcp_server_id": server["mcp_server_id"],
            "transport": server["transport"],
            "trust_tier": server["trust_tier"],
            "is_external_network": server["is_external_network"],
            "source": "api",
        },
    )
    open_breakers = await state_store.list_open_mcp_breakers()
    return _mcp_server_record(server, open_breakers)


async def _set_mcp_breaker(
    *,
    mcp_server_id: str,
    open_breaker: bool,
    reason: str,
    ttl_seconds: int,
    actor: str,
    tenant_id: str,
    source: str,
    session: AsyncSession,
    state_store: RunStateStore,
) -> McpBreakerRecord:
    server_map = await get_mcp_server_map(session, get_settings())
    if mcp_server_id not in server_map:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    if open_breaker:
        await state_store.open_mcp_breaker(mcp_server_id, reason=reason, ttl_seconds=ttl_seconds)
    else:
        await state_store.close_mcp_breaker(mcp_server_id)
    await persist_governance_event(
        session,
        tenant_id=tenant_id,
        user_id=actor,
        event_type="mcp_breaker.opened" if open_breaker else "mcp_breaker.closed",
        decision="tripped" if open_breaker else "reset",
        reason=reason,
        details={"mcp_server_id": mcp_server_id, "ttl_seconds": ttl_seconds, "source": source},
    )
    info = (await state_store.get_open_mcp_breakers([mcp_server_id])).get(mcp_server_id, {})
    return McpBreakerRecord(
        mcp_server_id=mcp_server_id,
        status="open" if open_breaker else "closed",
        is_open=open_breaker,
        reason=reason,
        opened_at=str(info.get("opened_at", "")),
        ttl_seconds=ttl_seconds,
    )


@router.post("/control-plane/mcp-servers/{mcp_server_id}/breaker/open", response_model=McpBreakerRecord)
async def post_open_mcp_breaker(
    mcp_server_id: str,
    payload: ToolBreakerActionRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "admin")
    settings = get_settings()
    return await _set_mcp_breaker(
        mcp_server_id=mcp_server_id,
        open_breaker=True,
        reason=payload.reason if payload is not None else "Manual MCP circuit breaker trip by admin",
        ttl_seconds=payload.ttl_seconds if payload is not None else settings.tool_breaker_ttl_seconds,
        actor=_actor(auth_context),
        tenant_id=auth_context.tenant_id or "default",
        source="api",
        session=session,
        state_store=state_store,
    )


@router.post("/control-plane/mcp-servers/{mcp_server_id}/breaker/close", response_model=McpBreakerRecord)
async def post_close_mcp_breaker(
    mcp_server_id: str,
    payload: ToolBreakerActionRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "admin")
    return await _set_mcp_breaker(
        mcp_server_id=mcp_server_id,
        open_breaker=False,
        reason=payload.reason if payload is not None else "Manual MCP circuit breaker reset by admin",
        ttl_seconds=0,
        actor=_actor(auth_context),
        tenant_id=auth_context.tenant_id or "default",
        source="api",
        session=session,
        state_store=state_store,
    )


@router.post("/control-plane/tools/{tool_name}/validate-arguments", response_model=ToolArgumentValidationResponse)
async def post_validate_tool_arguments(
    tool_name: str,
    payload: ToolArgumentValidationRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    settings = get_settings()
    tool = next((item for item in await list_tool_definitions(session) if item["tool_name"] == tool_name), None)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found.")
    result = validate_tool_arguments(
        tool_name,
        payload.arguments,
        tool,
        max_depth=settings.mcp_tool_arguments_max_depth,
        max_bytes=settings.mcp_tool_arguments_max_bytes,
    )
    sanitized, redacted = sanitize_tool_arguments(
        payload.arguments,
        sensitive_fields=list((tool.get("metadata") or {}).get("sensitive_fields") or []),
        allow_pii=payload.allow_pii,
        max_depth=settings.mcp_tool_arguments_max_depth,
        tool_name=tool_name,
    )
    return ToolArgumentValidationResponse(
        tool_name=tool_name,
        mcp_server_id=resolve_tool_mcp_server(tool),
        valid=result.valid,
        errors=result.errors,
        injection_flags=result.injection_flags,
        sanitized_arguments=sanitized,
        redacted_fields=redacted,
    )


@router.get("/control-plane/breakers", response_model=list[ToolBreakerRecord])
async def get_control_plane_breakers(
    auth_context: AuthContext = Depends(get_auth_context),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "viewer", "operator", "admin")
    open_map = await state_store.list_open_breakers()
    return [
        ToolBreakerRecord(
            tool_name=name,
            status=str(info.get("status", "open")),
            is_open=str(info.get("status", "open")) == "open",
            reason=str(info.get("reason", "")),
            opened_at=str(info.get("opened_at", "")),
            ttl_seconds=int(info.get("ttl_seconds", 1800)),
            failure_count=int(info.get("failure_count", 0)),
        )
        for name, info in sorted(open_map.items())
    ]


@router.post("/control-plane/tools/{tool_name}/breaker/open", response_model=ToolBreakerRecord)
async def post_open_tool_breaker(
    tool_name: str,
    payload: ToolBreakerActionRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "admin")
    settings = get_settings()
    reason = payload.reason if payload is not None else "Manual circuit breaker trip by admin"
    ttl_seconds = payload.ttl_seconds if payload is not None else settings.tool_breaker_ttl_seconds
    await state_store.open_breaker(tool_name, reason=reason, ttl_seconds=ttl_seconds)
    await persist_governance_event(
        session,
        tenant_id=auth_context.tenant_id or "default",
        user_id=_actor(auth_context),
        event_type="tool_breaker.opened",
        decision="tripped",
        reason=reason,
        details={"tool_name": tool_name, "ttl_seconds": ttl_seconds, "source": "api"},
    )
    open_map = await state_store.get_open_breakers([tool_name])
    info = open_map.get(tool_name, {})
    return ToolBreakerRecord(
        tool_name=tool_name,
        status="open",
        is_open=True,
        reason=reason,
        opened_at=str(info.get("opened_at", "")),
        ttl_seconds=ttl_seconds,
        failure_count=int(info.get("failure_count", 0)),
    )


@router.post("/control-plane/tools/{tool_name}/breaker/close", response_model=ToolBreakerRecord)
async def post_close_tool_breaker(
    tool_name: str,
    payload: ToolBreakerActionRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "admin")
    reason = payload.reason if payload is not None else "Manual circuit breaker reset by admin"
    await state_store.close_breaker(tool_name)
    await persist_governance_event(
        session,
        tenant_id=auth_context.tenant_id or "default",
        user_id=_actor(auth_context),
        event_type="tool_breaker.closed",
        decision="reset",
        reason=reason,
        details={"tool_name": tool_name, "source": "api"},
    )
    return ToolBreakerRecord(
        tool_name=tool_name,
        status="closed",
        is_open=False,
        reason=reason,
        opened_at="",
        ttl_seconds=0,
        failure_count=0,
    )


@router.get("/tenants", response_model=list[TenantRecord])
async def get_tenants(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    if auth_context.enabled and auth_context.role != "admin":
        tenant = await get_tenant(session, auth_context.tenant_id or "default")
        return [tenant] if tenant is not None else []
    return await list_tenants(session)


@router.post("/tenants", response_model=TenantRecord)
async def post_tenant(
    payload: TenantCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    settings = get_settings()
    tenant = await create_tenant(
        session,
        tenant_id=payload.tenant_id,
        display_name=payload.display_name,
        request_limit_per_minute=(
            payload.request_limit_per_minute
            if payload.request_limit_per_minute is not None
            else settings.tenant_request_limit_per_minute
        ),
        high_risk_limit_per_hour=(
            payload.high_risk_limit_per_hour
            if payload.high_risk_limit_per_hour is not None
            else settings.tenant_high_risk_limit_per_hour
        ),
    )
    await persist_governance_event(
        session,
        tenant_id=tenant.tenant_id,
        event_type="tenant.created",
        decision="created",
        reason=f"Tenant {tenant.tenant_id} created.",
        limit_value=tenant.request_limit_per_minute,
        details={"display_name": tenant.display_name, "actor_key_id": auth_context.key_id},
    )
    return tenant


@router.patch("/tenants/{tenant_id}", response_model=TenantRecord)
async def patch_tenant(
    tenant_id: str,
    payload: TenantUpdateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    tenant = await update_tenant(
        session,
        tenant,
        display_name=payload.display_name,
        status=payload.status,
        request_limit_per_minute=payload.request_limit_per_minute,
        high_risk_limit_per_hour=payload.high_risk_limit_per_hour,
    )
    await persist_governance_event(
        session,
        tenant_id=tenant.tenant_id,
        event_type="tenant.updated",
        decision="updated",
        reason=f"Tenant {tenant.tenant_id} updated.",
        limit_value=tenant.request_limit_per_minute,
        details={"status": tenant.status, "actor_key_id": auth_context.key_id},
    )
    return tenant


@router.get("/tenants/{tenant_id}/api-keys", response_model=list[ApiKeyRecord])
async def get_tenant_api_keys(
    tenant_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    return await list_tenant_api_keys(session, tenant_id=tenant_id)


@router.post("/tenants/{tenant_id}/api-keys", response_model=ApiKeyCreateResponse)
async def post_tenant_api_key(
    tenant_id: str,
    payload: ApiKeyCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    allowed_roles = {
        "viewer",
        "operator",
        "admin",
        "super_admin",
        "finops_admin",
        "tenant_admin",
        "reviewer",
        "end_user",
    }
    if payload.role not in allowed_roles:
        raise auth_error(422, "invalid_role", "Role must be a supported RBAC role.")
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    plaintext_key = generate_api_key()
    api_key = await create_tenant_api_key(
        session,
        tenant_id=tenant.tenant_id,
        key_id=f"key_{uuid.uuid4().hex[:12]}",
        name=payload.name,
        key_prefix=plaintext_key[:8],
        key_hash=hash_api_key(plaintext_key),
        role=payload.role,
    )
    await persist_governance_event(
        session,
        tenant_id=tenant.tenant_id,
        event_type="api_key.created",
        decision="created",
        reason=f"API key {api_key.key_id} created.",
        details={"role": api_key.role, "actor_key_id": auth_context.key_id},
    )
    key_payload = ApiKeyRecord.model_validate(api_key).model_dump()
    return ApiKeyCreateResponse(**key_payload, plaintext_key=plaintext_key)


@router.post("/tenants/{tenant_id}/api-keys/{key_id}/revoke", response_model=ApiKeyRecord)
async def post_revoke_tenant_api_key(
    tenant_id: str,
    key_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    api_key = await get_tenant_api_key(session, tenant_id=tenant_id, key_id=key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    api_key = await revoke_tenant_api_key(session, api_key)
    await persist_governance_event(
        session,
        tenant_id=tenant_id,
        event_type="api_key.revoked",
        decision="revoked",
        reason=f"API key {key_id} revoked.",
        details={"actor_key_id": auth_context.key_id},
    )
    return api_key


@router.post("/agent/submit", response_model=AgentResponse)
async def submit_agent_request(
    request: Request,
    agent_request: AgentRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    planner: ControlPlanePlanner = Depends(get_control_plane_planner),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    require_role(auth_context, "operator", "admin")
    agent_request = _request_with_tenant(agent_request, auth_context)
    agent_request = agent_request.model_copy(
        update={"metadata": {**agent_request.metadata, "trace_id": _trace_id(request)}}
    )
    settings = get_settings()
    request_limit, high_risk_limit = await _tenant_quota_limits(
        session,
        agent_request.tenant_id or "default",
    )
    if settings.quota_enabled:
        quota = await check_request_quota(
            state_store,
            settings=settings,
            tenant_id=agent_request.tenant_id or "default",
            limit=request_limit,
        )
        if not quota.allowed:
            await persist_governance_event(
                session,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
                event_type="request_quota_exceeded",
                decision="blocked",
                reason="Tenant request quota exceeded.",
                limit_value=quota.limit,
                used=quota.used,
                remaining=quota.remaining,
                details={"bucket": quota.bucket},
            )
            return _quota_response(
                "tenant_request_quota_exceeded",
                "Tenant request quota exceeded.",
            )
        user_quota = await check_user_request_quota(
            state_store,
            settings=settings,
            tenant_id=agent_request.tenant_id or "default",
            user_id=agent_request.user_id,
        )
        if not user_quota.allowed:
            await persist_governance_event(
                session,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
                event_type="user_request_quota_exceeded",
                decision="blocked",
                reason="User request quota exceeded.",
                limit_value=user_quota.limit,
                used=user_quota.used,
                remaining=user_quota.remaining,
                details={"bucket": user_quota.bucket},
            )
            return _quota_response(
                "user_request_quota_exceeded",
                "User request quota exceeded.",
            )
        tool_allowed, blocked_tool, tool_quota = await check_tool_quotas(
            state_store,
            settings=settings,
            tenant_id=agent_request.tenant_id or "default",
            user_id=agent_request.user_id,
            allowed_tools=agent_request.allowed_tools,
            tool_limits=await _tool_quota_limits(session),
        )
        if not tool_allowed and tool_quota is not None:
            await persist_governance_event(
                session,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
                event_type="tool_quota_exceeded",
                decision="blocked",
                reason=f"Tool call quota exceeded for {blocked_tool}.",
                limit_value=tool_quota.limit,
                used=tool_quota.used,
                remaining=tool_quota.remaining,
                details={"bucket": tool_quota.bucket, "tool_name": blocked_tool},
            )
            return _quota_response(
                "tool_quota_exceeded",
                f"Tool call quota exceeded for {blocked_tool}.",
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
            details={"bucket": mcp_quota.bucket, "mcp_server_id": mcp_result.quota_error["mcp_server_id"]},
        )
        return _quota_response("mcp_server_quota_exceeded", mcp_result.quota_error["message"])
    agent_request = mcp_result.sanitized_request
    disabled_tools = list((await state_store.get_open_breakers(agent_request.allowed_tools)).keys())
    result = orchestrator.submit(agent_request, disabled_tools=disabled_tools, mcp_context=mcp_result.context)
    if settings.quota_enabled:
        high_risk_quota = await check_high_risk_quota(
            state_store,
            settings=settings,
            request=agent_request,
            risk_score=result.response.risk_score,
            limit=high_risk_limit,
        )
        if high_risk_quota is not None and not high_risk_quota.allowed:
            result.response.status = "awaiting_approval"
            result.response.decision_reason = "Tenant high-risk quota exceeded."
            if "tenant_high_risk_quota_exceeded" not in result.response.policy_flags:
                result.response.policy_flags.append("tenant_high_risk_quota_exceeded")
            result.evaluation_summary["run_state"] = {
                "status": "awaiting_approval",
            }
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
                limit_value=high_risk_quota.limit,
                used=high_risk_quota.used,
                remaining=high_risk_quota.remaining,
                details={"bucket": high_risk_quota.bucket},
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
            resume_cursor=_resume_cursor_for_status(run.status),
            created_by=_actor(auth_context),
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

    REQUESTS_TOTAL.labels(status=result.response.status).inc()
    POLICY_DECISIONS_TOTAL.labels(status=result.response.status).inc()
    POLICY_RISK.observe(result.response.risk_score)

    return result.response


@router.post("/agent/submit-with-details")
async def submit_agent_request_with_details(
    request: Request,
    agent_request: AgentRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    planner: ControlPlanePlanner = Depends(get_control_plane_planner),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    require_role(auth_context, "operator", "admin")
    agent_request = _request_with_tenant(agent_request, auth_context)
    agent_request = agent_request.model_copy(
        update={"metadata": {**agent_request.metadata, "trace_id": _trace_id(request)}}
    )
    settings = get_settings()
    request_limit, high_risk_limit = await _tenant_quota_limits(
        session,
        agent_request.tenant_id or "default",
    )
    if settings.quota_enabled:
        quota = await check_request_quota(
            state_store,
            settings=settings,
            tenant_id=agent_request.tenant_id or "default",
            limit=request_limit,
        )
        if not quota.allowed:
            await persist_governance_event(
                session,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
                event_type="request_quota_exceeded",
                decision="blocked",
                reason="Tenant request quota exceeded.",
                limit_value=quota.limit,
                used=quota.used,
                remaining=quota.remaining,
                details={"bucket": quota.bucket},
            )
            return _quota_response(
                "tenant_request_quota_exceeded",
                "Tenant request quota exceeded.",
            )
        user_quota = await check_user_request_quota(
            state_store,
            settings=settings,
            tenant_id=agent_request.tenant_id or "default",
            user_id=agent_request.user_id,
        )
        if not user_quota.allowed:
            await persist_governance_event(
                session,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
                event_type="user_request_quota_exceeded",
                decision="blocked",
                reason="User request quota exceeded.",
                limit_value=user_quota.limit,
                used=user_quota.used,
                remaining=user_quota.remaining,
                details={"bucket": user_quota.bucket},
            )
            return _quota_response(
                "user_request_quota_exceeded",
                "User request quota exceeded.",
            )
        tool_allowed, blocked_tool, tool_quota = await check_tool_quotas(
            state_store,
            settings=settings,
            tenant_id=agent_request.tenant_id or "default",
            user_id=agent_request.user_id,
            allowed_tools=agent_request.allowed_tools,
            tool_limits=await _tool_quota_limits(session),
        )
        if not tool_allowed and tool_quota is not None:
            await persist_governance_event(
                session,
                tenant_id=agent_request.tenant_id or "default",
                user_id=agent_request.user_id,
                event_type="tool_quota_exceeded",
                decision="blocked",
                reason=f"Tool call quota exceeded for {blocked_tool}.",
                limit_value=tool_quota.limit,
                used=tool_quota.used,
                remaining=tool_quota.remaining,
                details={"bucket": tool_quota.bucket, "tool_name": blocked_tool},
            )
            return _quota_response(
                "tool_quota_exceeded",
                f"Tool call quota exceeded for {blocked_tool}.",
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
            details={"bucket": mcp_quota.bucket, "mcp_server_id": mcp_result.quota_error["mcp_server_id"]},
        )
        return _quota_response("mcp_server_quota_exceeded", mcp_result.quota_error["message"])
    agent_request = mcp_result.sanitized_request
    disabled_tools = list((await state_store.get_open_breakers(agent_request.allowed_tools)).keys())
    result = orchestrator.submit(agent_request, disabled_tools=disabled_tools, mcp_context=mcp_result.context)
    if settings.quota_enabled:
        high_risk_quota = await check_high_risk_quota(
            state_store,
            settings=settings,
            request=agent_request,
            risk_score=result.response.risk_score,
            limit=high_risk_limit,
        )
        if high_risk_quota is not None and not high_risk_quota.allowed:
            result.response.status = "awaiting_approval"
            result.response.decision_reason = "Tenant high-risk quota exceeded."
            if "tenant_high_risk_quota_exceeded" not in result.response.policy_flags:
                result.response.policy_flags.append("tenant_high_risk_quota_exceeded")
            result.evaluation_summary["run_state"] = {
                "status": "awaiting_approval",
            }
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
                limit_value=high_risk_quota.limit,
                used=high_risk_quota.used,
                remaining=high_risk_quota.remaining,
                details={"bucket": high_risk_quota.bucket},
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
            resume_cursor=_resume_cursor_for_status(run.status),
            created_by=_actor(auth_context),
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
    tenant_id: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context)
    return await list_approvals(
        session,
        status=status,
        reviewer=reviewer,
        expired=expired,
        tenant_id=tenant_id,
    )


@router.post("/approvals/{run_id}/decision", response_model=RunRecord)
async def decide_approval(
    run_id: str,
    decision: ApprovalDecisionRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    require_role(auth_context, "operator", "admin")
    try:
        run = await apply_approval_decision(session, run_id, decision)
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc), "status": exc.status}) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    _query_tenant(run.tenant_id, auth_context)
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
        await session.refresh(run)
    await state_store.set_run_state(run.run_id, run.status)
    if run.status == "queued":
        event = make_event("agent.requested", run.run_id, {"request": run.request_payload})
        await event_publisher.publish(get_settings().kafka_request_topic, run.run_id, event)
    return run


@router.get("/runs", response_model=list[RunRecord])
async def get_runs(
    status: str | None = None,
    task_type: str | None = None,
    tenant_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    tenant_id = _query_tenant(tenant_id, auth_context)
    return await list_runs(
        session,
        status=status,
        task_type=task_type,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
    )


@router.get("/runs/{run_id}/execution-plan", response_model=ExecutionPlanRecord)
async def get_run_execution_plan(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        await raise_if_purged(session, run_id, auth_context)
        raise HTTPException(status_code=404, detail="Execution plan not found.")
    return _execution_plan_record(plan)


@router.get("/runs/{run_id}/trace", response_model=RunTraceResponse)
async def get_run_trace(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Execution plan not found.")
    steps = await list_execution_plan_steps(session, run_id)
    return RunTraceResponse(
        trace_id=plan.trace_id or run.request_payload.get("metadata", {}).get("trace_id", ""),
        trace_metadata=plan.trace_metadata,
        run={
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,
            "status": run.status,
            "task_type": run.task_type,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        },
        execution_plan=_execution_plan_record(plan).model_dump(),
        steps=[_execution_plan_step_record(step) for step in steps],
    )


@router.get("/runs/{run_id}/replay-bundle", response_model=ReplayBundleResponse)
async def get_run_replay_bundle(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Execution plan not found.")
    steps = await list_execution_plan_steps(session, run_id)
    trace_id = plan.trace_id or run.request_payload.get("metadata", {}).get("trace_id", "")
    return ReplayBundleResponse(
        trace_context={
            "trace_id": trace_id,
            **plan.trace_metadata,
        },
        run={
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,
            "user_id": run.user_id,
            "task_type": run.task_type,
            "status": run.status,
            "request_payload": run.request_payload,
            "execution_output": run.execution_output,
            "evaluation_summary": run.evaluation_summary,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        },
        policy_decision=dict(run.evaluation_summary.get("policy", plan.policy_snapshot)),
        execution_plan=_execution_plan_record(plan).model_dump(),
        step_audit=[_execution_plan_step_record(step) for step in steps],
        tool_references=plan.selected_tools,
        limitations={
            "automatic_replay": False,
            "external_export": False,
            "note": "V1.4 keeps the auditable replay bundle and adds dry-run replay attempts without re-executing real tools.",
        },
    )


@router.get("/runs/{run_id}/checkpoints", response_model=list[RunCheckpointRecord])
async def get_run_checkpoints(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    return [_checkpoint_record(item) for item in await list_run_checkpoints(session, run_id=run_id)]


@router.post("/runs/{run_id}/resume", response_model=ResumeRunResponse)
async def post_run_resume(
    run_id: str,
    payload: ResumeRunRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    require_role(auth_context, "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    plan = await get_execution_plan_for_run(session, run_id)
    steps = await list_execution_plan_steps(session, run_id)
    checkpoint = await resume_run_from_checkpoint(
        session,
        run=run,
        plan=plan,
        steps=steps,
        resume_reason=payload.resume_reason,
        resume_cursor=payload.resume_cursor,
        created_by=payload.created_by,
    )
    await session.refresh(run)
    await record_checkpoint_usage(session, run=run, plan=plan, checkpoint=checkpoint)
    await acquire_run_budget_lease(session, state_store, run=run, plan=plan)
    await session.commit()
    await session.refresh(run)
    await session.refresh(checkpoint)
    await state_store.set_run_state(run.run_id, run.status)
    event = make_event("agent.requested", run.run_id, {"request": run.request_payload})
    await event_publisher.publish(get_settings().kafka_request_topic, run.run_id, event)
    return ResumeRunResponse(run=RunRecord.model_validate(run), checkpoint=_checkpoint_record(checkpoint))


@router.post("/runs/{run_id}/replay", response_model=ReplayAttemptRecord)
async def post_run_replay(
    run_id: str,
    payload: ReplayRunRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Execution plan not found.")
    steps = await list_execution_plan_steps(session, run_id)
    attempt = await persist_replay_attempt(
        session,
        run=run,
        plan=plan,
        steps=steps,
        mode=payload.mode,
        created_by=payload.created_by,
    )
    await record_replay_usage(session, run=run, plan=plan, attempt=attempt)
    return _replay_attempt_record(attempt)


@router.get("/runs/{run_id}", response_model=RunRecord)
async def get_run_by_id(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    return run


@router.get("/runs/{run_id}/worker-attempts", response_model=list[WorkerAttemptRecord])
async def get_run_worker_attempts(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is not None:
        _query_tenant(run.tenant_id, auth_context)
    else:
        await raise_if_purged(session, run_id, auth_context)
    return await list_worker_attempts(session, run_id=run_id)


@router.get("/runs/{run_id}/tool-executions", response_model=list[ToolExecutionRecord])
async def get_run_tool_executions(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is not None:
        _query_tenant(run.tenant_id, auth_context)
    else:
        await raise_if_purged(session, run_id, auth_context)
    return await list_tool_executions(session, run_id=run_id)


@router.get("/worker/dlq", response_model=list[WorkerAttemptRecord])
async def get_worker_dlq(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    return await list_worker_attempts(session, status="dlq")


@router.post("/worker/dlq/{run_id}/requeue", response_model=RunRecord)
async def post_worker_dlq_requeue(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    require_role(auth_context, "operator", "admin")
    run = await requeue_dlq_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    _query_tenant(run.tenant_id, auth_context)
    await acquire_run_budget_lease(
        session,
        state_store,
        run=run,
        plan=await get_execution_plan_for_run(session, run.run_id),
    )
    await session.commit()
    await session.refresh(run)
    await state_store.set_run_state(run.run_id, run.status)
    event = make_event("agent.requested", run.run_id, {"request": run.request_payload})
    await event_publisher.publish(get_settings().kafka_request_topic, run.run_id, event)
    return run


@router.post("/worker/dlq/{run_id}/resolve", response_model=list[WorkerAttemptRecord])
async def post_worker_dlq_resolve(
    run_id: str,
    payload: DlqResolveRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    reason = payload.reason if payload is not None else "Resolved by operator"
    attempts = await resolve_dlq_run(session, run_id, reason=reason)
    await state_store.release_token_lease(
        run.tenant_id,
        run.run_id,
        status="released",
        reason=f"DLQ resolved: {reason}",
    )
    return attempts


@router.post("/approvals/expire-stale", response_model=ExpireStaleApprovalsResponse)
async def post_expire_stale_approvals(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "operator", "admin")
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
    return ExpireStaleApprovalsResponse(
        expired_count=len(expired_run_ids),
        run_ids=expired_run_ids,
    )


# v1.5.8: GET /v1/evaluations moved to app/api/routers/evaluations.py (router split rule).


@router.get("/incidents", response_model=list[IncidentRecord])
async def get_incidents(
    severity: str | None = None,
    tenant_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    tenant_id = _query_tenant(tenant_id, auth_context)
    return await list_incidents(
        session,
        severity=severity,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
    )


@router.get("/runs/{run_id}/incidents", response_model=list[IncidentRecord])
async def get_run_incidents(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is not None:
        _query_tenant(run.tenant_id, auth_context)
    else:
        await raise_if_purged(session, run_id, auth_context)
    return await incidents_for_run(session, run_id)


@router.get("/dashboard/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
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


@router.get("/governance/events", response_model=list[GovernanceEventRecord])
async def get_governance_events(
    tenant_id: str | None = None,
    event_type: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    tenant_id = _query_tenant(tenant_id, auth_context)
    return await list_governance_events(
        session,
        tenant_id=tenant_id,
        event_type=event_type,
    )


@router.get("/governance/summary", response_model=GovernanceSummaryResponse)
async def get_governance_summary(
    tenant_id: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    tenant_id = _query_tenant(tenant_id, auth_context) or "default"
    summary = await governance_summary(session, tenant_id=tenant_id)
    return GovernanceSummaryResponse(
        tenant_id=summary.tenant_id,
        total_runs=summary.total_runs,
        awaiting_approval=summary.awaiting_approval,
        denied=summary.denied,
        quota_limited=summary.quota_limited,
        average_risk_score=summary.average_risk_score,
        recent_events=summary.recent_events,
    )
