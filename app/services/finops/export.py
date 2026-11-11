"""Tenant cost ledger CSV / JSON export."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.finops import (
    CostEvent,
)
from app.observability.metrics import (
    FINOPS_LEDGER_EXPORTS_TOTAL,
)
from app.schemas.finops import (
    CostEventRecord,
    TenantCostExportResponse,
)
from app.services.finops.core import (
    _ensure_utc,
    _money,
    format_decimal_plain,
)
from app.services.finops.ledger import (
    _list_cost_events,
)
from app.services.finops.reporting import (
    _breakdown,
    _mcp_server_cost_distribution,
)
from app.services.finops.serializers import (
    cost_to_dict,
)
from app.services.finops.settlement import (
    _billed_base_cost,
    _cost_matches_mcp_server,
    _pre_tax_base_cost,
    _settled_cost_events,
    _tax_base_cost,
    _total_base_cost,
    _total_cost,
)

CSV_EXPORT_COLUMNS: tuple[str, ...] = (
    "cost_id",
    "usage_event_id",
    "created_at",
    "tenant_id",
    "org_id",
    "project_id",
    "cost_center",
    "environment",
    "user_id",
    "run_id",
    "workflow_id",
    "step_id",
    "trace_id",
    "provider",
    "model_name",
    "resource_type",
    "cost_type",
    "is_settled",
    "quantity",
    "usage_unit",
    "unit_price",
    "gross_cost",
    "discount_amount",
    "net_cost",
    "tax_amount",
    "currency",
    "fx_rate",
    "fx_rate_source",
    "base_currency",
    "pre_tax_base_cost",
    "base_currency_cost",
    "provider_invoice_id",
    "billing_period_start",
    "billing_period_end",
    "calculation_version",
    "mcp_server_id",
    "tool_id",
)


_CSV_NUMERIC_COLUMNS = {
    "quantity",
    "unit_price",
    "gross_cost",
    "discount_amount",
    "net_cost",
    "tax_amount",
    "fx_rate",
    "pre_tax_base_cost",
    "base_currency_cost",
}


def _escape_csv_cell(value: object, *, is_numeric: bool = False) -> str:
    if value is None:
        return ""
    if is_numeric:
        return format_decimal_plain(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    if text and text[0] in {"=", "+", "-", "@", "\t", "\r"}:
        return f"'{text}"
    return text


def _parse_export_bound(value: str | date | datetime | None, *, is_end: bool = False) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        if is_end:
            return datetime(value.year, value.month, value.day, 23, 59, 59, 999999, tzinfo=timezone.utc)
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if len(text) == 10:
        try:
            d = date.fromisoformat(text)
            if is_end:
                return datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=timezone.utc)
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            pass
    return _ensure_utc(text)


async def export_tenant_cost_ledger(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None = None,
    cost_center: str | None = None,
    cost_type: str | None = None,
    provider: str | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    settled_only: bool = False,
    limit: int | None = None,
    export_format: str = "json",
    mcp_server_id: str | None = None,
) -> TenantCostExportResponse:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    all_tenant_costs = await _list_cost_events(session, tenant_id=tenant_id)
    settled_operational_all = _settled_cost_events([c for c in all_tenant_costs if c.cost_type != "adjustment"])
    settled_ids = {c.cost_id for c in settled_operational_all}

    start_dt = _parse_export_bound(start_date, is_end=False)
    end_dt = _parse_export_bound(end_date, is_end=True)

    filtered: list[CostEvent] = []
    for item in all_tenant_costs:
        item.is_settled = (item.cost_id in settled_ids) or (item.cost_type == "adjustment")
        item.pre_tax_base_cost = _pre_tax_base_cost(item)
        if project_id and item.project_id != project_id:
            continue
        if cost_center and item.cost_center != cost_center:
            continue
        if cost_type and item.cost_type != cost_type:
            continue
        if provider and item.provider != provider:
            continue
        if mcp_server_id and not _cost_matches_mcp_server(item, mcp_server_id):
            continue
        created = _ensure_utc(item.created_at)
        if start_dt is not None and created is not None and created < start_dt:
            continue
        if end_dt is not None and created is not None and created > end_dt:
            continue
        if settled_only and not item.is_settled:
            continue
        filtered.append(item)

    max_rows = settings.finops_export_max_rows
    if limit is not None and limit > 0:
        max_rows = min(limit, settings.finops_export_max_rows)
    truncated = len(filtered) > max_rows
    export_rows = filtered[:max_rows]

    FINOPS_LEDGER_EXPORTS_TOTAL.labels(
        tenant_id=tenant_id or "default",
        format=(export_format or "json").lower(),
    ).inc()

    exp_settled_op = [r for r in export_rows if r.cost_type != "adjustment" and getattr(r, "is_settled", True)]
    exp_adj = [r for r in export_rows if r.cost_type == "adjustment"]
    settled_pre_tax_base_cost = _total_base_cost(exp_settled_op)
    total_tax_amount = _money(sum((_tax_base_cost(r) for r in exp_settled_op), Decimal("0")))
    total_billed_base_cost = _money(sum((_billed_base_cost(r) for r in exp_settled_op), Decimal("0")))
    adjustment_total = _total_base_cost(exp_adj)

    currency_breakdown: dict[str, Decimal] = {}
    for item in exp_settled_op:
        curr = (item.currency or base_currency).upper()
        currency_breakdown[curr] = _money(currency_breakdown.get(curr, Decimal("0")) + _total_cost(item))

    cost_center_breakdown = _breakdown(exp_settled_op, "cost_center")

    return TenantCostExportResponse(
        tenant_id=tenant_id,
        total_records=len(export_rows),
        truncated=truncated,
        base_currency=base_currency,
        fx_rates_as_of=settings.finops_fx_rates_as_of,
        settled_pre_tax_base_cost=settled_pre_tax_base_cost,
        total_tax_amount=total_tax_amount,
        total_billed_base_cost=total_billed_base_cost,
        adjustment_total=adjustment_total,
        currency_breakdown=currency_breakdown,
        cost_center_breakdown=cost_center_breakdown,
        mcp_server_breakdown=_mcp_server_cost_distribution(exp_settled_op),
        items=[CostEventRecord(**cost_to_dict(r)) for r in export_rows],
    )


def render_cost_ledger_csv(export_data: TenantCostExportResponse) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(list(CSV_EXPORT_COLUMNS))
    for item in export_data.items:
        item_dict = item.model_dump()
        row = [
            _escape_csv_cell(item_dict.get(col), is_numeric=(col in _CSV_NUMERIC_COLUMNS))
            for col in CSV_EXPORT_COLUMNS
        ]
        writer.writerow(row)
    return buf.getvalue()
