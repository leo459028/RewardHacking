"""Run, tenant, workflow and model cost summaries."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.agent_run import AgentRun
from app.models.finops import (
    CostEvent,
)
from app.services.finops.budget_policies import (
    list_budget_policies,
    list_budget_violations,
)
from app.services.finops.core import (
    RunCostSummary,
    TenantCostSummary,
    _decimal,
    _money,
)
from app.services.finops.forecast import (
    _compute_burn_rate_forecasts_from_data,
)
from app.services.finops.leases import (
    _compute_active_leases_from_costs,
)
from app.services.finops.ledger import (
    _list_cost_events,
    list_usage_events,
)
from app.services.finops.pricing import (
    list_pricing_items,
)
from app.services.finops.settlement import (
    _billed_base_cost,
    _cost_matches_mcp_server,
    _cost_mcp_servers,
    _cost_tools,
    _pre_tax_base_cost,
    _settled_cost_events,
    _tax_base_cost,
    _total_base_cost,
    _total_cost,
)


async def _compute_savings_for_costs(
    session: AsyncSession,
    costs: list[CostEvent],
) -> tuple[Decimal, Decimal]:
    if not costs:
        return Decimal("0"), Decimal("0")
    active_pricing = await list_pricing_items(session, active_only=True)
    std_input_prices: dict[tuple[str, str], Decimal] = {}
    for row in active_pricing:
        if (
            row.pricing_mode == "standard"
            and row.token_type == "input"
            and row.input_threshold_tokens_min is None
        ):
            std_input_prices.setdefault((row.provider, row.model_name), _money(row.unit_price))

    cache_savings = Decimal("0")
    batch_savings = Decimal("0")
    for item in costs:
        snap = dict(item.pricing_snapshot or {})
        meta = dict(item.metadata_payload or {})
        token_type = str(snap.get("token_type") or meta.get("token_type") or "")
        pricing_mode = str(snap.get("pricing_mode") or meta.get("pricing_mode") or "")

        if token_type == "cached_input":
            std_price = std_input_prices.get((item.provider, item.model_name))
            actual_unit = _money(item.unit_price)
            if std_price is not None and std_price > actual_unit:
                cache_savings += _money((std_price - actual_unit) * _decimal(item.quantity))
            elif _decimal(item.discount_amount) > Decimal("0"):
                cache_savings += _money(item.discount_amount)

        if pricing_mode == "batch":
            diff = _money(_decimal(item.gross_cost) - _decimal(item.net_cost))
            if diff > Decimal("0"):
                batch_savings += diff
            else:
                std_price = std_input_prices.get((item.provider, item.model_name))
                actual_unit = _money(item.unit_price)
                if std_price is not None and std_price > actual_unit:
                    batch_savings += _money((std_price - actual_unit) * _decimal(item.quantity))
    return _money(cache_savings), _money(batch_savings)


async def run_cost_summary(session: AsyncSession, *, run_id: str) -> RunCostSummary:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    usage = await list_usage_events(session, run_id=run_id)
    costs = await _list_cost_events(session, run_id=run_id)
    estimated = _money(sum(_decimal(item.estimated_cost) for item in costs))
    actual = _money(sum(_decimal(item.actual_cost) for item in costs))
    operational_costs = [c for c in costs if c.cost_type != "adjustment"]
    settled_costs = _settled_cost_events(operational_costs)
    settled_ids = {c.cost_id for c in settled_costs}
    for item in costs:
        item.is_settled = (item.cost_id in settled_ids) or (item.cost_type == "adjustment")
        item.pre_tax_base_cost = _pre_tax_base_cost(item)

    settled_cost = _total_base_cost(settled_costs)
    total_tax_amount = _money(sum((_tax_base_cost(item) for item in settled_costs), Decimal("0")))
    total_billed_base_cost = _money(sum((_billed_base_cost(item) for item in settled_costs), Decimal("0")))
    currency_breakdown: dict[str, Decimal] = {}
    for item in settled_costs:
        curr = (item.currency or base_currency).upper()
        currency_breakdown[curr] = _money(currency_breakdown.get(curr, Decimal("0")) + _total_cost(item))

    tenant_id = usage[0].tenant_id if usage else (costs[0].tenant_id if costs else "")
    provider_breakdown = _breakdown(settled_costs, "provider")
    model_breakdown = _breakdown(settled_costs, "model_name")
    tool_breakdown = _tool_cost_distribution(settled_costs)
    mcp_server_breakdown = _mcp_server_cost_distribution(settled_costs)
    step_breakdown = _breakdown(settled_costs, "step_id")
    cost_type_breakdown = _breakdown(settled_costs, "cost_type")
    resource_breakdown = _breakdown(settled_costs, "resource_type")
    violations = await list_budget_violations(session, run_id=run_id)
    if any(item.decision in {"fail_closed", "require_approval"} for item in violations):
        budget_status = "exceeded"
    elif violations:
        budget_status = "warning"
    else:
        budget_status = "within_budget"
    cost_delta = _money(actual - estimated)
    cache_savings, batch_savings = await _compute_savings_for_costs(session, settled_costs)
    est_rows = [c for c in operational_costs if c.cost_type == "estimated"]
    act_rows = [c for c in operational_costs if c.cost_type == "actual"]
    first_est_meta = dict((est_rows[0].metadata_payload if est_rows else {}) or {})
    token_lease_id = str(first_est_meta.get("lease_id") or "")
    raw_lease_status = str(first_est_meta.get("lease_status") or "")
    if raw_lease_status == "released":
        token_lease_status = "released"
    elif act_rows or raw_lease_status == "settled":
        token_lease_status = "settled"
    elif est_rows:
        token_lease_status = "active"
    else:
        run_row = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        token_lease_status = "none (policy denied)" if (run_row and run_row.status == "denied") else "none"
    return RunCostSummary(
        run_id=run_id,
        tenant_id=tenant_id,
        base_currency=costs[0].base_currency if costs else base_currency,
        estimated_cost=estimated,
        actual_cost=actual,
        settled_cost=settled_cost,
        total_tax_amount=total_tax_amount,
        total_billed_base_cost=total_billed_base_cost,
        cost_delta=cost_delta,
        cost_delta_percent=_money((cost_delta / estimated * 100) if estimated else 0),
        currency=costs[0].currency if costs else base_currency,
        budget_status=budget_status,
        budget_policy_id=violations[0].budget_id if violations else "",
        budget_remaining=violations[0].remaining_budget_before if violations else Decimal("0"),
        currency_breakdown=currency_breakdown,
        provider_breakdown=provider_breakdown,
        model_breakdown=model_breakdown,
        tool_breakdown=tool_breakdown,
        mcp_server_breakdown=mcp_server_breakdown,
        step_breakdown=step_breakdown,
        cost_type_breakdown=cost_type_breakdown,
        resource_breakdown=resource_breakdown,
        breakdown={f"{key}_cost": value for key, value in resource_breakdown.items()},
        replay_cost=_money(sum((_pre_tax_base_cost(item) for item in settled_costs if item.replay_attempt_id), Decimal("0"))),
        resume_incremental_cost=_money(sum((_pre_tax_base_cost(item) for item in settled_costs if item.resume_attempt_id), Decimal("0"))),
        failed_retry_cost=_money(sum((_pre_tax_base_cost(item) for item in settled_costs if item.cost_type == "failed_retry"), Decimal("0"))),
        cache_savings=cache_savings,
        batch_savings=batch_savings,
        token_lease_status=token_lease_status,
        token_lease_id=token_lease_id,
        usage_events=usage,
        cost_events=costs,
    )


async def tenant_cost_summary(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None = None,
    cost_center: str | None = None,
    mcp_server_id: str | None = None,
) -> TenantCostSummary:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    costs = await _list_cost_events(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        cost_center=cost_center,
    )
    operational_costs = [c for c in costs if c.cost_type != "adjustment"]
    adjustment_costs = [c for c in costs if c.cost_type == "adjustment"]
    settled_costs = _settled_cost_events(operational_costs)
    if mcp_server_id:
        settled_costs = [c for c in settled_costs if _cost_matches_mcp_server(c, mcp_server_id)]
        adjustment_costs = []

    now = datetime.now(timezone.utc)
    today = now.date()
    period_start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
    if today.month == 12:
        period_end = datetime(today.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        period_end = datetime(today.year, today.month + 1, 1, tzinfo=timezone.utc)

    today_cost = Decimal("0")
    month_cost = Decimal("0")
    total_by_run: dict[str, Decimal] = {}
    total_by_model: dict[str, Decimal] = {}
    total_by_workflow: dict[str, Decimal] = {}
    total_by_tool: dict[str, Decimal] = {}
    total_by_user: dict[str, Decimal] = {}
    currency_breakdown: dict[str, Decimal] = {}

    for item in settled_costs:
        amount = _pre_tax_base_cost(item)
        curr = (item.currency or base_currency).upper()
        currency_breakdown[curr] = _money(currency_breakdown.get(curr, Decimal("0")) + _total_cost(item))
        total_by_run[item.run_id] = total_by_run.get(item.run_id, Decimal("0")) + amount
        if item.model_name:
            total_by_model[item.model_name] = total_by_model.get(item.model_name, Decimal("0")) + amount
        if item.workflow_id:
            total_by_workflow[item.workflow_id] = total_by_workflow.get(item.workflow_id, Decimal("0")) + amount
        if item.user_id:
            total_by_user[item.user_id] = total_by_user.get(item.user_id, Decimal("0")) + amount
        if item.created_at and item.created_at.date() == today:
            today_cost += amount
        if item.created_at and item.created_at.year == today.year and item.created_at.month == today.month:
            month_cost += amount

    total_by_tool.update(_tool_cost_distribution(settled_costs))
    mcp_server_breakdown = _mcp_server_cost_distribution(settled_costs)
    total_tax_amount = _money(sum((_tax_base_cost(item) for item in settled_costs), Decimal("0")))
    total_billed_base_cost = _money(sum((_billed_base_cost(item) for item in settled_costs), Decimal("0")))
    adjustment_total = _money(sum((_pre_tax_base_cost(item) for item in adjustment_costs), Decimal("0")))
    reconciled_mtd = _money(_money(month_cost) + adjustment_total)

    violations = await list_budget_violations(session, tenant_id=tenant_id)
    budgets = await list_budget_policies(session, tenant_id=tenant_id, active_only=True)
    monthly_limit = Decimal("0")
    for item in budgets:
        if item.period != "monthly":
            continue
        if project_id:
            if item.scope != "project" or (item.project_id or item.scope_id) != project_id:
                continue
        else:
            if item.scope != "tenant":
                continue
        raw_limit = _decimal(item.hard_limit_amount or item.limit_amount or item.soft_limit_amount)
        p_curr = (item.currency or base_currency).strip().upper()
        p_fx = settings.finops_fx_rates.get(p_curr, Decimal("1")) if p_curr != base_currency else Decimal("1")
        monthly_limit += _money(raw_limit * p_fx)

    forecast_resp = _compute_burn_rate_forecasts_from_data(
        budgets,
        costs,
        tenant_id=tenant_id,
        project_id=project_id,
        now=now,
    )
    leases_resp = _compute_active_leases_from_costs(costs, tenant_id=tenant_id, now=now)
    cache_savings, batch_savings = await _compute_savings_for_costs(session, settled_costs)
    return TenantCostSummary(
        tenant_id=tenant_id,
        period_start=period_start,
        period_end=period_end,
        timezone="UTC",
        base_currency=base_currency,
        today_cost=_money(today_cost),
        month_to_date_cost=_money(month_cost),
        total_tax_amount=total_tax_amount,
        total_billed_base_cost=total_billed_base_cost,
        adjustment_total=adjustment_total,
        reconciled_month_to_date_cost=reconciled_mtd,
        budget_used_percent=_money((month_cost / monthly_limit * 100) if monthly_limit else 0),
        violation_count=len(violations),
        hourly_burn_rate=forecast_resp.hourly_burn_rate,
        daily_burn_rate=forecast_resp.daily_burn_rate,
        projected_month_end_cost=forecast_resp.projected_month_end_cost,
        burn_status=forecast_resp.burn_status,
        burn_rate_forecasts=forecast_resp.burn_rate_forecasts,
        active_lease_count=leases_resp.active_lease_count,
        active_leased_tokens=leases_resp.active_leased_tokens,
        active_leased_base_cost=leases_resp.active_leased_base_cost,
        currency_breakdown=currency_breakdown,
        cost_center_breakdown=_breakdown(settled_costs, "cost_center"),
        provider_breakdown=_breakdown(settled_costs, "provider"),
        resource_type_breakdown=_breakdown(settled_costs, "resource_type"),
        environment_breakdown=_breakdown(settled_costs, "environment"),
        project_breakdown=_breakdown(settled_costs, "project_id"),
        top_runs=_top_items(total_by_run, "run_id"),
        top_models=_top_items(total_by_model, "model_name"),
        top_workflows=_top_items(total_by_workflow, "workflow_id"),
        top_tools=_top_items(total_by_tool, "tool_id"),
        top_users=_top_items(total_by_user, "user_id"),
        mcp_server_breakdown=mcp_server_breakdown,
        top_mcp_servers=_top_items(mcp_server_breakdown, "mcp_server_id"),
        failed_run_cost=_money(sum((_pre_tax_base_cost(item) for item in settled_costs if item.cost_type == "failed_retry"), Decimal("0"))),
        replay_cost=_money(sum((_pre_tax_base_cost(item) for item in settled_costs if item.replay_attempt_id), Decimal("0"))),
        cache_savings=cache_savings,
        batch_savings=batch_savings,
    )


def _breakdown(costs: list[CostEvent], attr: str) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for item in costs:
        key = str(getattr(item, attr, "") or "")
        if not key:
            continue
        values[key] = values.get(key, Decimal("0")) + _pre_tax_base_cost(item)
    return {key: _money(value) for key, value in values.items()}


def _split_evenly(amount: Decimal, count: int) -> list[Decimal]:
    if count <= 0:
        return []
    share = _money(amount / count)
    parts = [share] * (count - 1)
    parts.append(_money(amount - share * (count - 1)))
    return parts


def _tool_cost_distribution(costs: list[CostEvent]) -> dict[str, Decimal]:
    """Split each tool CostEvent evenly across its tools; the last tool absorbs rounding."""
    values: dict[str, Decimal] = {}
    for item in costs:
        if item.resource_type != "tool":
            continue
        tools = _cost_tools(item)
        if not tools:
            continue
        for name, part in zip(tools, _split_evenly(_pre_tax_base_cost(item), len(tools))):
            values[name] = values.get(name, Decimal("0")) + part
    return {key: _money(value) for key, value in values.items()}


def _mcp_server_cost_distribution(costs: list[CostEvent]) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for item in costs:
        if item.resource_type != "tool":
            continue
        meta = dict(item.metadata_payload or {})
        tools = _cost_tools(item)
        tool_map = dict(meta.get("tool_mcp_map") or {})
        if tools and tool_map:
            owners = [str(tool_map.get(name) or meta.get("mcp_server_id") or "mcp-core-local") for name in tools]
        else:
            owners = _cost_mcp_servers(item)
        if not owners:
            continue
        for owner, part in zip(owners, _split_evenly(_pre_tax_base_cost(item), len(owners))):
            values[owner] = values.get(owner, Decimal("0")) + part
    return {key: _money(value) for key, value in values.items()}


def _top_items(values: dict[str, Decimal], key: str) -> list[dict]:
    return [
        {key: name, "cost": _money(cost)}
        for name, cost in sorted(values.items(), key=lambda item: item[1], reverse=True)[:10]
        if name
    ]


async def workflow_cost_summary(
    session: AsyncSession,
    *,
    workflow_id: str,
    tenant_id: str | None = None,
) -> dict:
    costs = await _list_cost_events(session, workflow_id=workflow_id, tenant_id=tenant_id)
    settled = _settled_cost_events([c for c in costs if c.cost_type != "adjustment"])
    return {
        "workflow_id": workflow_id,
        "tenant_id": tenant_id or "",
        "total_cost": _total_base_cost(settled),
    }


async def model_cost_ranking(session: AsyncSession, *, tenant_id: str | None = None) -> list[dict]:
    costs = await _list_cost_events(session, tenant_id=tenant_id)
    settled = _settled_cost_events([c for c in costs if c.cost_type != "adjustment"])
    values: dict[str, Decimal] = {}
    for item in settled:
        if item.model_name:
            values[item.model_name] = values.get(item.model_name, Decimal("0")) + _pre_tax_base_cost(item)
    return _top_items(values, "model_name")
