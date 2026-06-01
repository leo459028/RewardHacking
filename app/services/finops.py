from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.checkpoint import ReplayAttempt, RunCheckpoint
from app.models.control_plane import ExecutionPlan
from app.models.finops import BudgetPolicy, BudgetViolation, CostEvent, PricingCatalog, UsageEvent
from app.schemas.agent import AgentRequest
from app.schemas.finops import (
    BudgetPolicyCreateRequest,
    BudgetPolicyUpdateRequest,
    PricingCatalogCreateRequest,
    PricingCatalogUpdateRequest,
    UsageEventCreateRequest,
)


DEFAULT_PRICING: tuple[dict, ...] = (
    {
        "provider": "internal",
        "resource_type": "model",
        "resource_subtype": "chat_completion",
        "model_name": "deterministic-control-plane",
        "token_type": "input",
        "price_unit": "input_tokens",
        "unit_price": Decimal("0.00000010"),
    },
    {
        "provider": "internal",
        "resource_type": "model",
        "resource_subtype": "chat_completion",
        "model_name": "deterministic-control-plane",
        "token_type": "output",
        "price_unit": "output_tokens",
        "unit_price": Decimal("0.00000020"),
    },
    {
        "provider": "internal",
        "resource_type": "tool",
        "resource_subtype": "other",
        "model_name": "",
        "token_type": "not_applicable",
        "price_unit": "tool_call",
        "unit_price": Decimal("0.00050000"),
    },
    {
        "provider": "internal",
        "resource_type": "runtime",
        "resource_subtype": "other",
        "model_name": "",
        "token_type": "not_applicable",
        "price_unit": "worker_seconds",
        "unit_price": Decimal("0.00001000"),
    },
    {
        "provider": "internal",
        "resource_type": "storage",
        "resource_subtype": "other",
        "model_name": "",
        "token_type": "not_applicable",
        "price_unit": "artifact_mb",
        "unit_price": Decimal("0.00000100"),
    },
    {
        "provider": "internal",
        "resource_type": "runtime",
        "resource_subtype": "other",
        "model_name": "",
        "token_type": "not_applicable",
        "price_unit": "replay_attempt",
        "unit_price": Decimal("0.00020000"),
    },
    {
        "provider": "internal",
        "resource_type": "storage",
        "resource_subtype": "other",
        "model_name": "",
        "token_type": "not_applicable",
        "price_unit": "checkpoint_write",
        "unit_price": Decimal("0.00010000"),
    },
)


@dataclass(frozen=True)
class RunCostSummary:
    run_id: str
    tenant_id: str
    base_currency: str
    estimated_cost: Decimal
    actual_cost: Decimal
    cost_delta: Decimal
    cost_delta_percent: Decimal
    currency: str
    budget_status: str
    budget_policy_id: str = ""
    budget_remaining: Decimal = Decimal("0")
    provider_breakdown: dict[str, Decimal] = field(default_factory=dict)
    model_breakdown: dict[str, Decimal] = field(default_factory=dict)
    tool_breakdown: dict[str, Decimal] = field(default_factory=dict)
    step_breakdown: dict[str, Decimal] = field(default_factory=dict)
    cost_type_breakdown: dict[str, Decimal] = field(default_factory=dict)
    resource_breakdown: dict[str, Decimal] = field(default_factory=dict)
    breakdown: dict[str, Decimal] = field(default_factory=dict)
    replay_cost: Decimal = Decimal("0")
    resume_incremental_cost: Decimal = Decimal("0")
    failed_retry_cost: Decimal = Decimal("0")
    usage_events: list[UsageEvent] = field(default_factory=list)
    cost_events: list[CostEvent] = field(default_factory=list)


@dataclass(frozen=True)
class TenantCostSummary:
    tenant_id: str
    today_cost: Decimal
    month_to_date_cost: Decimal
    budget_used_percent: Decimal
    violation_count: int
    provider_breakdown: dict[str, Decimal] = field(default_factory=dict)
    resource_type_breakdown: dict[str, Decimal] = field(default_factory=dict)
    environment_breakdown: dict[str, Decimal] = field(default_factory=dict)
    project_breakdown: dict[str, Decimal] = field(default_factory=dict)
    top_runs: list[dict] = field(default_factory=list)
    top_models: list[dict] = field(default_factory=list)
    top_workflows: list[dict] = field(default_factory=list)
    top_tools: list[dict] = field(default_factory=list)
    top_users: list[dict] = field(default_factory=list)
    failed_run_cost: Decimal = Decimal("0")
    replay_cost: Decimal = Decimal("0")
    cache_savings: Decimal = Decimal("0")
    batch_savings: Decimal = Decimal("0")


def _value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _money(value: object) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.00000001"))


def _quantity(value: object) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.000001"))


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or "0"))


def _float(value: object) -> float:
    return float(value or 0)


def _decimal_json(value: object) -> str:
    return format(Decimal(str(value or "0")).quantize(Decimal("0.00000001")), "f")


def pricing_to_dict(item: PricingCatalog) -> dict:
    return {
        "pricing_id": item.pricing_id,
        "provider": item.provider,
        "provider_display_name": item.provider_display_name,
        "billing_platform": item.billing_platform,
        "provider_region": item.provider_region,
        "billing_region": item.billing_region,
        "resource_type": item.resource_type,
        "resource_subtype": item.resource_subtype,
        "model_name": item.model_name,
        "model_alias": item.model_alias,
        "model_version": item.model_version,
        "model_family": item.model_family,
        "model_tier": item.model_tier,
        "modality": item.modality,
        "pricing_mode": item.pricing_mode,
        "service_tier": item.service_tier,
        "token_type": item.token_type,
        "price_unit": item.price_unit,
        "unit_price": item.unit_price,
        "currency": item.currency,
        "tax_included": item.tax_included,
        "discount_rate": item.discount_rate,
        "multiplier": item.multiplier,
        "minimum_billable_quantity": item.minimum_billable_quantity,
        "free_quota_quantity": item.free_quota_quantity,
        "free_quota_period": item.free_quota_period,
        "input_threshold_tokens_min": item.input_threshold_tokens_min,
        "input_threshold_tokens_max": item.input_threshold_tokens_max,
        "context_window_tokens": item.context_window_tokens,
        "max_output_tokens": item.max_output_tokens,
        "cache_ttl_seconds": item.cache_ttl_seconds,
        "cache_storage_unit_price": item.cache_storage_unit_price,
        "supports_cache": item.supports_cache,
        "supports_batch": item.supports_batch,
        "supports_tools": item.supports_tools,
        "supports_thinking": item.supports_thinking,
        "supports_streaming": item.supports_streaming,
        "supports_json_mode": item.supports_json_mode,
        "supports_multimodal": item.supports_multimodal,
        "source_url": item.source_url,
        "source_type": item.source_type,
        "source_confidence": item.source_confidence,
        "source_checked_at": item.source_checked_at,
        "pricing_version": item.pricing_version,
        "effective_from": item.effective_from,
        "effective_to": item.effective_to,
        "scope_type": item.scope_type,
        "scope_id": item.scope_id or None,
        "is_system_default": item.is_system_default,
        "is_tenant_override": item.is_tenant_override,
        "supersedes_pricing_id": item.supersedes_pricing_id or None,
        "import_batch_id": item.import_batch_id or None,
        "change_reason": item.change_reason,
        "created_by": item.created_by,
        "updated_by": item.updated_by,
        "is_active": item.is_active,
        "metadata": item.metadata_payload,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def usage_to_dict(item: UsageEvent) -> dict:
    return {
        "usage_id": item.usage_id,
        "tenant_id": item.tenant_id,
        "org_id": item.org_id,
        "project_id": item.project_id,
        "environment": item.environment,
        "cost_center": item.cost_center,
        "user_id": item.user_id,
        "workflow_id": item.workflow_id,
        "workflow_version": item.workflow_version,
        "run_id": item.run_id,
        "step_id": item.step_id,
        "attempt_id": item.attempt_id,
        "trace_id": item.trace_id,
        "span_id": item.span_id,
        "checkpoint_id": item.checkpoint_id,
        "replay_attempt_id": item.replay_attempt_id,
        "resume_attempt_id": item.resume_attempt_id,
        "provider": item.provider,
        "billing_platform": item.billing_platform,
        "provider_request_id": item.provider_request_id,
        "provider_response_id": item.provider_response_id,
        "model_name": item.model_name,
        "model_version": item.model_version,
        "pricing_id": item.pricing_id,
        "resource_type": item.resource_type,
        "resource_subtype": item.resource_subtype,
        "usage_unit": item.usage_unit,
        "quantity": item.quantity,
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "cached_input_tokens": item.cached_input_tokens,
        "cache_write_tokens": item.cache_write_tokens,
        "reasoning_tokens": item.reasoning_tokens,
        "audio_input_tokens": item.audio_input_tokens,
        "audio_output_tokens": item.audio_output_tokens,
        "image_input_count": item.image_input_count,
        "image_output_count": item.image_output_count,
        "video_input_seconds": item.video_input_seconds,
        "tool_id": item.tool_id,
        "tool_name": item.tool_name,
        "tool_provider": item.tool_provider,
        "tool_call_count": item.tool_call_count,
        "mcp_server_id": item.mcp_server_id,
        "tool_risk_level": item.tool_risk_level,
        "dry_run": item.dry_run,
        "external_side_effect": item.external_side_effect,
        "container_seconds": item.container_seconds,
        "worker_seconds": item.worker_seconds,
        "storage_gb_seconds": item.storage_gb_seconds,
        "network_egress_gb": item.network_egress_gb,
        "phase": item.phase,
        "status": item.status,
        "error_code": item.error_code,
        "error_message_hash": item.error_message_hash,
        "latency_ms": item.latency_ms,
        "queue_wait_ms": item.queue_wait_ms,
        "retry_count": item.retry_count,
        "idempotency_key": item.idempotency_key,
        "metadata": item.metadata_payload,
        "created_at": item.created_at,
    }


def cost_to_dict(item: CostEvent) -> dict:
    return {
        "cost_id": item.cost_id,
        "usage_event_id": item.usage_event_id,
        "pricing_catalog_id": item.pricing_catalog_id,
        "pricing_snapshot": item.pricing_snapshot,
        "tenant_id": item.tenant_id,
        "org_id": item.org_id,
        "project_id": item.project_id,
        "cost_center": item.cost_center,
        "environment": item.environment,
        "user_id": item.user_id,
        "run_id": item.run_id,
        "workflow_id": item.workflow_id,
        "step_id": item.step_id,
        "checkpoint_id": item.checkpoint_id,
        "replay_attempt_id": item.replay_attempt_id,
        "resume_attempt_id": item.resume_attempt_id,
        "trace_id": item.trace_id,
        "provider": item.provider,
        "billing_platform": item.billing_platform,
        "resource_type": item.resource_type,
        "resource_subtype": item.resource_subtype,
        "model_name": item.model_name,
        "model_version": item.model_version,
        "cost_type": item.cost_type,
        "usage_unit": item.usage_unit,
        "quantity": item.quantity,
        "unit_price": item.unit_price,
        "gross_cost": item.gross_cost,
        "discount_rate": item.discount_rate,
        "discount_amount": item.discount_amount,
        "multiplier": item.multiplier,
        "net_cost": item.net_cost,
        "tax_amount": item.tax_amount,
        "estimated_cost": item.estimated_cost,
        "actual_cost": item.actual_cost,
        "currency": item.currency,
        "base_currency": item.base_currency,
        "base_currency_cost": item.base_currency_cost,
        "fx_rate": item.fx_rate,
        "fx_rate_source": item.fx_rate_source,
        "billing_period_start": item.billing_period_start,
        "billing_period_end": item.billing_period_end,
        "provider_invoice_id": item.provider_invoice_id,
        "calculation_method": item.calculation_method,
        "calculation_version": item.calculation_version,
        "allocation_tags": item.allocation_tags,
        "metadata": item.metadata_payload,
        "created_at": item.created_at,
    }


def budget_to_dict(item: BudgetPolicy) -> dict:
    return {
        "budget_id": item.budget_id,
        "tenant_id": item.tenant_id,
        "scope": item.scope,
        "scope_id": item.scope_id,
        "project_id": item.project_id,
        "environment": item.environment,
        "policy_name": item.policy_name,
        "policy_version": item.policy_version,
        "period": item.period,
        "period_start": item.period_start,
        "period_end": item.period_end,
        "timezone": item.timezone,
        "limit_amount": item.limit_amount,
        "soft_limit_amount": item.soft_limit_amount,
        "hard_limit_amount": item.hard_limit_amount,
        "currency": item.currency,
        "base_currency": item.base_currency,
        "resource_type": item.resource_type,
        "provider": item.provider,
        "model_name": item.model_name,
        "model_tier": item.model_tier,
        "tool_id": item.tool_id,
        "workflow_id": item.workflow_id,
        "budget_basis": item.budget_basis,
        "include_failed_runs": item.include_failed_runs,
        "include_replay_runs": item.include_replay_runs,
        "include_dev_environment": item.include_dev_environment,
        "actions": item.actions,
        "actions_on_soft_limit": item.actions_on_soft_limit,
        "actions_on_hard_limit": item.actions_on_hard_limit,
        "notification_channels": item.notification_channels,
        "approval_required": item.approval_required,
        "approver_role": item.approver_role,
        "fallback_model_tier": item.fallback_model_tier,
        "effective_from": item.effective_from,
        "effective_to": item.effective_to,
        "created_by": item.created_by,
        "updated_by": item.updated_by,
        "is_active": item.is_active,
        "metadata": item.metadata_payload,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def violation_to_dict(item: BudgetViolation) -> dict:
    return {
        "violation_id": item.violation_id,
        "budget_id": item.budget_id,
        "budget_policy_version": item.budget_policy_version,
        "tenant_id": item.tenant_id,
        "scope": item.scope,
        "scope_id": item.scope_id,
        "project_id": item.project_id,
        "environment": item.environment,
        "user_id": item.user_id,
        "run_id": item.run_id,
        "step_id": item.step_id,
        "workflow_id": item.workflow_id,
        "provider": item.provider,
        "model_name": item.model_name,
        "resource_type": item.resource_type,
        "violation_type": item.violation_type,
        "severity": item.severity,
        "threshold_type": item.threshold_type,
        "estimated_amount": item.estimated_amount,
        "actual_amount": item.actual_amount,
        "limit_amount": item.limit_amount,
        "remaining_budget_before": item.remaining_budget_before,
        "overage_amount": item.overage_amount,
        "overage_percent": item.overage_percent,
        "currency": item.currency,
        "decision": item.decision,
        "recommended_action": item.recommended_action,
        "action_taken": item.action_taken,
        "approval_id": item.approval_id,
        "acknowledged_by": item.acknowledged_by,
        "acknowledged_at": item.acknowledged_at,
        "resolved_by": item.resolved_by,
        "resolved_at": item.resolved_at,
        "resolution_status": item.resolution_status,
        "details": item.details,
        "created_at": item.created_at,
    }


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
) -> PricingCatalog | None:
    item = await session.scalar(select(PricingCatalog).where(PricingCatalog.pricing_id == pricing_id))
    if item is None:
        return None
    updates = payload.model_dump(exclude_unset=True)
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
        elif value is not None:
            setattr(item, key, value)
    await session.commit()
    await session.refresh(item)
    return item


async def disable_pricing_item(session: AsyncSession, pricing_id: str, *, updated_by: str = "") -> PricingCatalog | None:
    item = await session.scalar(select(PricingCatalog).where(PricingCatalog.pricing_id == pricing_id))
    if item is None:
        return None
    item.is_active = False
    item.updated_by = updated_by
    item.effective_to = datetime.now(timezone.utc)
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

    stmt = (
        select(PricingCatalog)
        .where(
            PricingCatalog.is_active.is_(True),
            PricingCatalog.provider == usage.provider,
            PricingCatalog.billing_platform == usage.billing_platform,
            PricingCatalog.resource_type == usage.resource_type,
            PricingCatalog.price_unit.in_(price_units),
        )
        .order_by(PricingCatalog.model_name.desc(), PricingCatalog.created_at.desc())
    )
    candidates = list((await session.scalars(stmt)).all())
    for item in candidates:
        if item.model_name == usage.model_name and item.token_type in {usage_token_type, "not_applicable"}:
            return item
    for item in candidates:
        if item.model_name == "" and item.token_type in {usage_token_type, "not_applicable"}:
            return item
    return None


def _token_type_for_usage(usage: UsageEvent) -> str:
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
    if usage.input_tokens or usage.usage_unit == "input_tokens":
        return "input"
    return "not_applicable"


def _normalized_price_unit(usage_unit: str) -> str:
    if usage_unit in {
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
    }:
        return "per_1m_tokens"
    if usage_unit in {"tool_call", "replay_attempt", "checkpoint_write"}:
        return "per_call"
    if usage_unit == "worker_seconds":
        return "per_second"
    return usage_unit


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


async def _record_cost_for_usage(session: AsyncSession, usage: UsageEvent) -> CostEvent:
    pricing = await _lookup_pricing(session, usage)
    if pricing is None:
        await _record_missing_pricing_violation(session, usage)
        pricing_id = ""
        currency = "USD"
        unit_price = Decimal("0")
        quantity = _quantity(usage.quantity)
        gross_cost = Decimal("0")
        discount_rate = Decimal("0")
        discount_amount = Decimal("0")
        multiplier = Decimal("1")
        net_cost = Decimal("0")
        snapshot: dict[str, object] = {}
    else:
        pricing_id = pricing.pricing_id
        currency = pricing.currency
        unit_price = _money(pricing.unit_price)
        quantity = _billable_quantity(usage, pricing)
        gross_cost = _money(quantity * unit_price)
        discount_rate = _money(pricing.discount_rate)
        discount_amount = _money(gross_cost * discount_rate)
        multiplier = _money(pricing.multiplier or 1)
        net_cost = _money((gross_cost - discount_amount) * multiplier)
        snapshot = _pricing_snapshot(pricing)

    is_actual = usage.phase == "actual"
    amount = net_cost
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
        cost_type="actual" if is_actual else "estimated",
        usage_unit=usage.usage_unit,
        quantity=quantity,
        unit_price=unit_price,
        gross_cost=gross_cost,
        discount_rate=discount_rate,
        discount_amount=discount_amount,
        multiplier=multiplier,
        net_cost=net_cost,
        tax_amount=Decimal("0"),
        estimated_cost=Decimal("0") if is_actual else amount,
        actual_cost=amount if is_actual else Decimal("0"),
        currency=currency,
        base_currency="USD",
        base_currency_cost=amount,
        fx_rate=Decimal("1"),
        fx_rate_source="same_currency",
        calculation_method=usage.phase,
        calculation_version="v1.5.1",
        allocation_tags={"tenant_id": usage.tenant_id, "workflow_id": usage.workflow_id},
        metadata_payload={"usage_unit": usage.usage_unit, "usage_status": usage.status},
    )
    session.add(cost)
    await session.flush()
    return cost


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
) -> list[CostEvent]:
    stmt = select(CostEvent).order_by(CostEvent.created_at.asc())
    if run_id:
        stmt = stmt.where(CostEvent.run_id == run_id)
    if tenant_id:
        stmt = stmt.where(CostEvent.tenant_id == tenant_id)
    if workflow_id:
        stmt = stmt.where(CostEvent.workflow_id == workflow_id)
    return list((await session.scalars(stmt)).all())


def _token_estimate(text: str) -> int:
    return max(1, len(text.split()) * 4)


async def record_submit_usage(session: AsyncSession, *, run: AgentRun, plan: ExecutionPlan | None) -> None:
    request = AgentRequest.model_validate(run.request_payload)
    workflow_id = plan.workflow_id if plan is not None else request.task_type
    trace_id = (plan.trace_id if plan is not None else request.metadata.get("trace_id", "")) or ""
    tool_count = max(1, len(request.allowed_tools or ["summarize"]))
    input_tokens = _token_estimate(request.prompt + " " + request.goal)
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_1_plan",
            provider="internal",
            model_name="deterministic-control-plane",
            resource_type="model",
            resource_subtype="chat_completion",
            usage_unit="input_tokens",
            quantity=input_tokens,
            input_tokens=input_tokens,
            phase="estimated",
            metadata={"source": "submit"},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
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
            metadata={"tools": request.allowed_tools},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_3_replay_bundle",
            resource_type="storage",
            usage_unit="artifact_mb",
            quantity=Decimal("0.01"),
            phase="estimated",
            metadata={"source": "replay_bundle_placeholder"},
        ),
    ]
    await _record_events_and_budget(session, run=run, events=events)


async def record_actual_usage(session: AsyncSession, *, run: AgentRun, plan: ExecutionPlan | None) -> None:
    request = AgentRequest.model_validate(run.request_payload)
    workflow_id = plan.workflow_id if plan is not None else request.task_type
    trace_id = (plan.trace_id if plan is not None else request.metadata.get("trace_id", "")) or ""
    transcript = run.evaluation_summary.get("execution_event", {}).get("payload", {}).get("tool_transcript", [])
    output_tokens = _token_estimate(run.execution_output)
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
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
            metadata={"source": "worker", "completed_tools": len(transcript)},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_3_evaluation",
            provider="internal",
            model_name="deterministic-control-plane",
            resource_type="model",
            resource_subtype="chat_completion",
            usage_unit="output_tokens",
            quantity=output_tokens,
            output_tokens=output_tokens,
            phase="actual",
            metadata={"source": "worker"},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
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
            metadata={"source": "worker"},
        ),
    ]
    await _record_events_and_budget(session, run=run, events=events)


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
) -> None:
    for event in events:
        usage = _usage_from_payload(event)
        session.add(usage)
        await session.flush()
        await _record_cost_for_usage(session, usage)
    await _check_budgets(session, run=run)
    await session.commit()


async def create_budget_policy(session: AsyncSession, payload: BudgetPolicyCreateRequest) -> BudgetPolicy:
    limit_amount = payload.limit_amount or payload.hard_limit_amount or payload.soft_limit_amount or Decimal("0")
    hard_limit = payload.hard_limit_amount or limit_amount
    soft_limit = payload.soft_limit_amount or hard_limit
    item = BudgetPolicy(
        budget_id=payload.budget_id or f"budget_{uuid4().hex}",
        tenant_id=payload.tenant_id or "default",
        scope=payload.scope,
        scope_id=payload.scope_id,
        project_id=payload.project_id,
        environment=payload.environment,
        policy_name=payload.policy_name,
        policy_version=payload.policy_version,
        period=str(_value(payload.period)),
        period_start=payload.period_start,
        period_end=payload.period_end,
        timezone=payload.timezone,
        limit_amount=_money(limit_amount),
        soft_limit_amount=_money(soft_limit),
        hard_limit_amount=_money(hard_limit),
        currency=payload.currency,
        base_currency=payload.base_currency,
        resource_type=payload.resource_type,
        provider=payload.provider,
        model_name=payload.model_name,
        model_tier=payload.model_tier,
        tool_id=payload.tool_id,
        workflow_id=payload.workflow_id,
        budget_basis=str(_value(payload.budget_basis)),
        include_failed_runs=payload.include_failed_runs,
        include_replay_runs=payload.include_replay_runs,
        include_dev_environment=payload.include_dev_environment,
        actions=[str(_value(item)) for item in payload.actions],
        actions_on_soft_limit=[str(_value(item)) for item in payload.actions_on_soft_limit],
        actions_on_hard_limit=[str(_value(item)) for item in payload.actions_on_hard_limit],
        notification_channels=payload.notification_channels,
        approval_required=payload.approval_required,
        approver_role=payload.approver_role,
        fallback_model_tier=payload.fallback_model_tier,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        created_by=payload.created_by,
        updated_by=payload.updated_by,
        is_active=payload.is_active,
        metadata_payload=payload.metadata,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def update_budget_policy(
    session: AsyncSession,
    budget_id: str,
    payload: BudgetPolicyUpdateRequest,
) -> BudgetPolicy | None:
    item = await session.scalar(select(BudgetPolicy).where(BudgetPolicy.budget_id == budget_id))
    if item is None:
        return None
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        if key == "metadata":
            item.metadata_payload = value or {}
        elif isinstance(value, Enum):
            setattr(item, key, value.value)
        elif key in {"limit_amount", "soft_limit_amount", "hard_limit_amount"} and value is not None:
            setattr(item, key, _money(value))
        elif key in {"actions", "actions_on_soft_limit", "actions_on_hard_limit"} and value is not None:
            setattr(item, key, [str(_value(entry)) for entry in value])
        elif value is not None:
            setattr(item, key, value)
    await session.commit()
    await session.refresh(item)
    return item


async def list_budget_policies(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> list[BudgetPolicy]:
    stmt = select(BudgetPolicy).order_by(BudgetPolicy.created_at.desc())
    if tenant_id:
        stmt = stmt.where(BudgetPolicy.tenant_id == tenant_id)
    if active_only:
        stmt = stmt.where(BudgetPolicy.is_active.is_(True))
    return list((await session.scalars(stmt)).all())


async def list_budget_violations(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
) -> list[BudgetViolation]:
    stmt = select(BudgetViolation).order_by(BudgetViolation.created_at.desc()).limit(limit)
    if tenant_id:
        stmt = stmt.where(BudgetViolation.tenant_id == tenant_id)
    if run_id:
        stmt = stmt.where(BudgetViolation.run_id == run_id)
    return list((await session.scalars(stmt)).all())


async def _check_budgets(session: AsyncSession, *, run: AgentRun) -> None:
    policies = await list_budget_policies(session, tenant_id=run.tenant_id, active_only=True)
    if not policies:
        return
    summary = await run_cost_summary(session, run_id=run.run_id)
    run_total = _money(summary.estimated_cost + summary.actual_cost)
    for policy in policies:
        if policy.scope != "run":
            continue
        limit = _money(policy.hard_limit_amount or policy.limit_amount)
        if run_total <= limit:
            continue
        existing = await session.scalar(
            select(BudgetViolation).where(
                BudgetViolation.budget_id == policy.budget_id,
                BudgetViolation.run_id == run.run_id,
                BudgetViolation.violation_type == "run_budget_exceeded",
            )
        )
        if existing is not None:
            continue
        decision = "fail_closed" if "fail_closed" in policy.actions_on_hard_limit or "fail_closed" in policy.actions else "warn"
        overage = _money(run_total - limit)
        session.add(
            BudgetViolation(
                violation_id=f"bv_{uuid4().hex}",
                budget_id=policy.budget_id,
                budget_policy_version=policy.policy_version,
                tenant_id=run.tenant_id,
                scope=policy.scope,
                scope_id=policy.scope_id,
                project_id=policy.project_id,
                environment=policy.environment,
                user_id=run.user_id,
                run_id=run.run_id,
                workflow_id=summary.cost_events[0].workflow_id if summary.cost_events else "",
                provider=summary.cost_events[0].provider if summary.cost_events else "",
                model_name=summary.cost_events[0].model_name if summary.cost_events else "",
                resource_type=summary.cost_events[0].resource_type if summary.cost_events else "",
                violation_type="run_budget_exceeded",
                severity="high" if decision == "fail_closed" else "medium",
                threshold_type="hard",
                estimated_amount=summary.estimated_cost,
                actual_amount=run_total,
                limit_amount=limit,
                remaining_budget_before=Decimal("0"),
                overage_amount=overage,
                overage_percent=_money((overage / limit * 100) if limit else 0),
                currency=policy.currency,
                decision=decision,
                recommended_action=decision,
                action_taken="record_violation",
                details={
                    "actions": policy.actions,
                    "actions_on_hard_limit": policy.actions_on_hard_limit,
                    "scope": policy.scope,
                    "period": policy.period,
                },
            )
        )


async def run_cost_summary(session: AsyncSession, *, run_id: str) -> RunCostSummary:
    usage = await list_usage_events(session, run_id=run_id)
    costs = await _list_cost_events(session, run_id=run_id)
    estimated = _money(sum(_decimal(item.estimated_cost) for item in costs))
    actual = _money(sum(_decimal(item.actual_cost) for item in costs))
    tenant_id = usage[0].tenant_id if usage else ""
    provider_breakdown = _breakdown(costs, "provider")
    model_breakdown = _breakdown(costs, "model_name")
    tool_breakdown = _breakdown(costs, "tool_id")
    step_breakdown = _breakdown(costs, "step_id")
    cost_type_breakdown = _breakdown(costs, "cost_type")
    resource_breakdown = _breakdown(costs, "resource_type")
    violations = await list_budget_violations(session, run_id=run_id)
    if any(item.decision == "fail_closed" for item in violations):
        budget_status = "exceeded"
    elif violations:
        budget_status = "warning"
    else:
        budget_status = "within_budget"
    cost_delta = _money(actual - estimated)
    return RunCostSummary(
        run_id=run_id,
        tenant_id=tenant_id,
        base_currency=costs[0].base_currency if costs else "USD",
        estimated_cost=estimated,
        actual_cost=actual,
        cost_delta=cost_delta,
        cost_delta_percent=_money((cost_delta / estimated * 100) if estimated else 0),
        currency=costs[0].currency if costs else "USD",
        budget_status=budget_status,
        provider_breakdown=provider_breakdown,
        model_breakdown=model_breakdown,
        tool_breakdown=tool_breakdown,
        step_breakdown=step_breakdown,
        cost_type_breakdown=cost_type_breakdown,
        resource_breakdown=resource_breakdown,
        breakdown={f"{key}_cost": value for key, value in resource_breakdown.items()},
        replay_cost=sum((_total_cost(item) for item in costs if item.replay_attempt_id), Decimal("0")),
        resume_incremental_cost=sum((_total_cost(item) for item in costs if item.resume_attempt_id), Decimal("0")),
        failed_retry_cost=sum((_total_cost(item) for item in costs if item.cost_type == "failed_retry"), Decimal("0")),
        usage_events=usage,
        cost_events=costs,
    )


async def tenant_cost_summary(session: AsyncSession, *, tenant_id: str) -> TenantCostSummary:
    costs = await _list_cost_events(session, tenant_id=tenant_id)
    today = datetime.now(timezone.utc).date()
    today_cost = Decimal("0")
    month_cost = Decimal("0")
    total_by_run: dict[str, Decimal] = {}
    total_by_model: dict[str, Decimal] = {}
    total_by_workflow: dict[str, Decimal] = {}
    total_by_tool: dict[str, Decimal] = {}
    total_by_user: dict[str, Decimal] = {}
    for item in costs:
        amount = _total_cost(item)
        total_by_run[item.run_id] = total_by_run.get(item.run_id, Decimal("0")) + amount
        if item.model_name:
            total_by_model[item.model_name] = total_by_model.get(item.model_name, Decimal("0")) + amount
        if item.workflow_id:
            total_by_workflow[item.workflow_id] = total_by_workflow.get(item.workflow_id, Decimal("0")) + amount
        if item.step_id:
            total_by_tool[item.step_id] = total_by_tool.get(item.step_id, Decimal("0")) + amount
        if item.user_id:
            total_by_user[item.user_id] = total_by_user.get(item.user_id, Decimal("0")) + amount
        if item.created_at and item.created_at.date() == today:
            today_cost += amount
        if item.created_at and item.created_at.year == today.year and item.created_at.month == today.month:
            month_cost += amount
    violations = await list_budget_violations(session, tenant_id=tenant_id)
    budgets = await list_budget_policies(session, tenant_id=tenant_id, active_only=True)
    monthly_limit = sum((_decimal(item.hard_limit_amount or item.limit_amount) for item in budgets if item.period == "monthly"), Decimal("0"))
    return TenantCostSummary(
        tenant_id=tenant_id,
        today_cost=_money(today_cost),
        month_to_date_cost=_money(month_cost),
        budget_used_percent=_money((month_cost / monthly_limit * 100) if monthly_limit else 0),
        violation_count=len(violations),
        provider_breakdown=_breakdown(costs, "provider"),
        resource_type_breakdown=_breakdown(costs, "resource_type"),
        environment_breakdown=_breakdown(costs, "environment"),
        project_breakdown=_breakdown(costs, "project_id"),
        top_runs=_top_items(total_by_run, "run_id"),
        top_models=_top_items(total_by_model, "model_name"),
        top_workflows=_top_items(total_by_workflow, "workflow_id"),
        top_tools=_top_items(total_by_tool, "tool_id"),
        top_users=_top_items(total_by_user, "user_id"),
        failed_run_cost=sum((_total_cost(item) for item in costs if item.cost_type == "failed_retry"), Decimal("0")),
        replay_cost=sum((_total_cost(item) for item in costs if item.replay_attempt_id), Decimal("0")),
    )


def _total_cost(item: CostEvent) -> Decimal:
    return _decimal(item.estimated_cost) + _decimal(item.actual_cost)


def _breakdown(costs: list[CostEvent], attr: str) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for item in costs:
        key = str(getattr(item, attr, "") or "")
        if not key:
            continue
        values[key] = values.get(key, Decimal("0")) + _total_cost(item)
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
    return {
        "workflow_id": workflow_id,
        "tenant_id": tenant_id or "",
        "total_cost": _money(sum((_total_cost(c) for c in costs), Decimal("0"))),
    }


async def model_cost_ranking(session: AsyncSession, *, tenant_id: str | None = None) -> list[dict]:
    costs = await _list_cost_events(session, tenant_id=tenant_id)
    values: dict[str, Decimal] = {}
    for item in costs:
        if item.model_name:
            values[item.model_name] = values.get(item.model_name, Decimal("0")) + _total_cost(item)
    return _top_items(values, "model_name")
