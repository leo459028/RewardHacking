"""Usage and cost ledger recording, FX and tax."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.finops import (
    BudgetViolation,
    CostEvent,
    PricingCatalog,
    UsageEvent,
)
from app.observability.metrics import (
    FINOPS_BASE_CURRENCY_COST_TOTAL,
    FINOPS_BUDGET_VIOLATIONS_TOTAL,
    FINOPS_COST_USD_TOTAL,
    FINOPS_TAX_TOTAL,
    FINOPS_USAGE_EVENTS_TOTAL,
)
from app.schemas.finops import (
    UsageEventCreateRequest,
)
from app.services.finops.core import (
    _ALLOCATION_TAG_KEY_RE,
    _ATTRIBUTION_ID_RE,
    _VALID_ENVIRONMENTS,
    FINOPS_CALCULATION_VERSION,
    _decimal_json,
    _ensure_utc,
    _money,
    _quantity,
    _value,
)
from app.services.finops.pricing import (
    _lookup_pricing,
    _token_type_for_usage,
)


async def record_usage_event(session: AsyncSession, payload: UsageEventCreateRequest) -> UsageEvent:
    usage = _usage_from_payload(payload)
    session.add(usage)
    await session.flush()
    await _record_cost_for_usage(session, usage)
    await session.commit()
    await session.refresh(usage)
    return usage


def _usage_from_payload(payload: UsageEventCreateRequest) -> UsageEvent:
    return UsageEvent(
        usage_id=payload.usage_id or f"usage_{uuid4().hex}",
        tenant_id=payload.tenant_id or "default",
        org_id=payload.org_id,
        project_id=payload.project_id,
        environment=payload.environment,
        cost_center=payload.cost_center,
        user_id=payload.user_id,
        workflow_id=payload.workflow_id,
        workflow_version=payload.workflow_version,
        run_id=payload.run_id,
        step_id=payload.step_id,
        attempt_id=payload.attempt_id,
        trace_id=payload.trace_id,
        span_id=payload.span_id,
        checkpoint_id=payload.checkpoint_id,
        replay_attempt_id=payload.replay_attempt_id,
        resume_attempt_id=payload.resume_attempt_id,
        provider=str(_value(payload.provider)) or "internal",
        billing_platform=str(_value(payload.billing_platform)) or "direct_api",
        provider_request_id=payload.provider_request_id,
        provider_response_id=payload.provider_response_id,
        model_name=payload.model_name,
        model_version=payload.model_version,
        pricing_id=payload.pricing_id,
        resource_type=str(_value(payload.resource_type)),
        resource_subtype=str(_value(payload.resource_subtype)),
        usage_unit=payload.usage_unit,
        quantity=_quantity(payload.quantity),
        input_tokens=payload.input_tokens,
        output_tokens=payload.output_tokens,
        cached_input_tokens=payload.cached_input_tokens,
        cache_write_tokens=payload.cache_write_tokens,
        reasoning_tokens=payload.reasoning_tokens,
        audio_input_tokens=payload.audio_input_tokens,
        audio_output_tokens=payload.audio_output_tokens,
        image_input_count=payload.image_input_count,
        image_output_count=payload.image_output_count,
        video_input_seconds=_quantity(payload.video_input_seconds),
        tool_id=payload.tool_id,
        tool_name=payload.tool_name,
        tool_provider=payload.tool_provider,
        tool_call_count=payload.tool_call_count,
        mcp_server_id=payload.mcp_server_id,
        tool_risk_level=payload.tool_risk_level,
        dry_run=payload.dry_run,
        external_side_effect=payload.external_side_effect,
        container_seconds=_quantity(payload.container_seconds),
        worker_seconds=_quantity(payload.worker_seconds),
        storage_gb_seconds=_quantity(payload.storage_gb_seconds),
        network_egress_gb=_quantity(payload.network_egress_gb),
        phase=payload.phase,
        status=str(_value(payload.status)),
        error_code=payload.error_code,
        error_message_hash=payload.error_message_hash,
        latency_ms=payload.latency_ms,
        queue_wait_ms=payload.queue_wait_ms,
        retry_count=payload.retry_count,
        idempotency_key=payload.idempotency_key,
        metadata_payload=payload.metadata,
    )


async def _prior_used_quantity_for_free_quota(
    session: AsyncSession,
    usage: UsageEvent,
    pricing: PricingCatalog,
) -> Decimal:
    stmt = select(CostEvent).where(
        CostEvent.tenant_id == usage.tenant_id,
        CostEvent.pricing_catalog_id == pricing.pricing_id,
        CostEvent.usage_event_id != usage.usage_id,
    )
    prior_costs = list((await session.scalars(stmt)).all())
    now = _ensure_utc(usage.created_at) or datetime.now(timezone.utc)
    period = (pricing.free_quota_period or "").lower()
    total = Decimal("0")
    for item in prior_costs:
        dt = _ensure_utc(item.created_at) or now
        if period == "daily" and dt.date() != now.date():
            continue
        if period == "monthly" and (dt.year != now.year or dt.month != now.month):
            continue
        if period == "yearly" and dt.year != now.year:
            continue
        raw_q = (item.metadata_payload or {}).get("raw_quantity")
        if raw_q is not None:
            total += _quantity(raw_q)
        else:
            total += _quantity(item.quantity)
    return _quantity(total)


def extract_allocation(meta: dict | None) -> tuple[dict[str, object], list[str]]:
    raw = dict(meta or {})
    warnings: list[str] = []

    cost_center = ""
    if "cost_center" in raw and raw["cost_center"] not in (None, ""):
        cc_val = str(raw["cost_center"]).strip()
        if _ATTRIBUTION_ID_RE.fullmatch(cc_val):
            cost_center = cc_val
        else:
            warnings.append("Invalid cost_center format; ignored.")

    org_id = ""
    if "org_id" in raw and raw["org_id"] not in (None, ""):
        org_val = str(raw["org_id"]).strip()
        if _ATTRIBUTION_ID_RE.fullmatch(org_val):
            org_id = org_val
        else:
            warnings.append("Invalid org_id format; ignored.")

    explicit_env = ""
    env = "dev"
    if "environment" in raw and raw["environment"] not in (None, ""):
        env_val = str(raw["environment"]).strip().lower()
        if env_val in _VALID_ENVIRONMENTS:
            env = env_val
            explicit_env = env_val
        else:
            warnings.append(f"Unsupported environment {raw['environment']!r}; defaulted to 'dev'.")

    custom_tags: dict[str, str] = {}
    if "allocation_tags" in raw and raw["allocation_tags"] is not None:
        raw_tags = raw["allocation_tags"]
        if not isinstance(raw_tags, dict):
            warnings.append("allocation_tags must be an object; ignored.")
        else:
            items = list(raw_tags.items())
            if len(items) > 20:
                warnings.append(f"allocation_tags has {len(items)} keys (max 20); truncated to 20.")
                items = items[:20]
            for k, v in items:
                key_str = str(k)
                if not _ALLOCATION_TAG_KEY_RE.fullmatch(key_str):
                    warnings.append(f"Invalid allocation_tag key {key_str!r}; ignored.")
                    continue
                if not isinstance(v, str) or len(v) > 128:
                    warnings.append(f"Invalid allocation_tag value for {key_str!r}; must be str <= 128 chars.")
                    continue
                custom_tags[key_str] = v

    allocation: dict[str, object] = {
        "cost_center": cost_center,
        "org_id": org_id,
        "environment": env,
        "explicit_environment": explicit_env,
        "allocation_tags": custom_tags,
    }
    return allocation, warnings


def resolve_fx_rate(
    currency: str,
    base_currency: str,
    pricing: PricingCatalog | None = None,
) -> tuple[Decimal, str]:
    settings = get_settings()
    norm_base = (base_currency or settings.finops_base_currency or "USD").strip().upper()
    norm_curr = (currency or norm_base).strip().upper()
    if norm_curr == norm_base:
        return Decimal("1"), "same_currency"
    if pricing is not None:
        p_meta = dict(pricing.metadata_payload or {})
        raw_override = p_meta.get("fx_rate")
        if raw_override not in (None, ""):
            try:
                override_rate = Decimal(str(raw_override))
                if override_rate > Decimal("0"):
                    return override_rate, "pricing_override"
            except Exception:
                pass
    configured = settings.finops_fx_rates.get(norm_curr)
    if configured is not None and configured > Decimal("0"):
        return Decimal(str(configured)), "configured_fx_table"
    return Decimal("1"), "missing_fx_rate"


def _compute_tax(
    net_raw: Decimal,
    *,
    tax_included: bool,
    tax_rate: Decimal,
) -> tuple[Decimal, Decimal]:
    if tax_rate <= Decimal("0") or net_raw == Decimal("0"):
        return _money(net_raw), Decimal("0")
    if tax_included:
        pre_tax = _money(net_raw / (Decimal("1") + tax_rate))
        tax_amount = _money(net_raw - pre_tax)
        return pre_tax, tax_amount
    pre_tax = _money(net_raw)
    tax_amount = _money(pre_tax * tax_rate)
    return pre_tax, tax_amount


async def _record_cost_for_usage(session: AsyncSession, usage: UsageEvent) -> CostEvent:
    settings = get_settings()
    base_currency = settings.finops_base_currency
    pricing = await _lookup_pricing(session, usage)
    free_quota_applied = Decimal("0")
    raw_quantity = _quantity(usage.quantity)
    tax_included = False
    tax_rate = settings.finops_default_tax_rate
    if pricing is None:
        await _record_missing_pricing_violation(session, usage)
        pricing_id = ""
        currency = base_currency
        unit_price = Decimal("0")
        quantity = raw_quantity
        gross_cost = Decimal("0")
        discount_rate = Decimal("0")
        discount_amount = Decimal("0")
        multiplier = Decimal("1")
        net_raw = Decimal("0")
        snapshot: dict[str, object] = {}
    else:
        pricing_id = pricing.pricing_id
        if not usage.pricing_id:
            usage.pricing_id = pricing_id
        currency = (pricing.currency or base_currency).strip().upper()
        tax_included = bool(pricing.tax_included)
        p_meta = dict(pricing.metadata_payload or {})
        if p_meta.get("tax_rate") not in (None, ""):
            try:
                parsed_tr = Decimal(str(p_meta["tax_rate"]))
                if parsed_tr >= Decimal("0"):
                    tax_rate = parsed_tr
            except Exception:
                pass
        unit_price = _money(pricing.unit_price)
        raw_quantity = _billable_quantity(usage, pricing)
        quantity = raw_quantity
        free_quota_limit = _quantity(pricing.free_quota_quantity or 0)
        if free_quota_limit > Decimal("0"):
            prior_used = await _prior_used_quantity_for_free_quota(session, usage, pricing)
            remaining_free = max(Decimal("0"), _quantity(free_quota_limit - prior_used))
            free_quota_applied = min(raw_quantity, remaining_free)
            quantity = max(Decimal("0"), _quantity(raw_quantity - free_quota_applied))
        min_billable = _quantity(pricing.minimum_billable_quantity or 0)
        if quantity > Decimal("0") and min_billable > Decimal("0") and quantity < min_billable:
            quantity = min_billable
        gross_cost = _money(quantity * unit_price)
        discount_rate = _money(pricing.discount_rate)
        discount_amount = _money(gross_cost * discount_rate)
        multiplier = _money(pricing.multiplier or 1)
        net_raw = _money((gross_cost - discount_amount) * multiplier)
        snapshot = _pricing_snapshot(pricing)

    pre_tax_amount, tax_amount = _compute_tax(net_raw, tax_included=tax_included, tax_rate=tax_rate)
    net_cost = pre_tax_amount
    amount = pre_tax_amount
    billed_amount = _money(pre_tax_amount + tax_amount)

    fx_rate, fx_rate_source = resolve_fx_rate(currency, base_currency, pricing)
    if fx_rate_source == "missing_fx_rate":
        await _record_missing_fx_violation(session, usage, currency=currency, base_currency=base_currency)

    pre_tax_base_cost = _money(pre_tax_amount * fx_rate)
    tax_base_cost = _money(tax_amount * fx_rate)
    base_currency_cost = _money(billed_amount * fx_rate)

    usage_meta = dict(usage.metadata_payload or {})
    ignored_untrusted = [k for k in ("fx_rate", "tax_rate") if k in usage_meta]
    alloc_parsed, alloc_warnings = extract_allocation(
        {"allocation_tags": usage_meta.get("allocation_tags")} if "allocation_tags" in usage_meta else {}
    )
    custom_alloc_tags = dict(alloc_parsed.get("allocation_tags") or {})

    is_actual = usage.phase in {"actual", "manual"}
    is_failed_retry = (
        usage_meta.get("cost_type") == "failed_retry"
        or (is_actual and usage.status == "failed")
    )
    cost_type = "failed_retry" if is_failed_retry else ("actual" if is_actual else "estimated")

    cost_meta: dict[str, object] = {
        "usage_unit": usage.usage_unit,
        "usage_status": usage.status,
        "raw_quantity": _decimal_json(raw_quantity),
        "billable_quantity": _decimal_json(quantity),
        "free_quota_applied_quantity": _decimal_json(free_quota_applied),
        "pricing_mode": str(snapshot.get("pricing_mode") or usage_meta.get("pricing_mode") or "standard"),
        "token_type": str(snapshot.get("token_type") or _token_type_for_usage(usage)),
        "pre_tax_amount": _decimal_json(pre_tax_amount),
        "billed_amount": _decimal_json(billed_amount),
        "tax_rate": _decimal_json(tax_rate),
        "tax_included": tax_included,
        "fx_rates_as_of": settings.finops_fx_rates_as_of,
    }
    if usage_meta.get("explicit_environment"):
        cost_meta["explicit_environment"] = usage_meta["explicit_environment"]
    if usage.mcp_server_id or usage.tool_id:
        cost_meta["mcp_server_id"] = usage.mcp_server_id
        cost_meta["tool_id"] = usage.tool_id
        cost_meta["mcp_servers"] = list(usage_meta.get("mcp_servers") or ([usage.mcp_server_id] if usage.mcp_server_id else []))
        cost_meta["tools"] = list(usage_meta.get("tools") or ([usage.tool_id] if usage.tool_id else []))
        if isinstance(usage_meta.get("tool_mcp_map"), dict):
            cost_meta["tool_mcp_map"] = dict(usage_meta["tool_mcp_map"])
    if ignored_untrusted:
        cost_meta["ignored_untrusted_overrides"] = ignored_untrusted
    if alloc_warnings:
        cost_meta["attribution_warnings"] = alloc_warnings
    if fx_rate_source == "missing_fx_rate":
        cost_meta["fx_warning"] = f"Missing FX rate for {currency}->{base_currency}; fallback 1:1 recorded as violation."

    cost = CostEvent(
        cost_id=f"cost_{uuid4().hex}",
        usage_event_id=usage.usage_id,
        pricing_catalog_id=pricing_id,
        pricing_snapshot=snapshot,
        tenant_id=usage.tenant_id,
        org_id=usage.org_id,
        project_id=usage.project_id,
        cost_center=usage.cost_center,
        environment=usage.environment,
        user_id=usage.user_id,
        run_id=usage.run_id,
        workflow_id=usage.workflow_id,
        step_id=usage.step_id,
        checkpoint_id=usage.checkpoint_id,
        replay_attempt_id=usage.replay_attempt_id,
        resume_attempt_id=usage.resume_attempt_id,
        trace_id=usage.trace_id,
        provider=usage.provider,
        billing_platform=usage.billing_platform,
        resource_type=usage.resource_type,
        resource_subtype=usage.resource_subtype,
        model_name=usage.model_name,
        model_version=usage.model_version,
        cost_type=cost_type,
        usage_unit=usage.usage_unit,
        quantity=quantity,
        unit_price=unit_price,
        gross_cost=gross_cost,
        discount_rate=discount_rate,
        discount_amount=discount_amount,
        multiplier=multiplier,
        net_cost=net_cost,
        tax_amount=tax_amount,
        estimated_cost=Decimal("0") if is_actual else amount,
        actual_cost=amount if is_actual else Decimal("0"),
        currency=currency,
        base_currency=base_currency,
        base_currency_cost=base_currency_cost,
        fx_rate=fx_rate,
        fx_rate_source=fx_rate_source,
        calculation_method=usage.phase,
        calculation_version=FINOPS_CALCULATION_VERSION,
        allocation_tags={
            "tenant_id": usage.tenant_id,
            "workflow_id": usage.workflow_id,
            "project_id": usage.project_id,
            "cost_center": usage.cost_center,
            "org_id": usage.org_id,
            "environment": usage.environment,
            "custom": custom_alloc_tags,
            **({"mcp_server_id": usage.mcp_server_id} if usage.mcp_server_id else {}),
            **({"tool_id": usage.tool_id} if usage.tool_id else {}),
        },
        metadata_payload=cost_meta,
    )
    session.add(cost)
    await session.flush()
    FINOPS_USAGE_EVENTS_TOTAL.labels(
        provider=usage.provider or "internal",
        resource_type=usage.resource_type or "model",
        phase=usage.phase or "manual",
    ).inc()
    FINOPS_COST_USD_TOTAL.labels(
        tenant_id=usage.tenant_id or "default",
        cost_type=cost_type,
        provider=usage.provider or "internal",
    ).inc(float(pre_tax_base_cost))
    FINOPS_BASE_CURRENCY_COST_TOTAL.labels(
        tenant_id=usage.tenant_id or "default",
        currency=currency,
        base_currency=base_currency,
        fx_source=fx_rate_source,
    ).inc(float(pre_tax_base_cost))
    if tax_base_cost > Decimal("0"):
        FINOPS_TAX_TOTAL.labels(
            tenant_id=usage.tenant_id or "default",
            provider=usage.provider or "internal",
            base_currency=base_currency,
        ).inc(float(tax_base_cost))
    return cost


async def _record_missing_fx_violation(
    session: AsyncSession,
    usage: UsageEvent,
    *,
    currency: str,
    base_currency: str,
) -> None:
    session.add(
        BudgetViolation(
            violation_id=f"bv_{uuid4().hex}",
            tenant_id=usage.tenant_id,
            user_id=usage.user_id,
            run_id=usage.run_id,
            workflow_id=usage.workflow_id,
            step_id=usage.step_id,
            provider=usage.provider,
            model_name=usage.model_name,
            resource_type=usage.resource_type,
            violation_type="missing_fx_rate",
            severity="high",
            threshold_type="hard",
            actual_amount=Decimal("0"),
            estimated_amount=Decimal("0"),
            limit_amount=Decimal("0"),
            currency=currency,
            decision="record_violation",
            recommended_action="configure_fx_rate",
            action_taken="record_violation",
            details={"currency": currency, "base_currency": base_currency},
        )
    )
    FINOPS_BUDGET_VIOLATIONS_TOTAL.labels(
        tenant_id=usage.tenant_id or "default",
        scope="tenant",
        threshold_type="hard",
        decision="record_violation",
    ).inc()


async def _record_missing_pricing_violation(session: AsyncSession, usage: UsageEvent) -> None:
    session.add(
        BudgetViolation(
            violation_id=f"bv_{uuid4().hex}",
            tenant_id=usage.tenant_id,
            user_id=usage.user_id,
            run_id=usage.run_id,
            workflow_id=usage.workflow_id,
            step_id=usage.step_id,
            provider=usage.provider,
            model_name=usage.model_name,
            resource_type=usage.resource_type,
            violation_type="missing_pricing_rule",
            severity="medium",
            threshold_type="soft",
            actual_amount=Decimal("0"),
            estimated_amount=Decimal("0"),
            limit_amount=Decimal("0"),
            currency="USD",
            decision="record_violation",
            recommended_action="add_pricing_rule",
            action_taken="record_violation",
            details={"resource_type": usage.resource_type, "usage_unit": usage.usage_unit},
        )
    )


def _billable_quantity(usage: UsageEvent, pricing: PricingCatalog) -> Decimal:
    if pricing.price_unit == "per_1m_tokens":
        if usage.usage_unit == "per_1m_tokens":
            return _quantity(usage.quantity)
        tokens = {
            "input": usage.input_tokens or usage.quantity,
            "output": usage.output_tokens or usage.quantity,
            "cached_input": usage.cached_input_tokens or usage.quantity,
            "cache_write": usage.cache_write_tokens or usage.quantity,
            "cache_write_5m": usage.cache_write_tokens or usage.quantity,
            "cache_write_1h": usage.cache_write_tokens or usage.quantity,
            "reasoning": usage.reasoning_tokens or usage.quantity,
            "audio_input": usage.audio_input_tokens or usage.quantity,
            "audio_output": usage.audio_output_tokens or usage.quantity,
        }.get(pricing.token_type, usage.quantity)
        return _quantity(Decimal(str(tokens)) / Decimal("1000000"))
    if pricing.price_unit == "per_1k_calls":
        if usage.usage_unit == "per_1k_calls":
            return _quantity(usage.quantity)
        calls = usage.tool_call_count or usage.image_output_count or usage.quantity
        return _quantity(Decimal(str(calls)) / Decimal("1000"))
    if pricing.price_unit == "per_hour":
        seconds = usage.container_seconds or usage.worker_seconds or usage.quantity
        return _quantity(Decimal(str(seconds)) / Decimal("3600"))
    if pricing.price_unit == "per_minute":
        seconds = usage.video_input_seconds or usage.container_seconds or usage.worker_seconds or usage.quantity
        return _quantity(Decimal(str(seconds)) / Decimal("60"))
    if pricing.price_unit in {"per_image", "per_video"}:
        count = usage.image_output_count or usage.image_input_count or usage.quantity
        return _quantity(count)
    return _quantity(usage.quantity)


def _pricing_snapshot(pricing: PricingCatalog) -> dict[str, object]:
    return {
        "pricing_id": pricing.pricing_id,
        "provider": pricing.provider,
        "billing_platform": pricing.billing_platform,
        "provider_region": pricing.provider_region,
        "resource_type": pricing.resource_type,
        "resource_subtype": pricing.resource_subtype,
        "model_name": pricing.model_name,
        "model_version": pricing.model_version,
        "pricing_mode": pricing.pricing_mode,
        "token_type": pricing.token_type,
        "price_unit": pricing.price_unit,
        "unit_price": _decimal_json(pricing.unit_price),
        "currency": pricing.currency,
        "discount_rate": _decimal_json(pricing.discount_rate),
        "multiplier": _decimal_json(pricing.multiplier or 1),
        "source_url": pricing.source_url,
        "source_checked_at": pricing.source_checked_at.isoformat() if pricing.source_checked_at else "",
        "pricing_version": pricing.pricing_version,
        "effective_from": pricing.effective_from.isoformat() if pricing.effective_from else "",
    }


async def list_usage_events(
    session: AsyncSession,
    *,
    run_id: str | None = None,
    tenant_id: str | None = None,
) -> list[UsageEvent]:
    stmt = select(UsageEvent).order_by(UsageEvent.created_at.asc())
    if run_id:
        stmt = stmt.where(UsageEvent.run_id == run_id)
    if tenant_id:
        stmt = stmt.where(UsageEvent.tenant_id == tenant_id)
    return list((await session.scalars(stmt)).all())


async def _list_cost_events(
    session: AsyncSession,
    *,
    run_id: str | None = None,
    tenant_id: str | None = None,
    workflow_id: str | None = None,
    project_id: str | None = None,
    cost_center: str | None = None,
) -> list[CostEvent]:
    stmt = select(CostEvent).order_by(CostEvent.created_at.asc())
    if run_id:
        stmt = stmt.where(CostEvent.run_id == run_id)
    if tenant_id:
        stmt = stmt.where(CostEvent.tenant_id == tenant_id)
    if workflow_id:
        stmt = stmt.where(CostEvent.workflow_id == workflow_id)
    if project_id:
        stmt = stmt.where(CostEvent.project_id == project_id)
    if cost_center:
        stmt = stmt.where(CostEvent.cost_center == cost_center)
    return list((await session.scalars(stmt)).all())
