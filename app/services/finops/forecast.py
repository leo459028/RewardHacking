"""Burn-rate and budget exhaustion forecasting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.finops import (
    BudgetPolicy,
    CostEvent,
)
from app.observability.metrics import (
    FINOPS_BURN_RATE_ALERTS_TOTAL,
)
from app.schemas.finops import (
    PolicyBurnRateForecastRecord,
    TenantBurnRateForecastResponse,
)
from app.services.finops.budget_policies import (
    _matching_costs_for_policy,
    _resolve_period_bounds,
    list_budget_policies,
)
from app.services.finops.core import (
    _ensure_utc,
    _money,
)
from app.services.finops.ledger import (
    _list_cost_events,
)
from app.services.finops.settlement import (
    _cost_for_basis,
    _pre_tax_base_cost,
    _settled_cost_events,
)


def _compute_burn_rate_forecasts_from_data(
    policies: list[BudgetPolicy],
    tenant_costs: list[CostEvent],
    *,
    tenant_id: str,
    project_id: str | None = None,
    window_hours: int | None = None,
    now: datetime | None = None,
    record_metrics: bool = False,
) -> TenantBurnRateForecastResponse:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    eff_now = _ensure_utc(now) or datetime.now(timezone.utc)
    eff_window_hours = int(window_hours if window_hours is not None else settings.finops_burn_rate_window_hours)
    min_elapsed = Decimal(str(settings.finops_burn_rate_min_elapsed_hours))
    min_samples = int(settings.finops_burn_rate_min_samples)
    window_start = eff_now - timedelta(hours=eff_window_hours)

    forecasts: list[PolicyBurnRateForecastRecord] = []
    status_priority = {"normal": 0, "elevated_burn": 1, "critical_burn": 2}
    overall_status = "normal"

    for policy in policies:
        eff_from = _ensure_utc(policy.effective_from)
        eff_to = _ensure_utc(policy.effective_to)
        if eff_from is not None and eff_from > eff_now:
            continue
        if eff_to is not None and eff_to < eff_now:
            continue
        if project_id and policy.scope == "project" and (policy.project_id or policy.scope_id) not in {"", project_id}:
            continue
        bounds_info = _resolve_period_bounds(policy, eff_now)
        if bounds_info is None:
            continue
        p_start, p_end, _ = bounds_info
        matching = _matching_costs_for_policy(tenant_costs, policy, eff_now, bounds=(p_start, p_end))
        settled_matching = _settled_cost_events(matching)
        settled_period_spend = _money(
            sum((_cost_for_basis(c, policy.budget_basis) for c in settled_matching), Decimal("0"))
        )
        window_events = [
            c
            for c in settled_matching
            if (_ensure_utc(c.created_at) or eff_now) >= window_start
        ]
        window_spend = _money(
            sum((_cost_for_basis(c, policy.budget_basis) for c in window_events), Decimal("0"))
        )

        elapsed_seconds = max(0.0, (eff_now - p_start).total_seconds())
        elapsed_hours = Decimal(str(elapsed_seconds / 3600.0))
        remaining_seconds = max(0.0, (p_end - eff_now).total_seconds())
        remaining_period_hours = Decimal(str(remaining_seconds / 3600.0))

        eff_elapsed = max(elapsed_hours, Decimal("0.01"))
        period_avg_rate = settled_period_spend / eff_elapsed
        recent_denom = min(Decimal(str(eff_window_hours)), eff_elapsed)
        recent_window_rate = window_spend / recent_denom
        hourly_burn_rate = _money(max(period_avg_rate, recent_window_rate))
        daily_burn_rate = _money(hourly_burn_rate * Decimal("24"))
        projected_period_cost = _money(settled_period_spend + hourly_burn_rate * remaining_period_hours)

        policy_curr = (policy.currency or base_currency).strip().upper()
        limit_fx_rate = (
            settings.finops_fx_rates.get(policy_curr, Decimal("1"))
            if policy_curr != base_currency
            else Decimal("1")
        )
        hard_limit = _money(_money(policy.hard_limit_amount) * limit_fx_rate)
        soft_limit = _money(_money(policy.soft_limit_amount) * limit_fx_rate)
        if hard_limit == Decimal("0") and soft_limit == Decimal("0") and policy.limit_amount:
            hard_limit = _money(_money(policy.limit_amount) * limit_fx_rate)

        insufficient_data = (elapsed_hours < min_elapsed) or (len(settled_matching) < min_samples)
        estimated_hours_to_hard_limit: Decimal | None = None
        estimated_exhaustion_at: datetime | None = None
        if hard_limit > Decimal("0") and hourly_burn_rate > Decimal("0"):
            remaining_hard_budget = max(Decimal("0"), hard_limit - settled_period_spend)
            estimated_hours_to_hard_limit = _money(remaining_hard_budget / hourly_burn_rate)
            exhaust_secs = min(float(estimated_hours_to_hard_limit * Decimal("3600")), 315360000.0)
            estimated_exhaustion_at = eff_now + timedelta(seconds=exhaust_secs)

        burn_status = "normal"
        if not insufficient_data:
            if hard_limit > Decimal("0") and (
                settled_period_spend > hard_limit
                or (estimated_exhaustion_at is not None and estimated_exhaustion_at <= p_end)
            ):
                burn_status = "critical_burn"
            elif (soft_limit > Decimal("0") and projected_period_cost > soft_limit) or (
                hard_limit > Decimal("0") and projected_period_cost >= hard_limit * Decimal("0.85")
            ):
                burn_status = "elevated_burn"

        if record_metrics and burn_status in {"elevated_burn", "critical_burn"}:
            FINOPS_BURN_RATE_ALERTS_TOTAL.labels(
                tenant_id=tenant_id,
                scope=policy.scope,
                burn_status=burn_status,
            ).inc()

        if status_priority[burn_status] > status_priority[overall_status]:
            overall_status = burn_status

        forecasts.append(
            PolicyBurnRateForecastRecord(
                budget_id=policy.budget_id,
                policy_name=policy.policy_name,
                scope=policy.scope,
                scope_id=policy.scope_id or "",
                period=policy.period,
                period_start=p_start,
                period_end=p_end,
                settled_period_spend=settled_period_spend,
                soft_limit_amount=soft_limit,
                hard_limit_amount=hard_limit,
                hourly_burn_rate=hourly_burn_rate,
                daily_burn_rate=daily_burn_rate,
                projected_period_cost=projected_period_cost,
                estimated_hours_to_hard_limit=estimated_hours_to_hard_limit,
                estimated_exhaustion_at=estimated_exhaustion_at,
                burn_status=burn_status,
                insufficient_data=insufficient_data,
            )
        )

    tenant_monthly = next(
        (f for f in forecasts if f.scope == "tenant" and f.period == "monthly"),
        None,
    )
    if tenant_monthly is not None:
        summary_hourly = tenant_monthly.hourly_burn_rate
        summary_daily = tenant_monthly.daily_burn_rate
        summary_projected_month = tenant_monthly.projected_period_cost
    else:
        m_start = datetime(eff_now.year, eff_now.month, 1, tzinfo=timezone.utc)
        m_end = (
            datetime(eff_now.year + 1, 1, 1, tzinfo=timezone.utc)
            if eff_now.month == 12
            else datetime(eff_now.year, eff_now.month + 1, 1, tzinfo=timezone.utc)
        )
        op_costs = [
            c
            for c in tenant_costs
            if c.cost_type != "adjustment"
            and m_start <= (_ensure_utc(c.created_at) or eff_now) <= eff_now
        ]
        settled_m = _settled_cost_events(op_costs)
        m_spend = _money(sum((_pre_tax_base_cost(c) for c in settled_m), Decimal("0")))
        w_spend = _money(
            sum(
                (
                    _pre_tax_base_cost(c)
                    for c in settled_m
                    if (_ensure_utc(c.created_at) or eff_now) >= window_start
                ),
                Decimal("0"),
            )
        )
        m_elapsed = max(Decimal(str(max(0.0, (eff_now - m_start).total_seconds()) / 3600.0)), Decimal("0.01"))
        m_rem = Decimal(str(max(0.0, (m_end - eff_now).total_seconds()) / 3600.0))
        m_avg = m_spend / m_elapsed
        m_rec = w_spend / min(Decimal(str(eff_window_hours)), m_elapsed)
        summary_hourly = _money(max(m_avg, m_rec))
        if summary_hourly == Decimal("0") and forecasts:
            summary_hourly = max((f.hourly_burn_rate for f in forecasts), default=Decimal("0"))
        summary_daily = _money(summary_hourly * Decimal("24"))
        summary_projected_month = _money(m_spend + summary_hourly * m_rem)

    return TenantBurnRateForecastResponse(
        tenant_id=tenant_id,
        project_id=project_id or "",
        base_currency=base_currency,
        window_hours=eff_window_hours,
        as_of=eff_now,
        hourly_burn_rate=summary_hourly,
        daily_burn_rate=summary_daily,
        projected_month_end_cost=summary_projected_month,
        burn_status=overall_status,
        burn_rate_forecasts=forecasts,
    )


async def compute_tenant_burn_rate_forecast(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None = None,
    window_hours: int | None = None,
    now: datetime | None = None,
) -> TenantBurnRateForecastResponse:
    policies = await list_budget_policies(session, tenant_id=tenant_id, active_only=True)
    tenant_costs = await _list_cost_events(session, tenant_id=tenant_id, project_id=project_id)
    return _compute_burn_rate_forecasts_from_data(
        policies,
        tenant_costs,
        tenant_id=tenant_id,
        project_id=project_id,
        window_hours=window_hours,
        now=now,
        record_metrics=True,
    )
