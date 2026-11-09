"""FinOps API endpoints (v1.6.0 D27: moved from ``app/api/routes.py``; paths and responses unchanged)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.api.deps import (
    get_state_store,
)
from app.infra.postgres import get_session
from app.schemas.finops import (
    BudgetPolicyCreateRequest,
    BudgetPolicyRecord,
    BudgetPolicyUpdateRequest,
    BudgetViolationAcknowledgeRequest,
    BudgetViolationRecord,
    BudgetViolationResolveRequest,
    CostAdjustmentCreateRequest,
    CostEventRecord,
    PricingCatalogCreateRequest,
    PricingCatalogRecord,
    PricingChangeLogRecord,
    PricingImportBatchRecord,
    PricingImportReportResponse,
    PricingImportRequest,
    PricingCatalogUpdateRequest,
    PricingSourceDocumentRecord,
    RunCostSummaryResponse,
    TenantBurnRateForecastResponse,
    TenantCostSummaryResponse,
    TenantTokenLeasesResponse,
    TokenBudgetLeaseRecord,
    TokenLeaseReleaseRequest,
    UsageEventCreateRequest,
    UsageEventRecord,
)
from app.services.auth import (
    AuthContext,
    auth_error,
    get_auth_context,
    require_role,
)
from app.services.audit_log import (
    get_run,
)
from app.services.run_state import RunStateStore
from app.services.finops import (
    acknowledge_budget_violation,
    budget_to_dict,
    compute_tenant_burn_rate_forecast,
    cost_to_dict,
    create_budget_policy,
    create_cost_adjustment,
    create_pricing_item,
    disable_budget_policy,
    disable_pricing_item,
    export_tenant_cost_ledger,
    get_budget_policy,
    get_budget_violation,
    get_pricing_item,
    list_budget_policies,
    list_budget_violations,
    list_pricing_items,
    list_tenant_token_leases,
    list_usage_events,
    model_cost_ranking,
    pricing_to_dict,
    record_usage_event,
    release_run_budget_lease,
    render_cost_ledger_csv,
    resolve_budget_violation,
    run_cost_summary,
    tenant_cost_summary,
    update_budget_policy,
    update_pricing_item,
    usage_to_dict,
    violation_to_dict,
    workflow_cost_summary,
)
from app.models.finops import PricingChangeLog, PricingImportBatch, PricingSourceDocument
from app.services.pricing_import import apply_pricing_seed, dry_run_pricing_seed, load_pricing_seed
from app.api.common import _actor, _query_tenant

router = APIRouter(prefix="/v1", tags=["agent"])


def _require_finops_admin(auth_context: AuthContext) -> None:
    if not auth_context.enabled:
        return
    if auth_context.role not in {"super_admin", "finops_admin", "admin"}:
        raise auth_error(403, "insufficient_role", "FinOps admin role is required for this operation.")


def _require_finops_reviewer(auth_context: AuthContext) -> None:
    if not auth_context.enabled:
        return
    if auth_context.role not in {"super_admin", "finops_admin", "tenant_admin", "reviewer", "admin"}:
        raise auth_error(403, "insufficient_role", "FinOps reviewer role is required for this operation.")


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


def _pricing_import_batch_record(item) -> PricingImportBatchRecord:
    return PricingImportBatchRecord(
        import_batch_id=item.import_batch_id,
        source_file_name=item.source_file_name,
        source_file_hash=item.source_file_hash,
        import_mode=item.import_mode,
        status=item.status,
        created_by=item.created_by,
        created_at=item.created_at,
        applied_at=item.applied_at,
        summary=item.summary,
        error_details=item.error_details,
    )


def _pricing_change_record(item) -> PricingChangeLogRecord:
    return PricingChangeLogRecord(
        change_id=item.change_id,
        pricing_id_old=item.pricing_id_old,
        pricing_id_new=item.pricing_id_new,
        change_type=item.change_type,
        old_unit_price=item.old_unit_price,
        new_unit_price=item.new_unit_price,
        currency=item.currency,
        change_reason=item.change_reason,
        source_url=item.source_url,
        changed_by=item.changed_by,
        changed_at=item.changed_at,
        import_batch_id=item.import_batch_id,
    )


def _pricing_source_record(item) -> PricingSourceDocumentRecord:
    return PricingSourceDocumentRecord(
        source_id=item.source_id,
        provider=item.provider,
        source_url=item.source_url,
        source_type=item.source_type,
        checked_at=item.checked_at,
        content_hash=item.content_hash,
        confidence=item.confidence,
        notes=item.notes,
        created_at=item.created_at,
    )


def _run_cost_response(summary) -> RunCostSummaryResponse:
    return RunCostSummaryResponse(
        run_id=summary.run_id,
        tenant_id=summary.tenant_id,
        base_currency=summary.base_currency,
        estimated_cost=summary.estimated_cost,
        actual_cost=summary.actual_cost,
        settled_cost=summary.settled_cost,
        total_tax_amount=summary.total_tax_amount,
        total_billed_base_cost=summary.total_billed_base_cost,
        cost_delta=summary.cost_delta,
        cost_delta_percent=summary.cost_delta_percent,
        currency=summary.currency,
        budget_status=summary.budget_status,
        budget_policy_id=summary.budget_policy_id,
        budget_remaining=summary.budget_remaining,
        currency_breakdown=summary.currency_breakdown,
        provider_breakdown=summary.provider_breakdown,
        model_breakdown=summary.model_breakdown,
        tool_breakdown=summary.tool_breakdown,
        mcp_server_breakdown=summary.mcp_server_breakdown,
        step_breakdown=summary.step_breakdown,
        cost_type_breakdown=summary.cost_type_breakdown,
        resource_breakdown=summary.resource_breakdown,
        breakdown=summary.breakdown,
        replay_cost=summary.replay_cost,
        resume_incremental_cost=summary.resume_incremental_cost,
        failed_retry_cost=summary.failed_retry_cost,
        cache_savings=summary.cache_savings,
        batch_savings=summary.batch_savings,
        token_lease_status=summary.token_lease_status,
        token_lease_id=summary.token_lease_id,
        usage_events=[_usage_record(item) for item in summary.usage_events],
        cost_events=[_cost_record(item) for item in summary.cost_events],
    )


def _tenant_cost_response(summary) -> TenantCostSummaryResponse:
    return TenantCostSummaryResponse(
        tenant_id=summary.tenant_id,
        period_start=summary.period_start,
        period_end=summary.period_end,
        timezone=summary.timezone,
        base_currency=summary.base_currency,
        today_cost=summary.today_cost,
        month_to_date_cost=summary.month_to_date_cost,
        total_tax_amount=summary.total_tax_amount,
        total_billed_base_cost=summary.total_billed_base_cost,
        adjustment_total=summary.adjustment_total,
        reconciled_month_to_date_cost=summary.reconciled_month_to_date_cost,
        budget_used_percent=summary.budget_used_percent,
        violation_count=summary.violation_count,
        hourly_burn_rate=summary.hourly_burn_rate,
        daily_burn_rate=summary.daily_burn_rate,
        projected_month_end_cost=summary.projected_month_end_cost,
        burn_status=summary.burn_status,
        burn_rate_forecasts=summary.burn_rate_forecasts,
        active_lease_count=summary.active_lease_count,
        active_leased_tokens=summary.active_leased_tokens,
        active_leased_base_cost=summary.active_leased_base_cost,
        currency_breakdown=summary.currency_breakdown,
        cost_center_breakdown=summary.cost_center_breakdown,
        provider_breakdown=summary.provider_breakdown,
        resource_type_breakdown=summary.resource_type_breakdown,
        environment_breakdown=summary.environment_breakdown,
        project_breakdown=summary.project_breakdown,
        top_runs=summary.top_runs,
        top_models=summary.top_models,
        top_workflows=summary.top_workflows,
        top_tools=summary.top_tools,
        top_users=summary.top_users,
        mcp_server_breakdown=summary.mcp_server_breakdown,
        top_mcp_servers=summary.top_mcp_servers,
        failed_run_cost=summary.failed_run_cost,
        replay_cost=summary.replay_cost,
        cache_savings=summary.cache_savings,
        batch_savings=summary.batch_savings,
    )


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
    item = await update_pricing_item(session, pricing_id, payload, updated_by=_actor(auth_context))
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
    item = await disable_pricing_item(session, pricing_id, updated_by=_actor(auth_context))
    if item is None:
        raise HTTPException(status_code=404, detail="Pricing item not found.")
    return _pricing_record(item)


@router.get("/admin/finops/pricing", response_model=list[PricingCatalogRecord])
async def admin_get_pricing_catalog(
    provider: str | None = None,
    model: str | None = None,
    resource_type: str | None = None,
    pricing_mode: str | None = None,
    token_type: str | None = None,
    scope_type: str | None = None,
    active_only: bool = False,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_reviewer(auth_context)
    return [
        _pricing_record(item)
        for item in await list_pricing_items(
            session,
            active_only=active_only,
            provider=provider,
            model_name=model,
            resource_type=resource_type,
            pricing_mode=pricing_mode,
            token_type=token_type,
            scope_type=scope_type,
        )
    ]


@router.post("/admin/finops/pricing/import/dry-run", response_model=PricingImportReportResponse)
async def admin_dry_run_pricing_import(
    payload: PricingImportRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_reviewer(auth_context)
    seed = load_pricing_seed(payload.file_path)
    report = await dry_run_pricing_seed(
        session,
        seed,
        created_by=payload.created_by or _actor(auth_context),
        strict=payload.strict,
        allow_warnings=payload.allow_warnings,
    )
    return PricingImportReportResponse(**report.to_dict())


@router.post("/admin/finops/pricing/import/apply", response_model=PricingImportReportResponse)
async def admin_apply_pricing_import(
    payload: PricingImportRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_admin(auth_context)
    seed = load_pricing_seed(payload.file_path)
    report = await apply_pricing_seed(
        session,
        seed,
        created_by=payload.created_by or _actor(auth_context),
        base_currency=payload.base_currency,
        strict=payload.strict,
        allow_warnings=payload.allow_warnings,
    )
    return PricingImportReportResponse(**report.to_dict())


@router.get("/admin/finops/pricing/imports", response_model=list[PricingImportBatchRecord])
async def admin_list_pricing_imports(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_reviewer(auth_context)
    rows = list((await session.scalars(select(PricingImportBatch).order_by(PricingImportBatch.created_at.desc()))).all())
    return [_pricing_import_batch_record(item) for item in rows]


@router.get("/admin/finops/pricing/imports/{import_batch_id}", response_model=PricingImportBatchRecord)
async def admin_get_pricing_import(
    import_batch_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_reviewer(auth_context)
    item = await session.scalar(select(PricingImportBatch).where(PricingImportBatch.import_batch_id == import_batch_id))
    if item is None:
        raise HTTPException(status_code=404, detail="Pricing import batch not found.")
    return _pricing_import_batch_record(item)


@router.get("/admin/finops/pricing/sources", response_model=list[PricingSourceDocumentRecord])
async def admin_list_pricing_sources(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_reviewer(auth_context)
    rows = list((await session.scalars(select(PricingSourceDocument).order_by(PricingSourceDocument.created_at.desc()))).all())
    return [_pricing_source_record(item) for item in rows]


@router.get("/admin/finops/pricing/changes", response_model=list[PricingChangeLogRecord])
async def admin_list_pricing_changes(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_reviewer(auth_context)
    rows = list((await session.scalars(select(PricingChangeLog).order_by(PricingChangeLog.changed_at.desc()))).all())
    return [_pricing_change_record(item) for item in rows]


@router.get("/admin/finops/pricing/{pricing_id}", response_model=PricingCatalogRecord)
async def admin_get_pricing_item(
    pricing_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_reviewer(auth_context)
    item = await get_pricing_item(session, pricing_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pricing item not found.")
    return _pricing_record(item)


@router.patch("/admin/finops/pricing/{pricing_id}", response_model=PricingCatalogRecord)
async def admin_patch_pricing_item(
    pricing_id: str,
    payload: PricingCatalogUpdateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_admin(auth_context)
    item = await update_pricing_item(session, pricing_id, payload, updated_by=_actor(auth_context))
    if item is None:
        raise HTTPException(status_code=404, detail="Pricing item not found.")
    return _pricing_record(item)


@router.post("/admin/finops/pricing/{pricing_id}/disable", response_model=PricingCatalogRecord)
async def admin_disable_pricing_item(
    pricing_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_finops_admin(auth_context)
    item = await disable_pricing_item(session, pricing_id, updated_by=_actor(auth_context))
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
    project_id: str | None = None,
    cost_center: str | None = None,
    mcp_server_id: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context) or tenant_id
    return _tenant_cost_response(
        await tenant_cost_summary(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            cost_center=cost_center,
            mcp_server_id=mcp_server_id or None,
        )
    )


@router.get("/cost/tenants/{tenant_id}/forecast", response_model=TenantBurnRateForecastResponse)
async def get_tenant_cost_forecast(
    tenant_id: str,
    project_id: str | None = None,
    window_hours: int | None = Query(default=None, ge=1, le=720),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context) or tenant_id
    return await compute_tenant_burn_rate_forecast(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        window_hours=window_hours,
    )


@router.get("/cost/tenants/{tenant_id}/leases", response_model=TenantTokenLeasesResponse)
async def get_tenant_cost_leases(
    tenant_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context) or tenant_id
    return await list_tenant_token_leases(
        session,
        state_store,
        tenant_id=tenant_id,
    )


@router.post("/cost/leases/{run_id}/release", response_model=TokenBudgetLeaseRecord)
async def post_release_cost_lease(
    run_id: str,
    payload: TokenLeaseReleaseRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
):
    require_role(auth_context, "operator", "admin", "finops_admin", "super_admin")
    run = await get_run(session, run_id)
    if run is not None:
        _query_tenant(run.tenant_id, auth_context)
        if run.status == "running":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "run_currently_running",
                    "message": "Cannot manually release budget lease while run is actively running.",
                },
            )
    reason = payload.reason if payload is not None else "Manual lease release by operator"
    actor = (payload.released_by if (payload and payload.released_by) else "") or _actor(auth_context)
    released = await release_run_budget_lease(
        session,
        state_store,
        run_id=run_id,
        tenant_id=run.tenant_id if run is not None else None,
        reason=reason,
        actor=actor,
        record_governance_event=True,
    )
    if released is None:
        raise HTTPException(status_code=404, detail="Lease or run not found.")
    await session.commit()
    return released


@router.get("/cost/tenants/{tenant_id}/export")
async def export_tenant_cost(
    tenant_id: str,
    format: str = Query(default="json", pattern="^(json|csv)$"),
    project_id: str | None = None,
    cost_center: str | None = None,
    cost_type: str | None = None,
    provider: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    settled_only: bool = False,
    limit: int | None = Query(default=None, ge=1, le=10000),
    mcp_server_id: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin", "finops_admin", "super_admin", "tenant_admin")
    tenant_id = _query_tenant(tenant_id, auth_context) or tenant_id
    export_data = await export_tenant_cost_ledger(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        cost_center=cost_center,
        cost_type=cost_type,
        provider=provider,
        start_date=start_date,
        end_date=end_date,
        settled_only=settled_only,
        limit=limit,
        export_format=format,
        mcp_server_id=mcp_server_id or None,
    )
    if format == "csv":
        csv_text = render_cost_ledger_csv(export_data)
        safe_tenant = re.sub(r"[^A-Za-z0-9._-]", "_", tenant_id or "default")
        headers = {
            "Content-Disposition": f'attachment; filename="finops_ledger_{safe_tenant}.csv"',
        }
        if export_data.truncated:
            headers["X-Export-Truncated"] = "true"
        return Response(content=csv_text, media_type="text/csv; charset=utf-8", headers=headers)
    return export_data


@router.post("/cost/adjustments", response_model=CostEventRecord)
async def post_cost_adjustment(
    payload: CostAdjustmentCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin", "finops_admin", "super_admin")
    tenant_id = _query_tenant(payload.tenant_id or "default", auth_context) or payload.tenant_id or "default"
    payload = payload.model_copy(update={"tenant_id": tenant_id})
    try:
        item = await create_cost_adjustment(session, payload, actor=_actor(auth_context))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _cost_record(item)


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
    require_role(auth_context, "admin", "finops_admin", "super_admin")
    tenant_id = _query_tenant(payload.tenant_id or "default", auth_context) or payload.tenant_id or "default"
    payload = payload.model_copy(update={"tenant_id": tenant_id})
    return _budget_record(await create_budget_policy(session, payload))


@router.get("/budgets/{budget_id}", response_model=BudgetPolicyRecord)
async def get_budget_policy_by_id(
    budget_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    item = await get_budget_policy(session, budget_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Budget policy not found.")
    _query_tenant(item.tenant_id, auth_context)
    return _budget_record(item)


@router.patch("/budgets/{budget_id}", response_model=BudgetPolicyRecord)
async def patch_budget_policy(
    budget_id: str,
    payload: BudgetPolicyUpdateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin", "finops_admin", "super_admin")
    item = await update_budget_policy(session, budget_id, payload)
    if item is None:
        raise HTTPException(status_code=404, detail="Budget policy not found.")
    _query_tenant(item.tenant_id, auth_context)
    return _budget_record(item)


@router.post("/budgets/{budget_id}/disable", response_model=BudgetPolicyRecord)
async def post_disable_budget_policy(
    budget_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin", "finops_admin", "super_admin")
    existing = await get_budget_policy(session, budget_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Budget policy not found.")
    _query_tenant(existing.tenant_id, auth_context)
    item = await disable_budget_policy(session, budget_id)
    return _budget_record(item)


@router.get("/budget-violations", response_model=list[BudgetViolationRecord])
async def get_budget_violations(
    tenant_id: str | None = None,
    run_id: str | None = None,
    resolution_status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _query_tenant(tenant_id, auth_context)
    return [
        _violation_record(item)
        for item in await list_budget_violations(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            resolution_status=resolution_status,
            limit=limit,
        )
    ]


@router.get("/budget-violations/{violation_id}", response_model=BudgetViolationRecord)
async def get_budget_violation_by_id(
    violation_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    item = await get_budget_violation(session, violation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Budget violation not found.")
    _query_tenant(item.tenant_id, auth_context)
    return _violation_record(item)


@router.post("/budget-violations/{violation_id}/acknowledge", response_model=BudgetViolationRecord)
async def post_acknowledge_budget_violation(
    violation_id: str,
    payload: BudgetViolationAcknowledgeRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "operator", "reviewer", "finops_admin", "tenant_admin", "super_admin", "admin")
    existing = await get_budget_violation(session, violation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Budget violation not found.")
    _query_tenant(existing.tenant_id, auth_context)
    item = await acknowledge_budget_violation(
        session,
        violation_id,
        resolved_by=payload.acknowledged_by or payload.resolved_by or _actor(auth_context),
        notes=payload.note or payload.notes,
    )
    return _violation_record(item)


@router.post("/budget-violations/{violation_id}/resolve", response_model=BudgetViolationRecord)
async def post_resolve_budget_violation(
    violation_id: str,
    payload: BudgetViolationResolveRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "finops_admin", "super_admin", "admin")
    existing = await get_budget_violation(session, violation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Budget violation not found.")
    _query_tenant(existing.tenant_id, auth_context)
    item = await resolve_budget_violation(
        session,
        violation_id,
        resolution_status=payload.resolution_status,
        resolved_by=payload.resolved_by or _actor(auth_context),
        notes=payload.resolution_note or payload.note or payload.notes,
    )
    return _violation_record(item)
