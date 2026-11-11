"""Pricing catalog: defaults, CRUD and price lookup."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finops import (
    PricingCatalog,
    PricingChangeLog,
    UsageEvent,
)
from app.schemas.finops import (
    PricingCatalogCreateRequest,
    PricingCatalogUpdateRequest,
)
from app.services.finops.core import (
    DEFAULT_PRICING,
    _decimal,
    _ensure_utc,
    _value,
)


async def ensure_default_pricing(session: AsyncSession) -> None:
    for item in DEFAULT_PRICING:
        existing = await session.scalar(
            select(PricingCatalog).where(
                PricingCatalog.provider == item["provider"],
                PricingCatalog.resource_type == item["resource_type"],
                PricingCatalog.model_name == item["model_name"],
                PricingCatalog.price_unit == item["price_unit"],
                PricingCatalog.token_type == item["token_type"],
                PricingCatalog.is_active.is_(True),
            )
        )
        if existing is not None:
            continue
        session.add(
            PricingCatalog(
                pricing_id=f"price_{uuid4().hex}",
                provider=item["provider"],
                provider_display_name="Internal",
                billing_platform="direct_api",
                provider_region="local",
                billing_region="local",
                resource_type=item["resource_type"],
                resource_subtype=item["resource_subtype"],
                model_name=item["model_name"],
                model_family="internal",
                model_tier="deterministic",
                modality="text",
                pricing_mode="standard",
                service_tier="standard",
                token_type=item["token_type"],
                price_unit=item["price_unit"],
                unit_price=item["unit_price"],
                currency="USD",
                source_url="internal://default-pricing",
                source_type="internal_default",
                source_confidence="system",
                pricing_version="v1.5.1-internal-default",
                scope_type="system_default",
                is_system_default=True,
                is_active=True,
                metadata_payload={"source": "v1.5.1_default"},
            )
        )
    await session.flush()


async def create_pricing_item(session: AsyncSession, payload: PricingCatalogCreateRequest) -> PricingCatalog:
    item = PricingCatalog(
        pricing_id=payload.pricing_id or f"price_{uuid4().hex}",
        provider=str(_value(payload.provider)),
        provider_display_name=payload.provider_display_name,
        billing_platform=str(_value(payload.billing_platform)),
        provider_region=payload.provider_region,
        billing_region=payload.billing_region,
        resource_type=str(_value(payload.resource_type)),
        resource_subtype=str(_value(payload.resource_subtype)),
        model_name=payload.model_name,
        model_alias=payload.model_alias,
        model_version=payload.model_version,
        model_family=payload.model_family,
        model_tier=payload.model_tier,
        modality=str(_value(payload.modality)),
        pricing_mode=str(_value(payload.pricing_mode)),
        service_tier=payload.service_tier,
        token_type=str(_value(payload.token_type)),
        price_unit=str(_value(payload.price_unit)),
        unit_price=_decimal(payload.unit_price),
        currency=payload.currency,
        tax_included=payload.tax_included,
        discount_rate=_decimal(payload.discount_rate),
        multiplier=_decimal(payload.multiplier),
        minimum_billable_quantity=_decimal(payload.minimum_billable_quantity),
        free_quota_quantity=_decimal(payload.free_quota_quantity),
        free_quota_period=payload.free_quota_period,
        input_threshold_tokens_min=payload.input_threshold_tokens_min,
        input_threshold_tokens_max=payload.input_threshold_tokens_max,
        context_window_tokens=payload.context_window_tokens,
        max_output_tokens=payload.max_output_tokens,
        cache_ttl_seconds=payload.cache_ttl_seconds,
        cache_storage_unit_price=_decimal(payload.cache_storage_unit_price),
        supports_cache=payload.supports_cache,
        supports_batch=payload.supports_batch,
        supports_tools=payload.supports_tools,
        supports_thinking=payload.supports_thinking,
        supports_streaming=payload.supports_streaming,
        supports_json_mode=payload.supports_json_mode,
        supports_multimodal=payload.supports_multimodal,
        source_url=payload.source_url,
        source_type=payload.source_type,
        source_confidence=payload.source_confidence,
        source_checked_at=payload.source_checked_at,
        pricing_version=payload.pricing_version,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        scope_type=str(_value(payload.scope_type)),
        scope_id=payload.scope_id or "",
        is_system_default=payload.is_system_default,
        is_tenant_override=payload.is_tenant_override,
        supersedes_pricing_id=payload.supersedes_pricing_id or "",
        import_batch_id=payload.import_batch_id or "",
        change_reason=payload.change_reason,
        created_by=payload.created_by,
        updated_by=payload.updated_by,
        is_active=payload.is_active,
        metadata_payload=payload.metadata,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def update_pricing_item(
    session: AsyncSession,
    pricing_id: str,
    payload: PricingCatalogUpdateRequest,
    *,
    updated_by: str = "",
) -> PricingCatalog | None:
    item = await session.scalar(select(PricingCatalog).where(PricingCatalog.pricing_id == pricing_id))
    if item is None:
        return None
    old_unit_price = _decimal(item.unit_price)
    updates = payload.model_dump(exclude_unset=True)
    supersede_existing = bool(updates.pop("supersede_existing", False))
    actor = str(updates.get("updated_by") or updated_by or item.updated_by or "admin")
    reason = str(updates.get("change_reason") or item.change_reason or ("manual_supersede" if supersede_existing else "manual_update"))
    now = datetime.now(timezone.utc)

    if supersede_existing:
        new_item = PricingCatalog(
            pricing_id=f"price_{uuid4().hex}",
            provider=item.provider,
            provider_display_name=item.provider_display_name,
            billing_platform=item.billing_platform,
            provider_region=item.provider_region,
            billing_region=item.billing_region,
            resource_type=item.resource_type,
            resource_subtype=item.resource_subtype,
            model_name=item.model_name,
            model_alias=item.model_alias,
            model_version=item.model_version,
            model_family=item.model_family,
            model_tier=item.model_tier,
            modality=item.modality,
            pricing_mode=item.pricing_mode,
            service_tier=item.service_tier,
            token_type=item.token_type,
            price_unit=item.price_unit,
            unit_price=item.unit_price,
            currency=item.currency,
            tax_included=item.tax_included,
            discount_rate=item.discount_rate,
            multiplier=item.multiplier,
            minimum_billable_quantity=item.minimum_billable_quantity,
            free_quota_quantity=item.free_quota_quantity,
            free_quota_period=item.free_quota_period,
            input_threshold_tokens_min=item.input_threshold_tokens_min,
            input_threshold_tokens_max=item.input_threshold_tokens_max,
            context_window_tokens=item.context_window_tokens,
            max_output_tokens=item.max_output_tokens,
            cache_ttl_seconds=item.cache_ttl_seconds,
            cache_storage_unit_price=item.cache_storage_unit_price,
            supports_cache=item.supports_cache,
            supports_batch=item.supports_batch,
            supports_tools=item.supports_tools,
            supports_thinking=item.supports_thinking,
            supports_streaming=item.supports_streaming,
            supports_json_mode=item.supports_json_mode,
            supports_multimodal=item.supports_multimodal,
            source_url=item.source_url,
            source_type=item.source_type,
            source_confidence=item.source_confidence,
            source_checked_at=item.source_checked_at,
            pricing_version=item.pricing_version,
            effective_from=now,
            effective_to=item.effective_to,
            scope_type=item.scope_type,
            scope_id=item.scope_id,
            is_system_default=item.is_system_default,
            is_tenant_override=item.is_tenant_override,
            supersedes_pricing_id=item.pricing_id,
            import_batch_id=item.import_batch_id,
            change_reason=reason,
            created_by=actor,
            updated_by=actor,
            is_active=True,
            metadata_payload=dict(item.metadata_payload or {}),
        )
        for key, value in updates.items():
            if key == "metadata":
                new_item.metadata_payload = value or {}
            elif isinstance(value, Enum):
                setattr(new_item, key, value.value)
            elif key in {
                "unit_price",
                "discount_rate",
                "multiplier",
                "minimum_billable_quantity",
                "free_quota_quantity",
                "cache_storage_unit_price",
            } and value is not None:
                setattr(new_item, key, _decimal(value))
            elif key in {"effective_from", "effective_to", "source_checked_at"} and value is not None:
                setattr(new_item, key, _ensure_utc(value))
            elif value is not None:
                setattr(new_item, key, value)
        item.is_active = False
        item.effective_to = now
        item.updated_by = actor
        session.add(new_item)
        session.add(
            PricingChangeLog(
                change_id=f"pcl_{uuid4().hex}",
                pricing_id_old=item.pricing_id,
                pricing_id_new=new_item.pricing_id,
                change_type="supersede",
                old_unit_price=old_unit_price,
                new_unit_price=_decimal(new_item.unit_price),
                currency=new_item.currency,
                change_reason=reason,
                source_url=new_item.source_url,
                changed_by=actor,
                import_batch_id=new_item.import_batch_id or "",
            )
        )
        await session.commit()
        await session.refresh(new_item)
        return new_item

    for key, value in updates.items():
        if key == "metadata":
            item.metadata_payload = value or {}
        elif isinstance(value, Enum):
            setattr(item, key, value.value)
        elif key in {
            "unit_price",
            "discount_rate",
            "multiplier",
            "minimum_billable_quantity",
            "free_quota_quantity",
            "cache_storage_unit_price",
        } and value is not None:
            setattr(item, key, _decimal(value))
        elif key in {"effective_from", "effective_to", "source_checked_at"} and value is not None:
            setattr(item, key, _ensure_utc(value))
        elif value is not None:
            setattr(item, key, value)
    item.updated_by = actor
    item.change_reason = reason
    session.add(
        PricingChangeLog(
            change_id=f"pcl_{uuid4().hex}",
            pricing_id_old=item.pricing_id,
            pricing_id_new=item.pricing_id,
            change_type="update",
            old_unit_price=old_unit_price,
            new_unit_price=_decimal(item.unit_price),
            currency=item.currency,
            change_reason=reason,
            source_url=item.source_url,
            changed_by=actor,
            import_batch_id=item.import_batch_id or "",
        )
    )
    await session.commit()
    await session.refresh(item)
    return item


async def disable_pricing_item(
    session: AsyncSession,
    pricing_id: str,
    *,
    updated_by: str = "",
    change_reason: str = "manual_disable",
) -> PricingCatalog | None:
    item = await session.scalar(select(PricingCatalog).where(PricingCatalog.pricing_id == pricing_id))
    if item is None:
        return None
    old_unit_price = _decimal(item.unit_price)
    actor = updated_by or item.updated_by or "admin"
    item.is_active = False
    item.updated_by = actor
    item.effective_to = datetime.now(timezone.utc)
    session.add(
        PricingChangeLog(
            change_id=f"pcl_{uuid4().hex}",
            pricing_id_old=item.pricing_id,
            pricing_id_new=item.pricing_id,
            change_type="disable",
            old_unit_price=old_unit_price,
            new_unit_price=_decimal(item.unit_price),
            currency=item.currency,
            change_reason=change_reason or item.change_reason or "manual_disable",
            source_url=item.source_url,
            changed_by=actor,
            import_batch_id=item.import_batch_id or "",
        )
    )
    await session.commit()
    await session.refresh(item)
    return item


async def list_pricing_items(
    session: AsyncSession,
    *,
    active_only: bool = False,
    provider: str | None = None,
    model_name: str | None = None,
    resource_type: str | None = None,
    pricing_mode: str | None = None,
    token_type: str | None = None,
    scope_type: str | None = None,
) -> list[PricingCatalog]:
    stmt = select(PricingCatalog).order_by(PricingCatalog.created_at.desc())
    if active_only:
        stmt = stmt.where(PricingCatalog.is_active.is_(True))
    if provider:
        stmt = stmt.where(PricingCatalog.provider == provider)
    if model_name:
        stmt = stmt.where(PricingCatalog.model_name.contains(model_name))
    if resource_type:
        stmt = stmt.where(PricingCatalog.resource_type == resource_type)
    if pricing_mode:
        stmt = stmt.where(PricingCatalog.pricing_mode == pricing_mode)
    if token_type:
        stmt = stmt.where(PricingCatalog.token_type == token_type)
    if scope_type:
        stmt = stmt.where(PricingCatalog.scope_type == scope_type)
    return list((await session.scalars(stmt)).all())


async def get_pricing_item(session: AsyncSession, pricing_id: str) -> PricingCatalog | None:
    return await session.scalar(select(PricingCatalog).where(PricingCatalog.pricing_id == pricing_id))


async def _lookup_pricing(session: AsyncSession, usage: UsageEvent) -> PricingCatalog | None:
    if usage.provider == "internal":
        await ensure_default_pricing(session)
    if usage.pricing_id:
        explicit = await session.scalar(
            select(PricingCatalog).where(
                PricingCatalog.pricing_id == usage.pricing_id,
                PricingCatalog.is_active.is_(True),
            )
        )
        if explicit is not None:
            return explicit

    usage_token_type = _token_type_for_usage(usage)
    price_units = [usage.usage_unit]
    normalized = _normalized_price_unit(usage.usage_unit)
    if normalized not in price_units:
        price_units.append(normalized)

    event_time = _ensure_utc(usage.created_at) or datetime.now(timezone.utc)
    meta = dict(usage.metadata_payload or {})
    requested_mode = str(meta.get("pricing_mode") or "")
    requested_service_tier = str(meta.get("service_tier") or "")

    stmt = (
        select(PricingCatalog)
        .where(
            PricingCatalog.is_active.is_(True),
            PricingCatalog.provider == usage.provider,
            PricingCatalog.billing_platform == usage.billing_platform,
            PricingCatalog.resource_type == usage.resource_type,
            PricingCatalog.price_unit.in_(price_units),
        )
        .order_by(PricingCatalog.created_at.desc(), PricingCatalog.id.desc())
    )
    candidates = list((await session.scalars(stmt)).all())
    matched: list[PricingCatalog] = []

    mcp_server_for_pricing = usage.mcp_server_id if usage.resource_type == "tool" else ""
    for item in candidates:
        if item.token_type not in {usage_token_type, "not_applicable"}:
            continue
        item_mcp = str((item.metadata_payload or {}).get("mcp_server_id") or "") if mcp_server_for_pricing else ""
        mcp_match = bool(mcp_server_for_pricing) and (item.model_name == mcp_server_for_pricing or item_mcp == mcp_server_for_pricing)
        if item_mcp and item_mcp != mcp_server_for_pricing:
            continue
        if item.model_name not in {usage.model_name, ""} and not mcp_match:
            continue
        eff_from = _ensure_utc(item.effective_from)
        eff_to = _ensure_utc(item.effective_to)
        if eff_from is not None and eff_from > event_time:
            continue
        if eff_to is not None and eff_to < event_time:
            continue

        if item.scope_type == "tenant_override" or item.is_tenant_override:
            if item.scope_id and item.scope_id != usage.tenant_id:
                continue
        elif item.scope_type == "contract":
            if item.scope_id and item.scope_id not in {usage.tenant_id, usage.org_id}:
                continue

        if requested_mode and item.pricing_mode != requested_mode:
            continue
        if requested_service_tier and item.service_tier != requested_service_tier:
            continue

        if usage.input_tokens > 0:
            if item.input_threshold_tokens_min is not None and usage.input_tokens < item.input_threshold_tokens_min:
                continue
            if item.input_threshold_tokens_max is not None and usage.input_tokens > item.input_threshold_tokens_max:
                continue
        else:
            if item.input_threshold_tokens_min is not None and item.input_threshold_tokens_min > 0:
                continue

        matched.append(item)

    if not matched:
        return None

    def _candidate_rank(item: PricingCatalog) -> tuple[int, int, int, int, int, int, int]:
        model_exact = 1 if item.model_name == usage.model_name else 0
        if mcp_server_for_pricing and (
            item.model_name == mcp_server_for_pricing
            or str((item.metadata_payload or {}).get("mcp_server_id") or "") == mcp_server_for_pricing
        ):
            model_exact = 2
        if (item.scope_type == "tenant_override" or item.is_tenant_override) and (
            item.scope_id == usage.tenant_id or not item.scope_id
        ):
            scope_rank = 4
        elif item.scope_type == "contract" and (
            item.scope_id in {usage.tenant_id, usage.org_id} or not item.scope_id
        ):
            scope_rank = 3
        elif item.scope_type == "system_default" or item.is_system_default:
            scope_rank = 2
        elif item.scope_type == "manual":
            scope_rank = 1
        else:
            scope_rank = 0

        if requested_mode:
            mode_rank = 2 if item.pricing_mode == requested_mode else 0
        else:
            mode_rank = 2 if item.pricing_mode in {"standard", "tenant_override", "contract", "manual"} else 1

        has_threshold = (
            1
            if (item.input_threshold_tokens_min is not None or item.input_threshold_tokens_max is not None)
            else 0
        )
        min_threshold = int(item.input_threshold_tokens_min or 0)
        token_exact = 1 if item.token_type == usage_token_type else 0
        row_id = int(item.id or 0)
        return (model_exact, scope_rank, mode_rank, has_threshold, min_threshold, token_exact, row_id)

    matched.sort(key=_candidate_rank, reverse=True)
    return matched[0]


def _token_type_for_usage(usage: UsageEvent) -> str:
    explicit = str((usage.metadata_payload or {}).get("token_type") or "")
    if explicit:
        return explicit
    if usage.cached_input_tokens:
        return "cached_input"
    if usage.cache_write_tokens:
        return "cache_write"
    if usage.reasoning_tokens:
        return "reasoning"
    if usage.audio_input_tokens:
        return "audio_input"
    if usage.audio_output_tokens:
        return "audio_output"
    if usage.image_output_count:
        return "image_output"
    if usage.output_tokens or usage.usage_unit == "output_tokens":
        return "output"
    if usage.input_tokens or usage.usage_unit in {"input_tokens", "tokens"}:
        return "input"
    return "not_applicable"


def _normalized_price_unit(usage_unit: str) -> str:
    if usage_unit in {
        "tokens",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
    }:
        return "per_1m_tokens"
    if usage_unit in {"calls", "requests"}:
        return "per_1k_calls"
    if usage_unit in {"tool_call", "replay_attempt", "checkpoint_write"}:
        return "per_call"
    if usage_unit == "worker_seconds":
        return "per_second"
    return usage_unit
