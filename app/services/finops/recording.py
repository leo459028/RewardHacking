"""Run-level usage recording for submit, worker, checkpoint and replay paths."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.agent_run import AgentRun
from app.models.checkpoint import ReplayAttempt, RunCheckpoint
from app.models.control_plane import ExecutionPlan
from app.models.incident import Incident
from app.observability.metrics import (
    INCIDENTS_TOTAL,
)
from app.schemas.agent import AgentRequest
from app.schemas.finops import (
    UsageEventCreateRequest,
)
from app.services.finops.budget_enforcement import (
    _check_budgets,
    apply_budget_enforcement_to_run,
)
from app.services.finops.core import (
    BudgetEnforcementOutcome,
)
from app.services.finops.leases import (
    acquire_run_budget_lease,
    release_run_budget_lease,
    settle_run_budget_lease,
)
from app.services.finops.ledger import (
    _record_cost_for_usage,
    _usage_from_payload,
    extract_allocation,
)
from app.services.run_state import RunStateStore


def _token_estimate(text: str) -> int:
    return max(1, len(text.split()) * 4)


def _resolve_run_allocation(route: dict, meta: dict) -> tuple[dict[str, object], list[str]]:
    raw_alloc = route.get("allocation")
    if isinstance(raw_alloc, dict):
        warnings = list(route.get("attribution_warnings") or [])
        return raw_alloc, warnings
    return extract_allocation(meta)


_TOOL_RISK_ORDER = {"": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _mcp_tool_attribution(request: AgentRequest, plan: ExecutionPlan | None) -> dict[str, object]:
    """Resolve per-tool / per-MCP-server attribution for the step_2_tools usage event (v1.5.7)."""
    route = dict((plan.model_route if plan is not None else {}) or {})
    mcp_route = dict(route.get("mcp_route") or {})
    selected = list((plan.selected_tools if plan is not None else []) or [])
    tool_names = [str(item.get("name")) for item in selected if item.get("name")]
    if not tool_names:
        tool_names = list(request.allowed_tools or ["summarize"])
    tool_mcp_map = {str(k): str(v) for k, v in dict(mcp_route.get("tool_mcp_map") or {}).items()}
    if not tool_mcp_map:
        gateway_map = dict(((request.metadata or {}).get("mcp_gateway") or {}).get("tool_mcp_map") or {})
        tool_mcp_map = {str(k): str(v) for k, v in gateway_map.items()}
    for name in tool_names:
        tool_mcp_map.setdefault(name, "mcp-core-local")
    tool_mcp_map = {name: tool_mcp_map[name] for name in tool_names}
    mcp_servers = list(dict.fromkeys(tool_mcp_map.values()))
    primary = str(mcp_route.get("primary_mcp_server_id") or (mcp_servers[0] if mcp_servers else "mcp-core-local"))
    risk = ""
    for item in selected:
        level = str(item.get("risk_level") or "")
        if _TOOL_RISK_ORDER.get(level, 0) > _TOOL_RISK_ORDER.get(risk, 0):
            risk = level
    primary_tool = tool_names[0] if tool_names else "summarize"
    return {
        "mcp_server_id": primary,
        "mcp_servers": mcp_servers,
        "tool_mcp_map": tool_mcp_map,
        "tools": tool_names,
        "tool_id": primary_tool[:128],
        "tool_name": primary_tool[:128],
        "tool_provider": primary[:128],
        "tool_risk_level": (risk or "low")[:32],
    }


def _mcp_usage_fields(attr: dict[str, object]) -> dict[str, object]:
    return {
        "tool_id": attr["tool_id"],
        "tool_name": attr["tool_name"],
        "tool_provider": attr["tool_provider"],
        "mcp_server_id": str(attr["mcp_server_id"])[:128],
        "tool_risk_level": attr["tool_risk_level"],
    }


def _mcp_usage_metadata(attr: dict[str, object]) -> dict[str, object]:
    return {
        "mcp_server_id": attr["mcp_server_id"],
        "mcp_servers": list(attr["mcp_servers"]),  # type: ignore[arg-type]
        "tool_mcp_map": dict(attr["tool_mcp_map"]),  # type: ignore[arg-type]
        "tools": list(attr["tools"]),  # type: ignore[arg-type]
        "tool_id": attr["tool_id"],
    }


async def record_submit_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    result: object | None = None,
    state_store: RunStateStore | None = None,
) -> BudgetEnforcementOutcome:
    if run.status == "denied":
        return BudgetEnforcementOutcome(decision="allow")
    request = AgentRequest.model_validate(run.request_payload)
    workflow_id = plan.workflow_id if plan is not None else request.task_type
    trace_id = (plan.trace_id if plan is not None else request.metadata.get("trace_id", "")) or ""
    route = dict((plan.model_route if plan is not None else {}) or {})
    meta = dict(request.metadata or {})
    routed_provider = str(route.get("provider") or meta.get("provider") or "internal").strip() or "internal"
    routed_model = (
        str(route.get("model") or meta.get("model_name") or "deterministic-control-plane").strip()
        or "deterministic-control-plane"
    )
    routed_pricing_mode = str(route.get("pricing_mode") or meta.get("pricing_mode") or "standard").strip() or "standard"
    routed_service_tier = str(route.get("service_tier") or meta.get("service_tier") or "standard").strip() or "standard"
    routed_project_id = str(route.get("project_id") or meta.get("project_id") or "").strip()
    alloc, _ = _resolve_run_allocation(route, meta)
    alloc_cost_center = str(alloc.get("cost_center") or "")
    alloc_org_id = str(alloc.get("org_id") or "")
    alloc_env = str(alloc.get("environment") or "dev")
    explicit_env = str(alloc.get("explicit_environment") or "")
    custom_tags = dict(alloc.get("allocation_tags") or {})
    alloc_meta_extra: dict[str, object] = {}
    if explicit_env:
        alloc_meta_extra["explicit_environment"] = explicit_env
    if custom_tags:
        alloc_meta_extra["allocation_tags"] = custom_tags

    tool_count = max(1, len(request.allowed_tools or ["summarize"]))
    mcp_attr = _mcp_tool_attribution(request, plan)
    input_tokens = _token_estimate(request.prompt + " " + request.goal)
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_1_plan",
            provider=routed_provider,
            model_name=routed_model,
            service_tier=routed_service_tier,
            pricing_mode=routed_pricing_mode,
            resource_type="model",
            resource_subtype="chat_completion",
            usage_unit="input_tokens",
            quantity=input_tokens,
            input_tokens=input_tokens,
            phase="estimated",
            metadata={"source": "submit", "pricing_mode": routed_pricing_mode, **alloc_meta_extra},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_2_tools",
            resource_type="tool",
            usage_unit="tool_call",
            quantity=tool_count,
            tool_call_count=tool_count,
            phase="estimated",
            **_mcp_usage_fields(mcp_attr),
            metadata={**_mcp_usage_metadata(mcp_attr), "requested_tools": request.allowed_tools, **alloc_meta_extra},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_3_replay_bundle",
            resource_type="storage",
            usage_unit="artifact_mb",
            quantity=Decimal("0.01"),
            phase="estimated",
            metadata={"source": "replay_bundle_placeholder", **alloc_meta_extra},
        ),
    ]
    outcome = await _record_events_and_budget(
        session,
        run=run,
        plan=plan,
        result=result,
        events=events,
        enforce_active_guardrails=True,
    )
    if run.status == "denied" or outcome.decision == "fail_closed":
        await release_run_budget_lease(
            session,
            state_store,
            run_id=run.run_id,
            tenant_id=run.tenant_id,
            reason=outcome.reason or run.decision_reason or "finops_fail_closed",
        )
    elif run.status in {"queued", "awaiting_approval"}:
        await acquire_run_budget_lease(
            session,
            state_store,
            run=run,
            plan=plan,
        )
    await session.commit()
    return outcome


async def record_actual_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    is_failed_retry: bool = False,
    state_store: RunStateStore | None = None,
) -> None:
    request = AgentRequest.model_validate(run.request_payload)
    workflow_id = plan.workflow_id if plan is not None else request.task_type
    trace_id = (plan.trace_id if plan is not None else request.metadata.get("trace_id", "")) or ""
    route = dict((plan.model_route if plan is not None else {}) or {})
    meta = dict(request.metadata or {})
    routed_provider = str(route.get("provider") or meta.get("provider") or "internal").strip() or "internal"
    routed_model = (
        str(route.get("model") or meta.get("model_name") or "deterministic-control-plane").strip()
        or "deterministic-control-plane"
    )
    routed_pricing_mode = str(route.get("pricing_mode") or meta.get("pricing_mode") or "standard").strip() or "standard"
    routed_service_tier = str(route.get("service_tier") or meta.get("service_tier") or "standard").strip() or "standard"
    routed_project_id = str(route.get("project_id") or meta.get("project_id") or "").strip()
    alloc, _ = _resolve_run_allocation(route, meta)
    alloc_cost_center = str(alloc.get("cost_center") or "")
    alloc_org_id = str(alloc.get("org_id") or "")
    alloc_env = str(alloc.get("environment") or "dev")
    explicit_env = str(alloc.get("explicit_environment") or "")
    custom_tags = dict(alloc.get("allocation_tags") or {})

    usage_status = "failed" if is_failed_retry else "success"
    cost_meta_base: dict[str, object] = {"source": "worker"}
    if is_failed_retry:
        cost_meta_base["cost_type"] = "failed_retry"
    if explicit_env:
        cost_meta_base["explicit_environment"] = explicit_env
    if custom_tags:
        cost_meta_base["allocation_tags"] = custom_tags
    transcript = run.evaluation_summary.get("execution_event", {}).get("payload", {}).get("tool_transcript", [])
    output_tokens = _token_estimate(run.execution_output or "worker_attempt")
    mcp_attr = _mcp_tool_attribution(request, plan)
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_2_tools",
            resource_type="tool",
            usage_unit="tool_call",
            quantity=max(1, len(transcript)),
            tool_call_count=max(1, len(transcript)),
            phase="actual",
            status=usage_status,
            **_mcp_usage_fields(mcp_attr),
            metadata={**cost_meta_base, **_mcp_usage_metadata(mcp_attr), "completed_tools": len(transcript)},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_3_evaluation",
            provider=routed_provider,
            model_name=routed_model,
            service_tier=routed_service_tier,
            pricing_mode=routed_pricing_mode,
            resource_type="model",
            resource_subtype="chat_completion",
            usage_unit="output_tokens",
            quantity=output_tokens,
            output_tokens=output_tokens,
            phase="actual",
            status=usage_status,
            metadata={**cost_meta_base, "pricing_mode": routed_pricing_mode},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="worker_runtime",
            resource_type="runtime",
            usage_unit="worker_seconds",
            quantity=1,
            worker_seconds=Decimal("1"),
            phase="actual",
            status=usage_status,
            metadata=dict(cost_meta_base),
        ),
    ]
    outcome = await _record_events_and_budget(session, run=run, plan=plan, events=events)
    if run.status in {"completed", "failed", "review_required", "rejected"}:
        await settle_run_budget_lease(session, state_store, run=run, plan=plan)
    if outcome.decision == "fail_closed":
        flags = list(run.policy_flags or [])
        if "finops_runtime_budget_exceeded" not in flags:
            flags.append("finops_runtime_budget_exceeded")
            run.policy_flags = flags
        session.add(
            Incident(
                run_id=run.run_id,
                severity="high",
                code="finops_runtime_budget_exceeded",
                detail=outcome.reason or "Runtime actual cost exceeded hard budget limit.",
            )
        )
        INCIDENTS_TOTAL.labels(severity="high", code="finops_runtime_budget_exceeded").inc()
    await session.commit()


async def record_checkpoint_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    checkpoint: RunCheckpoint,
) -> None:
    workflow_id = plan.workflow_id if plan is not None else run.task_type
    trace_id = (plan.trace_id if plan is not None else checkpoint.trace_id) or ""
    await _record_events_and_budget(
        session,
        run=run,
        events=[
            UsageEventCreateRequest(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.run_id,
                trace_id=trace_id,
                workflow_id=workflow_id,
                checkpoint_id=checkpoint.checkpoint_id,
                resume_attempt_id=checkpoint.checkpoint_id,
                resource_type="storage",
                usage_unit="checkpoint_write",
                quantity=1,
                phase="resume",
                metadata={"checkpoint_type": checkpoint.checkpoint_type},
            )
        ],
    )


async def record_replay_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan,
    attempt: ReplayAttempt,
) -> None:
    await _record_events_and_budget(
        session,
        run=run,
        events=[
            UsageEventCreateRequest(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.run_id,
                trace_id=attempt.trace_id,
                workflow_id=plan.workflow_id,
                replay_attempt_id=attempt.replay_id,
                resource_type="runtime",
                usage_unit="replay_attempt",
                quantity=1,
                dry_run=True,
                phase="replay",
                metadata={"mode": attempt.mode, "external_execution": False},
            )
        ],
    )


async def _record_events_and_budget(
    session: AsyncSession,
    *,
    run: AgentRun,
    events: list[UsageEventCreateRequest],
    plan: ExecutionPlan | None = None,
    result: object | None = None,
    enforce_active_guardrails: bool = False,
) -> BudgetEnforcementOutcome:
    for event in events:
        usage = _usage_from_payload(event)
        session.add(usage)
        await session.flush()
        await _record_cost_for_usage(session, usage)
    outcome = await _check_budgets(session, run=run, plan=plan)
    if enforce_active_guardrails and get_settings().finops_budget_enforcement_enabled:
        await apply_budget_enforcement_to_run(
            session,
            run=run,
            plan=plan,
            result=result,
            outcome=outcome,
        )
    await session.commit()
    return outcome
