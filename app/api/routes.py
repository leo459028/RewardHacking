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
    EvaluationRecord,
    GovernanceEventRecord,
    GovernanceSummaryResponse,
    IncidentRecord,
    OperationsCheckRecord,
    OperationsEvidenceCreateRequest,
    OperationsEvidenceRecord,
    OperationsReadinessResponse,
    ReleaseGateCreateRequest,
    ReleaseGateItemRecord,
    ReleaseGateRecord,
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
    ReplayBundleResponse,
    RunTraceResponse,
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
from app.schemas.finops import (
    BudgetPolicyCreateRequest,
    BudgetPolicyRecord,
    BudgetPolicyUpdateRequest,
    BudgetViolationRecord,
    CostEventRecord,
    PricingCatalogCreateRequest,
    PricingCatalogRecord,
    PricingCatalogUpdateRequest,
    RunCostSummaryResponse,
    TenantCostSummaryResponse,
    UsageEventCreateRequest,
    UsageEventRecord,
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
    apply_approval_decision,
    create_tenant,
    create_tenant_api_key,
    dashboard_summary,
    get_tenant,
    get_tenant_api_key,
    get_run,
    governance_summary,
    incidents_for_run,
    list_approvals,
    list_evaluation_results,
    list_governance_events,
    list_incidents,
    list_runs,
    list_tenant_api_keys,
    list_tenants,
    list_tool_executions,
    list_worker_attempts,
    observability_summary,
    persist_governance_event,
    persist_orchestration_result,
    revoke_tenant_api_key,
    update_tenant,
)
from app.services.orchestrator import AgentOrchestrator
from app.services.quota import check_high_risk_quota, check_request_quota
from app.services.run_state import RunStateStore
from app.services.operations import (
    compute_operations_readiness,
    get_release_gate,
    list_operations_evidence,
    list_release_gate_items,
    list_release_gates,
    persist_operations_evidence,
    persist_release_gate,
)
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
    budget_to_dict,
    cost_to_dict,
    create_budget_policy,
    create_pricing_item,
    disable_pricing_item,
    list_budget_policies,
    list_budget_violations,
    list_pricing_items,
    list_usage_events,
    model_cost_ranking,
    pricing_to_dict,
    record_checkpoint_usage,
    record_replay_usage,
    record_submit_usage,
    record_usage_event,
    run_cost_summary,
    tenant_cost_summary,
    update_budget_policy,
    update_pricing_item,
    usage_to_dict,
    violation_to_dict,
    workflow_cost_summary,
)
from app.services.orchestrator import OrchestrationResult
from app.workers.producer import make_event

router = APIRouter(prefix="/v1", tags=["agent"])


def _request_with_tenant(agent_request: AgentRequest, auth_context: AuthContext) -> AgentRequest:
    if auth_context.enabled:
        tenant_id = auth_context.tenant_id or "default"
        if agent_request.tenant_id and agent_request.tenant_id != tenant_id:
            raise auth_error(403, "tenant_mismatch", "Request tenant_id does not match API key tenant.")
        return agent_request.model_copy(update={"tenant_id": tenant_id})
    return agent_request.model_copy(update={"tenant_id": agent_request.tenant_id or "default"})


def _query_tenant(tenant_id: str | None, auth_context: AuthContext) -> str | None:
    if auth_context.enabled:
        auth_tenant = auth_context.tenant_id or "default"
        if tenant_id and tenant_id != auth_tenant:
            raise auth_error(403, "tenant_mismatch", "Query tenant_id does not match API key tenant.")
        return auth_tenant
    return tenant_id


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


def _actor(auth_context: AuthContext) -> str:
    if auth_context.key_id:
        return f"key:{auth_context.key_id}"
    if auth_context.enabled:
        return f"tenant:{auth_context.tenant_id or 'default'}"
    return "dashboard"


def _trace_id(request: Request) -> str:
    return str(getattr(request.state, "trace_id", ""))


def _evidence_record(evidence) -> OperationsEvidenceRecord:
    return OperationsEvidenceRecord(
        evidence_type=evidence.evidence_type,
        status=evidence.status,
        summary=evidence.summary,
        reference_url=evidence.reference_url,
        metadata=evidence.metadata_payload,
        created_by=evidence.created_by,
        created_at=evidence.created_at,
    )


def _check_record(check) -> OperationsCheckRecord:
    return OperationsCheckRecord(
        check_key=check.check_key,
        status=check.status,
        title=check.title,
        detail=check.detail,
        metadata=check.metadata,
    )


def _gate_item_record(item) -> ReleaseGateItemRecord:
    return ReleaseGateItemRecord(
        check_key=item.check_key,
        status=item.status,
        title=item.title,
        detail=item.detail,
        metadata=item.metadata_payload,
    )


async def _gate_record(session: AsyncSession, gate) -> ReleaseGateRecord:
    items = await list_release_gate_items(session, gate.gate_id)
    return ReleaseGateRecord(
        gate_id=gate.gate_id,
        release_version=gate.release_version,
        status=gate.status,
        summary=gate.summary,
        created_by=gate.created_by,
        trace_id=gate.trace_id,
        created_at=gate.created_at,
        items=[_gate_item_record(item) for item in items],
    )


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


def _pricing_record(item) -> PricingCatalogRecord:
    return PricingCatalogRecord(**pricing_to_dict(item))


def _usage_record(item) -> UsageEventRecord:
    return UsageEventRecord(**usage_to_dict(item))


def _cost_record(item) -> CostEventRecord:
    return CostEventRecord(**cost_to_dict(item))


def _budget_record(item) -> BudgetPolicyRecord:
    return BudgetPolicyRecord(**budget_to_dict(item))


def _violation_record(item) -> BudgetViolationRecord:
    return BudgetViolationRecord(**violation_to_dict(item))


def _run_cost_response(summary) -> RunCostSummaryResponse:
    return RunCostSummaryResponse(
        run_id=summary.run_id,
        tenant_id=summary.tenant_id,
        estimated_cost=summary.estimated_cost,
        actual_cost=summary.actual_cost,
        cost_delta=summary.cost_delta,
        currency=summary.currency,
        budget_status=summary.budget_status,
        breakdown=summary.breakdown,
        usage_events=[_usage_record(item) for item in summary.usage_events],
        cost_events=[_cost_record(item) for item in summary.cost_events],
    )


def _tenant_cost_response(summary) -> TenantCostSummaryResponse:
    return TenantCostSummaryResponse(
        tenant_id=summary.tenant_id,
        today_cost=summary.today_cost,
        month_to_date_cost=summary.month_to_date_cost,
        budget_used_percent=summary.budget_used_percent,
        violation_count=summary.violation_count,
        top_runs=summary.top_runs,
        top_models=summary.top_models,
        top_workflows=summary.top_workflows,
    )


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
async def metrics(auth_context: AuthContext = Depends(get_auth_context)):
    require_role(auth_context, "viewer", "operator", "admin")
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


@router.get("/operations/readiness", response_model=OperationsReadinessResponse)
async def get_operations_readiness(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    readiness = await compute_operations_readiness(session, settings=get_settings())
    return OperationsReadinessResponse(
        status=readiness.status,
        summary=readiness.summary,
        checks=[_check_record(check) for check in readiness.checks],
    )


@router.post("/operations/evidence", response_model=OperationsEvidenceRecord)
async def post_operations_evidence(
    payload: OperationsEvidenceCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    if payload.status not in {"pass", "warn", "fail"}:
        raise auth_error(422, "invalid_evidence_status", "Evidence status must be pass, warn, or fail.")
    evidence = await persist_operations_evidence(
        session,
        evidence_type=payload.evidence_type,
        status=payload.status,
        summary=payload.summary,
        reference_url=payload.reference_url,
        metadata=payload.metadata,
        created_by=_actor(auth_context),
    )
    return _evidence_record(evidence)


@router.get("/operations/evidence", response_model=list[OperationsEvidenceRecord])
async def get_operations_evidence(
    evidence_type: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    evidence = await list_operations_evidence(session, evidence_type=evidence_type)
    return [_evidence_record(item) for item in evidence]


@router.post("/operations/release-gates", response_model=ReleaseGateRecord)
async def post_release_gate(
    payload: ReleaseGateCreateRequest,
    request: Request,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    readiness = await compute_operations_readiness(session, settings=get_settings())
    gate = await persist_release_gate(
        session,
        release_version=payload.release_version,
        created_by=payload.created_by,
        trace_id=_trace_id(request),
        readiness=readiness,
    )
    return await _gate_record(session, gate)


@router.get("/operations/release-gates", response_model=list[ReleaseGateRecord])
async def get_release_gates(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    gates = await list_release_gates(session)
    return [await _gate_record(session, gate) for gate in gates]


@router.get("/operations/release-gates/{gate_id}", response_model=ReleaseGateRecord)
async def get_release_gate_by_id(
    gate_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    gate = await get_release_gate(session, gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="Release gate not found.")
    return await _gate_record(session, gate)


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
    if payload.role not in {"viewer", "operator", "admin"}:
        raise auth_error(422, "invalid_role", "Role must be viewer, operator, or admin.")
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


@router.get("/pricing", response_model=list[PricingCatalogRecord])
async def get_pricing_catalog(
    active_only: bool = False,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    return [_pricing_record(item) for item in await list_pricing_items(session, active_only=active_only)]


@router.post("/pricing", response_model=PricingCatalogRecord)
async def post_pricing_catalog_item(
    payload: PricingCatalogCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    return _pricing_record(await create_pricing_item(session, payload))


@router.patch("/pricing/{pricing_id}", response_model=PricingCatalogRecord)
async def patch_pricing_catalog_item(
    pricing_id: str,
    payload: PricingCatalogUpdateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    item = await update_pricing_item(session, pricing_id, payload)
    if item is None:
        raise HTTPException(status_code=404, detail="Pricing item not found.")
    return _pricing_record(item)


@router.delete("/pricing/{pricing_id}", response_model=PricingCatalogRecord)
async def delete_pricing_catalog_item(
    pricing_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    item = await disable_pricing_item(session, pricing_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pricing item not found.")
    return _pricing_record(item)


@router.post("/usage-events", response_model=UsageEventRecord)
async def post_usage_event(
    payload: UsageEventCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "operator", "admin")
    tenant_id = _query_tenant(payload.tenant_id or "default", auth_context) or payload.tenant_id or "default"
    payload = payload.model_copy(update={"tenant_id": tenant_id})
    return _usage_record(await record_usage_event(session, payload))


@router.get("/usage-events/runs/{run_id}", response_model=list[UsageEventRecord])
async def get_run_usage_events(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is not None:
        _query_tenant(run.tenant_id, auth_context)
    events = await list_usage_events(session, run_id=run_id)
    if run is None and events:
        _query_tenant(events[0].tenant_id, auth_context)
    return [_usage_record(item) for item in events]


@router.get("/usage-events/tenants/{tenant_id}", response_model=list[UsageEventRecord])
async def get_tenant_usage_events(
    tenant_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context) or tenant_id
    return [_usage_record(item) for item in await list_usage_events(session, tenant_id=tenant_id)]


@router.get("/cost/runs/{run_id}", response_model=RunCostSummaryResponse)
async def get_run_cost(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is not None:
        _query_tenant(run.tenant_id, auth_context)
    summary = await run_cost_summary(session, run_id=run_id)
    if run is None and not summary.cost_events:
        raise HTTPException(status_code=404, detail="Run cost not found.")
    if run is None and summary.tenant_id:
        _query_tenant(summary.tenant_id, auth_context)
    return _run_cost_response(summary)


@router.get("/cost/tenants/{tenant_id}", response_model=TenantCostSummaryResponse)
async def get_tenant_cost(
    tenant_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context) or tenant_id
    return _tenant_cost_response(await tenant_cost_summary(session, tenant_id=tenant_id))


@router.get("/cost/workflows/{workflow_id}")
async def get_workflow_cost(
    workflow_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(None, auth_context)
    return await workflow_cost_summary(session, workflow_id=workflow_id, tenant_id=tenant_id)


@router.get("/cost/models")
async def get_model_costs(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(None, auth_context)
    return await model_cost_ranking(session, tenant_id=tenant_id)


@router.get("/budgets", response_model=list[BudgetPolicyRecord])
async def get_budget_policies(
    tenant_id: str | None = None,
    active_only: bool = False,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context)
    return [
        _budget_record(item)
        for item in await list_budget_policies(session, tenant_id=tenant_id, active_only=active_only)
    ]


@router.post("/budgets", response_model=BudgetPolicyRecord)
async def post_budget_policy(
    payload: BudgetPolicyCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    tenant_id = _query_tenant(payload.tenant_id or "default", auth_context) or payload.tenant_id or "default"
    payload = payload.model_copy(update={"tenant_id": tenant_id})
    return _budget_record(await create_budget_policy(session, payload))


@router.patch("/budgets/{budget_id}", response_model=BudgetPolicyRecord)
async def patch_budget_policy(
    budget_id: str,
    payload: BudgetPolicyUpdateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    item = await update_budget_policy(session, budget_id, payload)
    if item is None:
        raise HTTPException(status_code=404, detail="Budget policy not found.")
    _query_tenant(item.tenant_id, auth_context)
    return _budget_record(item)


@router.get("/budget-violations", response_model=list[BudgetViolationRecord])
async def get_budget_violations(
    tenant_id: str | None = None,
    run_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context)
    return [
        _violation_record(item)
        for item in await list_budget_violations(session, tenant_id=tenant_id, run_id=run_id, limit=limit)
    ]


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
    disabled_tools = list((await state_store.get_open_breakers(agent_request.allowed_tools)).keys())
    result = orchestrator.submit(agent_request, disabled_tools=disabled_tools)
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
    disabled_tools = list((await state_store.get_open_breakers(agent_request.allowed_tools)).keys())
    result = orchestrator.submit(agent_request, disabled_tools=disabled_tools)
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
    run = await apply_approval_decision(session, run_id, decision)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    _query_tenant(run.tenant_id, auth_context)
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
        raise HTTPException(status_code=404, detail="Run not found.")
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
        raise HTTPException(status_code=404, detail="Run not found.")
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
        raise HTTPException(status_code=404, detail="Run not found.")
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
        raise HTTPException(status_code=404, detail="Run not found.")
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
        raise HTTPException(status_code=404, detail="Run not found.")
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
        raise HTTPException(status_code=404, detail="Run not found.")
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
    return await list_tool_executions(session, run_id=run_id)


@router.get("/worker/dlq", response_model=list[WorkerAttemptRecord])
async def get_worker_dlq(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    return await list_worker_attempts(session, status="dlq")


@router.get("/evaluations", response_model=list[EvaluationRecord])
async def get_evaluations(
    task_type: str | None = None,
    verdict: str | None = None,
    tenant_id: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    tenant_id = _query_tenant(tenant_id, auth_context)
    return await list_evaluation_results(
        session,
        task_type=task_type,
        verdict=verdict,
        tenant_id=tenant_id,
    )


@router.get("/observability/summary")
async def get_observability_summary(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
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
