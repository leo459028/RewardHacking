"""Settled-ledger and cost-item helpers (pure functions over CostEvent rows)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.models.finops import (
    BudgetPolicy,
    CostEvent,
)
from app.services.finops.core import (
    _decimal,
    _ensure_utc,
    _money,
)


def _fx_for_item(item: CostEvent) -> Decimal:
    fx = _decimal(item.fx_rate)
    return fx if fx > Decimal("0") else Decimal("1")


def _pre_tax_base_cost(item: CostEvent) -> Decimal:
    return _money(_total_cost(item) * _fx_for_item(item))


def _tax_base_cost(item: CostEvent) -> Decimal:
    return _money(_decimal(item.tax_amount) * _fx_for_item(item))


def _billed_base_cost(item: CostEvent) -> Decimal:
    bcc = _decimal(item.base_currency_cost)
    if bcc != Decimal("0"):
        return _money(bcc)
    return _money((_total_cost(item) + _decimal(item.tax_amount)) * _fx_for_item(item))


def _total_base_cost(items: list[CostEvent]) -> Decimal:
    return _money(sum((_pre_tax_base_cost(item) for item in items), Decimal("0")))


def _settlement_key(item: CostEvent) -> tuple[str, str, str, str, str]:
    return (
        item.run_id or "",
        item.replay_attempt_id or "",
        item.resume_attempt_id or "",
        item.resource_type or "",
        item.usage_unit or "",
    )


def _settled_cost_events(costs: list[CostEvent]) -> list[CostEvent]:
    has_actual_by_key: dict[tuple[str, str, str, str, str], bool] = {}
    for item in costs:
        if not item.run_id or item.cost_type == "adjustment":
            continue
        key = _settlement_key(item)
        if item.cost_type in {"actual", "failed_retry"}:
            has_actual_by_key[key] = True
        else:
            has_actual_by_key.setdefault(key, False)

    settled: list[CostEvent] = []
    for item in costs:
        if not item.run_id or item.cost_type == "adjustment":
            settled.append(item)
            continue
        if item.cost_type == "estimated" and str((item.metadata_payload or {}).get("lease_status") or "") == "released":
            continue
        key = _settlement_key(item)
        if has_actual_by_key.get(key, False):
            if item.cost_type in {"actual", "failed_retry"}:
                settled.append(item)
        else:
            settled.append(item)
    return settled


def _cost_for_basis(item: CostEvent, basis: str) -> Decimal:
    fx = _fx_for_item(item)
    if basis == "gross":
        return _money(_decimal(item.gross_cost) * fx)
    if basis == "net":
        return _money(_decimal(item.net_cost) * fx)
    if basis == "base_currency":
        return _billed_base_cost(item)
    if basis == "estimated":
        return _money(_decimal(item.estimated_cost) * fx)
    return _pre_tax_base_cost(item)


def _cost_in_period(item: CostEvent, policy: BudgetPolicy, now: datetime, run_id: str) -> bool:
    if policy.scope == "run" or policy.period == "per_run":
        return item.run_id == run_id
    dt = _ensure_utc(item.created_at) or now
    p_start = _ensure_utc(policy.period_start)
    p_end = _ensure_utc(policy.period_end)
    if p_start is not None and dt < p_start:
        return False
    if p_end is not None and dt > p_end:
        return False
    if policy.period == "daily":
        return dt.date() == now.date()
    if policy.period == "weekly":
        return dt >= now - timedelta(days=7)
    if policy.period == "monthly":
        return dt.year == now.year and dt.month == now.month
    if policy.period == "quarterly":
        return dt.year == now.year and ((dt.month - 1) // 3) == ((now.month - 1) // 3)
    if policy.period == "yearly":
        return dt.year == now.year
    return True


def _total_cost(item: CostEvent) -> Decimal:
    return _decimal(item.estimated_cost) + _decimal(item.actual_cost)


def _cost_tools(item: CostEvent) -> list[str]:
    meta = dict(item.metadata_payload or {})
    tools = [str(t) for t in (meta.get("tools") or []) if str(t)]
    if not tools:
        tool_id = str(meta.get("tool_id") or (item.allocation_tags or {}).get("tool_id") or "")
        if tool_id:
            tools = [tool_id]
    return tools


def _cost_mcp_servers(item: CostEvent) -> list[str]:
    meta = dict(item.metadata_payload or {})
    servers = [str(t) for t in (meta.get("mcp_servers") or []) if str(t)]
    if not servers:
        server = str(meta.get("mcp_server_id") or (item.allocation_tags or {}).get("mcp_server_id") or "")
        if server:
            servers = [server]
    return servers


def _cost_matches_mcp_server(item: CostEvent, mcp_server_id: str | None) -> bool:
    if not mcp_server_id:
        return True
    return mcp_server_id in _cost_mcp_servers(item)
