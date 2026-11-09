"""Manual cost adjustments."""

from __future__ import annotations

import hashlib
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.finops import (
    CostEvent,
)
from app.models.governance_event import GovernanceEvent
from app.schemas.finops import (
    CostAdjustmentCreateRequest,
)
from app.services.finops.core import (
    FINOPS_CALCULATION_VERSION,
    _decimal,
    _decimal_json,
    _ensure_utc,
    _money,
    format_decimal_plain,
)
from app.services.finops.ledger import (
    resolve_fx_rate,
)
from app.services.finops.settlement import (
    _pre_tax_base_cost,
)


async def create_cost_adjustment(
    session: AsyncSession,
    payload: CostAdjustmentCreateRequest,
    *,
    actor: str = "finops_admin",
) -> CostEvent:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    tenant_id = (payload.tenant_id or "default").strip() or "default"
    currency = (payload.currency or base_currency).strip().upper()
    amount = _money(payload.amount)

    idem_source = (
        payload.idempotency_key.strip()
        if payload.idempotency_key and payload.idempotency_key.strip()
        else f"{tenant_id}|{payload.provider_invoice_id}|{_decimal_json(amount)}|{currency}|{payload.reason}"
    )
    cost_id = f"cost_adj_{hashlib.sha256(idem_source.encode('utf-8')).hexdigest()[:32]}"
    existing = await session.scalar(select(CostEvent).where(CostEvent.cost_id == cost_id))
    if existing is not None:
        existing.is_settled = True
        existing.pre_tax_base_cost = _pre_tax_base_cost(existing)
        return existing

    if payload.reverses_cost_id:
        reversed_target = await session.scalar(
            select(CostEvent).where(
                CostEvent.cost_id == payload.reverses_cost_id,
                CostEvent.tenant_id == tenant_id,
            )
        )
        if reversed_target is None:
            raise ValueError(f"reverses_cost_id {payload.reverses_cost_id!r} not found for tenant {tenant_id!r}.")

    if payload.fx_rate is not None and _decimal(payload.fx_rate) > Decimal("0"):
        fx_rate = _decimal(payload.fx_rate)
        fx_rate_source = "adjustment_override"
    else:
        fx_rate, fx_rate_source = resolve_fx_rate(currency, base_currency, None)

    tax_rate = _decimal(payload.tax_rate) if payload.tax_rate is not None else settings.finops_default_tax_rate
    tax_amount = _money(amount * tax_rate) if tax_rate > Decimal("0") else Decimal("0")
    billed_amount = _money(amount + tax_amount)
    base_currency_cost = _money(billed_amount * fx_rate)
    pre_tax_base_cost = _money(amount * fx_rate)
    created_by = payload.created_by or actor

    cost = CostEvent(
        cost_id=cost_id,
        usage_event_id="",
        pricing_catalog_id="",
        pricing_snapshot={},
        tenant_id=tenant_id,
        org_id=payload.org_id or "",
        project_id=payload.project_id or "",
        cost_center=payload.cost_center or "",
        environment=payload.environment or "prod",
        user_id=created_by,
        run_id=payload.run_id or "",
        workflow_id="",
        step_id="",
        checkpoint_id="",
        replay_attempt_id="",
        resume_attempt_id="",
        trace_id="",
        provider=payload.provider or "internal",
        billing_platform="direct_api",
        resource_type="adjustment",
        resource_subtype="other",
        model_name=payload.model_name or "",
        model_version="",
        cost_type="adjustment",
        usage_unit="adjustment",
        quantity=Decimal("0"),
        unit_price=amount,
        gross_cost=amount,
        discount_rate=Decimal("0"),
        discount_amount=Decimal("0"),
        multiplier=Decimal("1"),
        net_cost=amount,
        tax_amount=tax_amount,
        estimated_cost=Decimal("0"),
        actual_cost=amount,
        currency=currency,
        base_currency=base_currency,
        base_currency_cost=base_currency_cost,
        fx_rate=fx_rate,
        fx_rate_source=fx_rate_source,
        billing_period_start=_ensure_utc(payload.billing_period_start),
        billing_period_end=_ensure_utc(payload.billing_period_end),
        provider_invoice_id=payload.provider_invoice_id,
        calculation_method="manual_adjustment",
        calculation_version=FINOPS_CALCULATION_VERSION,
        allocation_tags={
            "tenant_id": tenant_id,
            "workflow_id": "",
            "project_id": payload.project_id or "",
            "cost_center": payload.cost_center or "",
            "org_id": payload.org_id or "",
            "environment": payload.environment or "prod",
            "provider_invoice_id": payload.provider_invoice_id,
            "custom": {},
        },
        metadata_payload={
            "reason": payload.reason,
            "created_by": created_by,
            "reverses_cost_id": payload.reverses_cost_id or "",
            "pre_tax_amount": _decimal_json(amount),
            "billed_amount": _decimal_json(billed_amount),
            "tax_rate": _decimal_json(tax_rate),
            "fx_rates_as_of": settings.finops_fx_rates_as_of,
            **(payload.metadata or {}),
        },
    )
    session.add(cost)
    session.add(
        GovernanceEvent(
            tenant_id=tenant_id,
            user_id=created_by,
            run_id=payload.run_id or "",
            event_type="finops_cost_adjustment",
            decision="recorded",
            reason=payload.reason,
            details={
                "cost_id": cost_id,
                "provider_invoice_id": payload.provider_invoice_id,
                "amount": format_decimal_plain(amount),
                "currency": currency,
                "base_currency_cost": format_decimal_plain(base_currency_cost),
                "reverses_cost_id": payload.reverses_cost_id or "",
            },
        )
    )
    await session.commit()
    await session.refresh(cost)
    cost.is_settled = True
    cost.pre_tax_base_cost = pre_tax_base_cost
    return cost
