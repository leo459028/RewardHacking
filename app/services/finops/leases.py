"""Dynamic Token Broker run budget leases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.agent_run import AgentRun
from app.models.control_plane import ExecutionPlan
from app.models.finops import (
    CostEvent,
)
from app.models.governance_event import GovernanceEvent
from app.observability.metrics import (
    FINOPS_TOKEN_LEASES_TOTAL,
)
from app.schemas.finops import (
    TenantTokenLeasesResponse,
    TokenBudgetLeaseRecord,
)
from app.services.finops.core import (
    _decimal,
    _ensure_utc,
    _money,
    format_decimal_plain,
)
from app.services.finops.ledger import (
    _list_cost_events,
)
from app.services.finops.settlement import (
    _pre_tax_base_cost,
)
from app.services.run_state import RunStateStore


async def acquire_run_budget_lease(
    session: AsyncSession,
    state_store: RunStateStore | None = None,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None = None,
) -> TokenBudgetLeaseRecord:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    ttl_seconds = int(settings.finops_token_lease_ttl_seconds)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)

    costs = await _list_cost_events(session, run_id=run.run_id)
    est_costs = [c for c in costs if c.cost_type == "estimated"]
    lease_id = f"lease_{run.run_id}"
    estimated_base_cost = _money(sum((_pre_tax_base_cost(c) for c in est_costs), Decimal("0")))
    estimated_tokens = sum(
        int(_decimal(c.quantity))
        for c in est_costs
        if c.resource_type == "model"
    )
    model_name = next((c.model_name for c in est_costs if c.model_name), "")
    project_id = next((c.project_id for c in est_costs if c.project_id), "")

    for item in est_costs:
        meta = dict(item.metadata_payload or {})
        meta.pop("lease_released_reason", None)
        meta.pop("lease_released_at", None)
        meta.update(
            {
                "lease_id": lease_id,
                "lease_status": "active",
                "lease_created_at": now.isoformat(),
                "lease_expires_at": expires_at.isoformat(),
                "lease_ttl_seconds": ttl_seconds,
            }
        )
        item.metadata_payload = meta

    lease_dict = {
        "lease_id": lease_id,
        "run_id": run.run_id,
        "tenant_id": run.tenant_id,
        "user_id": run.user_id,
        "project_id": project_id,
        "model_name": model_name,
        "estimated_tokens": estimated_tokens,
        "estimated_base_cost": format_decimal_plain(estimated_base_cost),
        "currency": base_currency,
        "status": "active",
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "ttl_seconds": ttl_seconds,
    }
    if plan is not None:
        old_route = dict(plan.model_route or {})
        old_route["token_lease"] = {
            "lease_id": lease_id,
            "status": "active",
            "estimated_base_cost": format_decimal_plain(estimated_base_cost),
            "estimated_tokens": estimated_tokens,
            "expires_at": expires_at.isoformat(),
        }
        plan.model_route = old_route
        summary = dict(run.evaluation_summary or {})
        exec_plan_dict = dict(summary.get("execution_plan") or {})
        if exec_plan_dict:
            exec_plan_dict["model_route"] = plan.model_route
            summary["execution_plan"] = exec_plan_dict
            run.evaluation_summary = summary

    if state_store is not None:
        await state_store.acquire_token_lease(lease_dict, ttl_seconds=ttl_seconds)
    FINOPS_TOKEN_LEASES_TOTAL.labels(tenant_id=run.tenant_id, action="acquired").inc()
    await session.flush()

    return TokenBudgetLeaseRecord(
        lease_id=lease_id,
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        project_id=project_id,
        model_name=model_name,
        estimated_tokens=estimated_tokens,
        estimated_base_cost=estimated_base_cost,
        currency=base_currency,
        status="active",
        stale=False,
        created_at=now,
        expires_at=expires_at,
        ttl_seconds=ttl_seconds,
    )


async def settle_run_budget_lease(
    session: AsyncSession,
    state_store: RunStateStore | None = None,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    costs = await _list_cost_events(session, run_id=run.run_id)
    est_costs = [c for c in costs if c.cost_type == "estimated"]
    already_settled = all(
        str((c.metadata_payload or {}).get("lease_status") or "") == "settled"
        for c in est_costs
    ) if est_costs else True

    for item in est_costs:
        meta = dict(item.metadata_payload or {})
        meta["lease_id"] = str(meta.get("lease_id") or f"lease_{run.run_id}")
        meta["lease_status"] = "settled"
        meta["lease_settled_at"] = now.isoformat()
        item.metadata_payload = meta

    if plan is not None:
        old_route = dict(plan.model_route or {})
        lease_info = dict(old_route.get("token_lease") or {})
        if lease_info:
            lease_info["status"] = "settled"
            lease_info["settled_at"] = now.isoformat()
            old_route["token_lease"] = lease_info
            plan.model_route = old_route

    if state_store is not None:
        await state_store.release_token_lease(
            run.tenant_id,
            run.run_id,
            status="settled",
            reason="worker_completed",
        )
    if not already_settled:
        FINOPS_TOKEN_LEASES_TOTAL.labels(tenant_id=run.tenant_id, action="settled").inc()
    await session.flush()


async def release_run_budget_lease(
    session: AsyncSession,
    state_store: RunStateStore | None = None,
    *,
    run_id: str,
    tenant_id: str | None = None,
    reason: str = "released",
    actor: str = "system",
    record_governance_event: bool = False,
) -> TokenBudgetLeaseRecord | None:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    now = datetime.now(timezone.utc)
    costs = await _list_cost_events(session, run_id=run_id)
    est_costs = [c for c in costs if c.cost_type == "estimated"]
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    eff_tenant_id = tenant_id or (run.tenant_id if run is not None else (est_costs[0].tenant_id if est_costs else "default"))

    if not est_costs and run is None:
        return None

    already_released = all(
        str((c.metadata_payload or {}).get("lease_status") or "") == "released"
        for c in est_costs
    ) if est_costs else False

    lease_id = f"lease_{run_id}"
    estimated_base_cost = _money(sum((_pre_tax_base_cost(c) for c in est_costs), Decimal("0")))
    estimated_tokens = sum(
        int(_decimal(c.quantity))
        for c in est_costs
        if c.resource_type == "model"
    )
    model_name = next((c.model_name for c in est_costs if c.model_name), "")
    project_id = next((c.project_id for c in est_costs if c.project_id), "")
    created_at_dt = _ensure_utc((est_costs[0].metadata_payload or {}).get("lease_created_at")) if est_costs else now
    expires_at_dt = _ensure_utc((est_costs[0].metadata_payload or {}).get("lease_expires_at")) if est_costs else now
    ttl_seconds = int((est_costs[0].metadata_payload or {}).get("lease_ttl_seconds") or settings.finops_token_lease_ttl_seconds) if est_costs else int(settings.finops_token_lease_ttl_seconds)

    for item in est_costs:
        meta = dict(item.metadata_payload or {})
        lease_id = str(meta.get("lease_id") or lease_id)
        meta["lease_id"] = lease_id
        meta["lease_status"] = "released"
        meta["lease_released_reason"] = reason
        meta["lease_released_at"] = now.isoformat()
        item.metadata_payload = meta

    plan = await session.scalar(select(ExecutionPlan).where(ExecutionPlan.run_id == run_id))
    if plan is not None:
        old_route = dict(plan.model_route or {})
        lease_info = dict(old_route.get("token_lease") or {"lease_id": lease_id})
        lease_info["status"] = "released"
        lease_info["released_reason"] = reason
        lease_info["released_at"] = now.isoformat()
        old_route["token_lease"] = lease_info
        plan.model_route = old_route
        if run is not None:
            summary = dict(run.evaluation_summary or {})
            exec_plan_dict = dict(summary.get("execution_plan") or {})
            if exec_plan_dict:
                exec_plan_dict["model_route"] = plan.model_route
                summary["execution_plan"] = exec_plan_dict
                run.evaluation_summary = summary

    if state_store is not None:
        await state_store.release_token_lease(eff_tenant_id, run_id, status="released", reason=reason)
    if est_costs and not already_released:
        FINOPS_TOKEN_LEASES_TOTAL.labels(tenant_id=eff_tenant_id, action="released").inc()
    if record_governance_event:
        session.add(
            GovernanceEvent(
                tenant_id=eff_tenant_id,
                user_id=actor,
                run_id=run_id,
                event_type="finops_token_lease_released",
                decision="released",
                reason=reason,
                details={
                    "lease_id": lease_id,
                    "estimated_base_cost": format_decimal_plain(estimated_base_cost),
                    "estimated_tokens": estimated_tokens,
                },
            )
        )
    await session.flush()

    return TokenBudgetLeaseRecord(
        lease_id=lease_id,
        run_id=run_id,
        tenant_id=eff_tenant_id,
        user_id=run.user_id if run is not None else (est_costs[0].user_id if est_costs else ""),
        project_id=project_id,
        model_name=model_name,
        estimated_tokens=estimated_tokens,
        estimated_base_cost=estimated_base_cost,
        currency=base_currency,
        status="released",
        stale=False,
        created_at=created_at_dt or now,
        expires_at=expires_at_dt or now,
        ttl_seconds=ttl_seconds,
        released_reason=reason,
    )


def _compute_active_leases_from_costs(
    costs: list[CostEvent],
    *,
    tenant_id: str,
    now: datetime | None = None,
) -> TenantTokenLeasesResponse:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    eff_now = _ensure_utc(now) or datetime.now(timezone.utc)
    default_ttl = int(settings.finops_token_lease_ttl_seconds)

    by_run: dict[str, list[CostEvent]] = {}
    for item in costs:
        if not item.run_id or item.cost_type == "adjustment":
            continue
        by_run.setdefault(item.run_id, []).append(item)

    active_leases: list[TokenBudgetLeaseRecord] = []
    for run_id, run_costs in by_run.items():
        if any(c.cost_type == "actual" for c in run_costs):
            continue
        est_rows = [c for c in run_costs if c.cost_type == "estimated"]
        if not est_rows:
            continue
        first_meta = dict(est_rows[0].metadata_payload or {})
        lease_status = str(first_meta.get("lease_status") or "")
        if lease_status != "active":
            continue
        lease_id = str(first_meta.get("lease_id") or f"lease_{run_id}")
        est_cost = _money(sum((_pre_tax_base_cost(c) for c in est_rows), Decimal("0")))
        est_tokens = sum(int(_decimal(c.quantity)) for c in est_rows if c.resource_type == "model")
        model_name = next((c.model_name for c in est_rows if c.model_name), "")
        project_id = next((c.project_id for c in est_rows if c.project_id), "")
        user_id = next((c.user_id for c in est_rows if c.user_id), "")
        created_dt = _ensure_utc(first_meta.get("lease_created_at")) or _ensure_utc(est_rows[0].created_at) or eff_now
        ttl_sec = int(first_meta.get("lease_ttl_seconds") or default_ttl)
        expires_dt = _ensure_utc(first_meta.get("lease_expires_at")) or (created_dt + timedelta(seconds=ttl_sec))
        is_stale = expires_dt <= eff_now
        active_leases.append(
            TokenBudgetLeaseRecord(
                lease_id=lease_id,
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                model_name=model_name,
                estimated_tokens=est_tokens,
                estimated_base_cost=est_cost,
                currency=base_currency,
                status="active",
                stale=is_stale,
                created_at=created_dt,
                expires_at=expires_dt,
                ttl_seconds=ttl_sec,
            )
        )

    active_leases.sort(key=lambda r: r.created_at or eff_now, reverse=True)
    total_tokens = sum(r.estimated_tokens for r in active_leases)
    total_cost = _money(sum((r.estimated_base_cost for r in active_leases), Decimal("0")))
    return TenantTokenLeasesResponse(
        tenant_id=tenant_id,
        base_currency=base_currency,
        active_lease_count=len(active_leases),
        active_leased_tokens=total_tokens,
        active_leased_base_cost=total_cost,
        active_leases=active_leases,
    )


async def list_tenant_token_leases(
    session: AsyncSession,
    state_store: RunStateStore | None = None,
    *,
    tenant_id: str,
    now: datetime | None = None,
) -> TenantTokenLeasesResponse:
    costs = await _list_cost_events(session, tenant_id=tenant_id)
    return _compute_active_leases_from_costs(costs, tenant_id=tenant_id, now=now)
