from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.agent_run import AgentRun
from app.models.approval_event import ApprovalEvent
from app.models.checkpoint import ReplayAttempt, RunCheckpoint
from app.models.control_plane import ExecutionPlan
from app.models.finops import (
    BudgetPolicy,
    BudgetViolation,
    CostEvent,
    PricingCatalog,
    PricingChangeLog,
    UsageEvent,
)
from app.models.incident import Incident
from app.observability.metrics import (
    FINOPS_BUDGET_VIOLATIONS_TOTAL,
    FINOPS_COST_USD_TOTAL,
    FINOPS_USAGE_EVENTS_TOTAL,
    INCIDENTS_TOTAL,
)
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
    cache_savings: Decimal = Decimal("0")
    batch_savings: Decimal = Decimal("0")
    usage_events: list[UsageEvent] = field(default_factory=list)
    cost_events: list[CostEvent] = field(default_factory=list)


@dataclass
class BudgetEnforcementOutcome:
    decision: str = "allow"
    triggered_actions: list[str] = field(default_factory=list)
    policy_flags: list[str] = field(default_factory=list)
    fallback_model_tier: str = ""
    approval_required: bool = False
    matched_budget_id: str = ""
    reason: str = ""
    violations: list[BudgetViolation] = field(default_factory=list)


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


def format_decimal_plain(value: object) -> str:
    if value is None or value == "":
        return ""
    d = Decimal(str(value))
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _ensure_utc(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                parsed_d = date.fromisoformat(value)
                return datetime(parsed_d.year, parsed_d.month, parsed_d.day, tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


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
        "unit_price_snapshot": item.unit_price,
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
        "scope_type": item.scope,
        "scope_id": item.scope_id,
        "project_id": item.project_id,
        "environment": item.environment,
        "policy_name": item.policy_name,
        "name": item.policy_name,
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

    for item in candidates:
        if item.token_type not in {usage_token_type, "not_applicable"}:
            continue
        if item.model_name not in {usage.model_name, ""}:
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


async def _record_cost_for_usage(session: AsyncSession, usage: UsageEvent) -> CostEvent:
    pricing = await _lookup_pricing(session, usage)
    free_quota_applied = Decimal("0")
    raw_quantity = _quantity(usage.quantity)
    if pricing is None:
        await _record_missing_pricing_violation(session, usage)
        pricing_id = ""
        currency = "USD"
        unit_price = Decimal("0")
        quantity = raw_quantity
        gross_cost = Decimal("0")
        discount_rate = Decimal("0")
        discount_amount = Decimal("0")
        multiplier = Decimal("1")
        net_cost = Decimal("0")
        snapshot: dict[str, object] = {}
    else:
        pricing_id = pricing.pricing_id
        if not usage.pricing_id:
            usage.pricing_id = pricing_id
        currency = pricing.currency
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
        net_cost = _money((gross_cost - discount_amount) * multiplier)
        snapshot = _pricing_snapshot(pricing)

    is_actual = usage.phase in {"actual", "manual"}
    amount = net_cost
    is_failed_retry = (
        (usage.metadata_payload or {}).get("cost_type") == "failed_retry"
        or (is_actual and usage.status == "failed")
    )
    cost_type = "failed_retry" if is_failed_retry else ("actual" if is_actual else "estimated")
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
        tax_amount=Decimal("0"),
        estimated_cost=Decimal("0") if is_actual else amount,
        actual_cost=amount if is_actual else Decimal("0"),
        currency=currency,
        base_currency="USD",
        base_currency_cost=amount,
        fx_rate=Decimal("1"),
        fx_rate_source="same_currency",
        calculation_method=usage.phase,
        calculation_version="v1.5.4",
        allocation_tags={"tenant_id": usage.tenant_id, "workflow_id": usage.workflow_id},
        metadata_payload={
            "usage_unit": usage.usage_unit,
            "usage_status": usage.status,
            "raw_quantity": _decimal_json(raw_quantity),
            "billable_quantity": _decimal_json(quantity),
            "free_quota_applied_quantity": _decimal_json(free_quota_applied),
            "pricing_mode": str(snapshot.get("pricing_mode") or (usage.metadata_payload or {}).get("pricing_mode") or "standard"),
            "token_type": str(snapshot.get("token_type") or _token_type_for_usage(usage)),
        },
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
    ).inc(float(amount))
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


async def record_submit_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    result: object | None = None,
) -> BudgetEnforcementOutcome:
    request = AgentRequest.model_validate(run.request_payload)
    workflow_id = plan.workflow_id if plan is not None else request.task_type
    trace_id = (plan.trace_id if plan is not None else request.metadata.get("trace_id", "")) or ""
    route = dict((plan.model_route if plan is not None else {}) or {})
    meta = dict(request.metadata or {})
    routed_provider = str(route.get("provider") or meta.get("provider") or "internal").strip() or "internal"
    routed_model = (
        str(route.get("model") or meta.get("model_name") or "deterministic-control-plane").strip()
        or "deterministic-control-plane"
    )
    routed_pricing_mode = str(route.get("pricing_mode") or meta.get("pricing_mode") or "standard").strip() or "standard"
    routed_service_tier = str(route.get("service_tier") or meta.get("service_tier") or "standard").strip() or "standard"
    routed_project_id = str(route.get("project_id") or meta.get("project_id") or "").strip()
    tool_count = max(1, len(request.allowed_tools or ["summarize"]))
    input_tokens = _token_estimate(request.prompt + " " + request.goal)
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            project_id=routed_project_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_1_plan",
            provider=routed_provider,
            model_name=routed_model,
            service_tier=routed_service_tier,
            pricing_mode=routed_pricing_mode,
            resource_type="model",
            resource_subtype="chat_completion",
            usage_unit="input_tokens",
            quantity=input_tokens,
            input_tokens=input_tokens,
            phase="estimated",
            metadata={"source": "submit", "pricing_mode": routed_pricing_mode},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            project_id=routed_project_id,
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
            project_id=routed_project_id,
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
    return await _record_events_and_budget(
        session,
        run=run,
        plan=plan,
        result=result,
        events=events,
        enforce_active_guardrails=True,
    )


async def record_actual_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    is_failed_retry: bool = False,
) -> None:
    request = AgentRequest.model_validate(run.request_payload)
    workflow_id = plan.workflow_id if plan is not None else request.task_type
    trace_id = (plan.trace_id if plan is not None else request.metadata.get("trace_id", "")) or ""
    route = dict((plan.model_route if plan is not None else {}) or {})
    meta = dict(request.metadata or {})
    routed_provider = str(route.get("provider") or meta.get("provider") or "internal").strip() or "internal"
    routed_model = (
        str(route.get("model") or meta.get("model_name") or "deterministic-control-plane").strip()
        or "deterministic-control-plane"
    )
    routed_pricing_mode = str(route.get("pricing_mode") or meta.get("pricing_mode") or "standard").strip() or "standard"
    routed_service_tier = str(route.get("service_tier") or meta.get("service_tier") or "standard").strip() or "standard"
    routed_project_id = str(route.get("project_id") or meta.get("project_id") or "").strip()
    usage_status = "failed" if is_failed_retry else "success"
    cost_meta_base: dict[str, object] = {"source": "worker"}
    if is_failed_retry:
        cost_meta_base["cost_type"] = "failed_retry"
    transcript = run.evaluation_summary.get("execution_event", {}).get("payload", {}).get("tool_transcript", [])
    output_tokens = _token_estimate(run.execution_output or "worker_attempt")
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            project_id=routed_project_id,
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
            status=usage_status,
            metadata={**cost_meta_base, "completed_tools": len(transcript)},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            project_id=routed_project_id,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_3_evaluation",
            provider=routed_provider,
            model_name=routed_model,
            service_tier=routed_service_tier,
            pricing_mode=routed_pricing_mode,
            resource_type="model",
            resource_subtype="chat_completion",
            usage_unit="output_tokens",
            quantity=output_tokens,
            output_tokens=output_tokens,
            phase="actual",
            status=usage_status,
            metadata={**cost_meta_base, "pricing_mode": routed_pricing_mode},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            project_id=routed_project_id,
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
            status=usage_status,
            metadata=dict(cost_meta_base),
        ),
    ]
    outcome = await _record_events_and_budget(session, run=run, plan=plan, events=events)
    if outcome.decision == "fail_closed":
        flags = list(run.policy_flags or [])
        if "finops_runtime_budget_exceeded" not in flags:
            flags.append("finops_runtime_budget_exceeded")
            run.policy_flags = flags
        session.add(
            Incident(
                run_id=run.run_id,
                severity="high",
                code="finops_runtime_budget_exceeded",
                detail=outcome.reason or "Runtime actual cost exceeded hard budget limit.",
            )
        )
        INCIDENTS_TOTAL.labels(severity="high", code="finops_runtime_budget_exceeded").inc()
        await session.commit()


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
    plan: ExecutionPlan | None = None,
    result: object | None = None,
    enforce_active_guardrails: bool = False,
) -> BudgetEnforcementOutcome:
    for event in events:
        usage = _usage_from_payload(event)
        session.add(usage)
        await session.flush()
        await _record_cost_for_usage(session, usage)
    outcome = await _check_budgets(session, run=run, plan=plan)
    if enforce_active_guardrails and get_settings().finops_budget_enforcement_enabled:
        await apply_budget_enforcement_to_run(
            session,
            run=run,
            plan=plan,
            result=result,
            outcome=outcome,
        )
    await session.commit()
    return outcome


async def create_budget_policy(
    session: AsyncSession,
    payload: BudgetPolicyCreateRequest,
    *,
    supersede_existing: bool = False,
) -> BudgetPolicy:
    if supersede_existing:
        existing_rows = list(
            (
                await session.scalars(
                    select(BudgetPolicy).where(
                        BudgetPolicy.tenant_id == (payload.tenant_id or "default"),
                        BudgetPolicy.scope == payload.scope,
                        BudgetPolicy.scope_id == payload.scope_id,
                        BudgetPolicy.period == str(_value(payload.period)),
                        BudgetPolicy.is_active.is_(True),
                    )
                )
            ).all()
        )
        now = datetime.now(timezone.utc)
        for old_row in existing_rows:
            old_row.is_active = False
            old_row.effective_to = now
    has_hard = payload.hard_limit_amount is not None or payload.limit_amount is not None
    if has_hard:
        hard_limit = payload.hard_limit_amount if payload.hard_limit_amount is not None else payload.limit_amount
        limit_amount = payload.limit_amount if payload.limit_amount is not None else hard_limit
        soft_limit = payload.soft_limit_amount if payload.soft_limit_amount is not None else Decimal("0")
    else:
        soft_limit = payload.soft_limit_amount or Decimal("0")
        hard_limit = Decimal("0")
        limit_amount = soft_limit

    if "actions" in payload.model_fields_set and "actions_on_hard_limit" not in payload.model_fields_set:
        hard_actions = [str(_value(item)) for item in payload.actions]
    else:
        hard_actions = [str(_value(item)) for item in payload.actions_on_hard_limit]

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
        actions_on_hard_limit=hard_actions,
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


async def get_budget_policy(session: AsyncSession, budget_id: str) -> BudgetPolicy | None:
    return await session.scalar(select(BudgetPolicy).where(BudgetPolicy.budget_id == budget_id))


async def disable_budget_policy(
    session: AsyncSession,
    budget_id: str,
    *,
    updated_by: str = "",
) -> BudgetPolicy | None:
    item = await get_budget_policy(session, budget_id)
    if item is None:
        return None
    item.is_active = False
    item.effective_to = datetime.now(timezone.utc)
    if updated_by:
        item.updated_by = updated_by
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
    resolution_status: str | None = None,
    limit: int = 100,
) -> list[BudgetViolation]:
    stmt = select(BudgetViolation).order_by(BudgetViolation.created_at.desc()).limit(limit)
    if tenant_id:
        stmt = stmt.where(BudgetViolation.tenant_id == tenant_id)
    if run_id:
        stmt = stmt.where(BudgetViolation.run_id == run_id)
    if resolution_status:
        stmt = stmt.where(BudgetViolation.resolution_status == resolution_status)
    return list((await session.scalars(stmt)).all())


async def get_budget_violation(session: AsyncSession, violation_id: str) -> BudgetViolation | None:
    return await session.scalar(select(BudgetViolation).where(BudgetViolation.violation_id == violation_id))


async def acknowledge_budget_violation(
    session: AsyncSession,
    violation_id: str,
    *,
    acknowledged_by: str = "",
    resolved_by: str = "",
    note: str = "",
    notes: str = "",
) -> BudgetViolation | None:
    item = await get_budget_violation(session, violation_id)
    if item is None:
        return None
    actor = acknowledged_by or resolved_by or "operator"
    final_note = note or notes
    item.resolution_status = "acknowledged"
    item.acknowledged_by = actor
    item.acknowledged_at = datetime.now(timezone.utc)
    if final_note:
        details = dict(item.details or {})
        details["acknowledge_note"] = final_note
        item.details = details
    await session.commit()
    await session.refresh(item)
    return item


async def resolve_budget_violation(
    session: AsyncSession,
    violation_id: str,
    *,
    resolved_by: str = "",
    resolution_status: str = "resolved",
    resolution_note: str = "",
    note: str = "",
    notes: str = "",
) -> BudgetViolation | None:
    item = await get_budget_violation(session, violation_id)
    if item is None:
        return None
    now = datetime.now(timezone.utc)
    actor = resolved_by or "admin"
    final_note = resolution_note or note or notes
    item.resolution_status = resolution_status if resolution_status in {"resolved", "ignored"} else "resolved"
    item.resolved_by = actor
    item.resolved_at = now
    if not item.acknowledged_by:
        item.acknowledged_by = actor
        item.acknowledged_at = now
    details = dict(item.details or {})
    if final_note:
        details["resolution_note"] = final_note
    item.details = details
    await session.commit()
    await session.refresh(item)
    return item


def _cost_for_basis(item: CostEvent, basis: str) -> Decimal:
    if basis == "gross":
        return _decimal(item.gross_cost)
    if basis == "net":
        return _decimal(item.net_cost)
    if basis == "base_currency":
        return _decimal(item.base_currency_cost)
    if basis == "estimated":
        return _decimal(item.estimated_cost) or _total_cost(item)
    return _total_cost(item)


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


async def evaluate_budget_policies(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None = None,
) -> BudgetEnforcementOutcome:
    outcome = BudgetEnforcementOutcome()
    policies = await list_budget_policies(session, tenant_id=run.tenant_id, active_only=True)
    if not policies:
        return outcome

    now = datetime.now(timezone.utc)
    tenant_costs = await _list_cost_events(session, tenant_id=run.tenant_id)
    run_costs = [c for c in tenant_costs if c.run_id == run.run_id]
    run_estimated = _money(sum(_decimal(c.estimated_cost) for c in run_costs))
    workflow_id = (
        plan.workflow_id
        if plan is not None
        else (run_costs[0].workflow_id if run_costs else run.task_type)
    )
    allowed_tools = list((run.request_payload or {}).get("allowed_tools") or [])

    decision_priority = {
        "allow": 0,
        "warn": 1,
        "degrade_model": 2,
        "dry_run_only": 3,
        "require_approval": 4,
        "fail_closed": 5,
    }

    for policy in policies:
        eff_from = _ensure_utc(policy.effective_from)
        eff_to = _ensure_utc(policy.effective_to)
        if eff_from is not None and eff_from > now:
            continue
        if eff_to is not None and eff_to < now:
            continue

        if policy.scope == "run" and policy.scope_id and policy.scope_id not in {"default", "*", run.tenant_id, run.run_id}:
            continue
        if policy.scope == "tenant" and policy.scope_id and policy.scope_id != run.tenant_id:
            continue
        if policy.scope == "user":
            target_user = policy.scope_id
            if target_user and target_user != run.user_id:
                continue
        if policy.scope == "project":
            target_proj = policy.project_id or policy.scope_id
            run_proj = str(
                (plan.model_route or {}).get("project_id")
                if plan is not None
                else ((run.request_payload or {}).get("metadata") or {}).get("project_id") or ""
            )
            if not run_proj and run_costs:
                run_proj = str(run_costs[0].project_id or "")
            if target_proj and target_proj != run_proj:
                continue
        if policy.scope == "model":
            target_model = policy.model_name or policy.scope_id
            run_model = str(
                (plan.model_route or {}).get("model")
                if plan is not None
                else ((run.request_payload or {}).get("metadata") or {}).get("model_name") or "deterministic-control-plane"
            )
            if target_model and target_model != run_model:
                continue
        if policy.scope == "workflow":
            target_wf = policy.workflow_id or policy.scope_id
            if target_wf and target_wf != workflow_id:
                continue
        if policy.scope == "tool":
            target_tool = policy.tool_id or policy.scope_id
            if target_tool and target_tool not in allowed_tools:
                continue

        matching_costs: list[CostEvent] = []
        for item in tenant_costs:
            if not _cost_in_period(item, policy, now, run.run_id):
                continue
            if policy.scope == "user":
                target_user = policy.scope_id
                if target_user and item.user_id != target_user:
                    continue
            elif policy.scope == "workflow":
                target_wf = policy.workflow_id or policy.scope_id
                if target_wf and item.workflow_id != target_wf:
                    continue
            elif policy.scope == "project":
                target_proj = policy.project_id or policy.scope_id
                if target_proj and item.project_id != target_proj:
                    continue
            elif policy.scope == "model":
                target_model = policy.model_name or policy.scope_id
                if target_model and item.model_name != target_model:
                    continue
            elif policy.scope == "tool":
                if item.resource_type != "tool":
                    continue
            if policy.provider and item.provider != policy.provider:
                continue
            if policy.model_name and item.model_name and item.model_name != policy.model_name:
                continue
            if policy.workflow_id and item.workflow_id and item.workflow_id != policy.workflow_id:
                continue
            if policy.resource_type and policy.resource_type != "all" and item.resource_type != policy.resource_type:
                continue
            if not policy.include_replay_runs and item.replay_attempt_id:
                continue
            if not policy.include_failed_runs and item.cost_type == "failed_retry":
                continue
            if not policy.include_dev_environment and (item.metadata_payload or {}).get("explicit_environment") == "dev":
                continue
            matching_costs.append(item)

        spent_before = _money(
            sum(_cost_for_basis(c, policy.budget_basis) for c in matching_costs if c.run_id != run.run_id)
        )
        current_run_amount = _money(
            sum(_cost_for_basis(c, policy.budget_basis) for c in matching_costs if c.run_id == run.run_id)
        )
        total_amount = _money(spent_before + current_run_amount)

        hard_limit = _money(policy.hard_limit_amount)
        soft_limit = _money(policy.soft_limit_amount)
        if hard_limit == Decimal("0") and soft_limit == Decimal("0") and policy.limit_amount:
            hard_limit = _money(policy.limit_amount)

        threshold_type = ""
        limit_used = Decimal("0")
        actions: list[str] = []
        decision = "allow"
        action_taken = "record_violation"
        severity = "medium"

        if hard_limit > Decimal("0") and total_amount > hard_limit:
            threshold_type = "hard"
            limit_used = hard_limit
            actions = [str(a) for a in (policy.actions_on_hard_limit or policy.actions or ["record_violation", "warn"])]
            if policy.approval_required and "require_approval" not in actions and "fail_closed" not in actions:
                actions.append("require_approval")
            if "fail_closed" in actions:
                decision = "fail_closed"
                action_taken = "fail_closed"
                severity = "high"
            elif "require_approval" in actions or policy.approval_required:
                decision = "require_approval"
                action_taken = "require_approval"
                severity = "high"
            elif "degrade_model" in actions:
                decision = "degrade_model"
                action_taken = "degrade_model"
                severity = "medium"
            elif "dry_run_only" in actions:
                decision = "dry_run_only"
                action_taken = "dry_run_only"
                severity = "medium"
            else:
                decision = "warn"
                action_taken = "record_violation"
                severity = "medium"
        elif soft_limit > Decimal("0") and total_amount > soft_limit:
            threshold_type = "soft"
            limit_used = soft_limit
            actions = [str(a) for a in (policy.actions_on_soft_limit or policy.actions or ["record_violation", "warn"])]
            if "degrade_model" in actions or policy.fallback_model_tier:
                decision = "degrade_model"
                action_taken = "degrade_model"
                severity = "medium"
            elif "require_approval" in actions:
                decision = "require_approval"
                action_taken = "require_approval"
                severity = "medium"
            else:
                decision = "warn"
                action_taken = "warn" if "warn" in actions else "record_violation"
                severity = "low"
        else:
            continue

        violation_type = (
            "run_budget_exceeded"
            if (policy.scope == "run" and threshold_type == "hard")
            else f"{policy.scope}_budget_{threshold_type}_limit_exceeded"
        )
        existing = await session.scalar(
            select(BudgetViolation).where(
                BudgetViolation.budget_id == policy.budget_id,
                BudgetViolation.run_id == run.run_id,
                BudgetViolation.threshold_type == threshold_type,
            )
        )
        if existing is None:
            remaining_before = max(Decimal("0"), _money(limit_used - spent_before))
            overage = _money(total_amount - limit_used)
            violation = BudgetViolation(
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
                workflow_id=workflow_id,
                provider=run_costs[0].provider if run_costs else "",
                model_name=run_costs[0].model_name if run_costs else "",
                resource_type=run_costs[0].resource_type if run_costs else "",
                violation_type=violation_type,
                severity=severity,
                threshold_type=threshold_type,
                estimated_amount=run_estimated,
                actual_amount=total_amount,
                limit_amount=limit_used,
                remaining_budget_before=remaining_before,
                overage_amount=overage,
                overage_percent=_money((overage / limit_used * 100) if limit_used else 0),
                currency=policy.currency,
                decision=decision,
                recommended_action=decision,
                action_taken=action_taken,
                details={
                    "actions": policy.actions,
                    "actions_on_soft_limit": policy.actions_on_soft_limit,
                    "actions_on_hard_limit": policy.actions_on_hard_limit,
                    "scope": policy.scope,
                    "period": policy.period,
                    "fallback_model_tier": policy.fallback_model_tier,
                },
            )
            session.add(violation)
            await session.flush()
            FINOPS_BUDGET_VIOLATIONS_TOTAL.labels(
                tenant_id=run.tenant_id,
                scope=policy.scope,
                threshold_type=threshold_type,
                decision=decision,
            ).inc()
            outcome.violations.append(violation)
        else:
            outcome.violations.append(existing)

        for act in actions:
            if act not in outcome.triggered_actions:
                outcome.triggered_actions.append(act)
        if decision == "degrade_model" or "degrade_model" in actions:
            outcome.fallback_model_tier = policy.fallback_model_tier or outcome.fallback_model_tier or "cheap"
            for flag in ("finops_model_degraded", "finops_budget_degraded_model"):
                if flag not in outcome.policy_flags:
                    outcome.policy_flags.append(flag)
        if decision == "dry_run_only" or "dry_run_only" in actions:
            if "finops_budget_dry_run_only" not in outcome.policy_flags:
                outcome.policy_flags.append("finops_budget_dry_run_only")
        if decision == "require_approval":
            outcome.approval_required = True
            if "finops_budget_requires_approval" not in outcome.policy_flags:
                outcome.policy_flags.append("finops_budget_requires_approval")
        if decision == "fail_closed":
            if "finops_budget_hard_limit_exceeded" not in outcome.policy_flags:
                outcome.policy_flags.append("finops_budget_hard_limit_exceeded")

        if decision_priority.get(decision, 0) > decision_priority.get(outcome.decision, 0):
            outcome.decision = decision
            outcome.matched_budget_id = policy.budget_id
            outcome.reason = (
                f"Budget policy {policy.policy_name} ({policy.scope}/{policy.period}) {threshold_type} limit exceeded: "
                f"{format_decimal_plain(total_amount)} > {format_decimal_plain(limit_used)} {policy.currency}."
            )

    return outcome


async def _check_budgets(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None = None,
) -> BudgetEnforcementOutcome:
    return await evaluate_budget_policies(session, run=run, plan=plan)


async def apply_budget_enforcement_to_run(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    result: object | None,
    outcome: BudgetEnforcementOutcome,
) -> None:
    if outcome.decision == "allow" and not outcome.policy_flags:
        return

    flags = list(run.policy_flags or [])
    for flag in outcome.policy_flags:
        if flag not in flags:
            flags.append(flag)
    run.policy_flags = flags
    if result is not None and hasattr(result, "response"):
        for flag in outcome.policy_flags:
            if flag not in result.response.policy_flags:
                result.response.policy_flags.append(flag)

    summary = dict(run.evaluation_summary or {})

    if "finops_model_degraded" in outcome.policy_flags and plan is not None:
        tier = outcome.fallback_model_tier or "cheap"
        old_route = dict(plan.model_route or {})
        provider_name = str(old_route.get("provider") or "internal")
        old_model = str(old_route.get("model") or "deterministic-control-plane")
        degraded_model = f"deterministic-{tier}" if provider_name == "internal" else f"{old_model}-{tier}"
        plan.model_route = {
            **old_route,
            "model": degraded_model,
            "model_tier": tier,
            "fallback_model_tier": tier,
            "degraded_from": old_model,
            "routing_reason": f"V1.5.4 FinOps budget soft-limit degraded model route to {tier} tier.",
        }
        exec_plan_dict = dict(summary.get("execution_plan") or {})
        if exec_plan_dict:
            exec_plan_dict["model_route"] = plan.model_route
            summary["execution_plan"] = exec_plan_dict

    if outcome.decision == "dry_run_only" or "finops_budget_dry_run_only" in outcome.policy_flags:
        req_payload = dict(run.request_payload or {})
        req_meta = dict(req_payload.get("metadata") or {})
        req_meta["forced_dry_run"] = True
        req_payload["metadata"] = req_meta
        run.request_payload = req_payload
        if plan is not None:
            pol_snap = dict(plan.policy_snapshot or {})
            pol_snap["forced_dry_run"] = True
            plan.policy_snapshot = pol_snap
            exec_plan_dict = dict(summary.get("execution_plan") or {})
            if exec_plan_dict:
                exec_plan_dict["policy_snapshot"] = pol_snap
                summary["execution_plan"] = exec_plan_dict

    if outcome.decision == "require_approval":
        reason = outcome.reason or "Budget policy requires human approval before execution."
        run.status = "awaiting_approval"
        run.decision_reason = reason
        run.requires_human_review = True
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().approval_ttl_seconds)
        existing_approval = await session.scalar(
            select(ApprovalEvent).where(
                ApprovalEvent.run_id == run.run_id,
                ApprovalEvent.status == "pending",
            )
        )
        if existing_approval is None:
            existing_approval = ApprovalEvent(
                run_id=run.run_id,
                status="pending",
                reviewer="",
                reason=reason,
                expires_at=expires_at,
                expired=False,
            )
            session.add(existing_approval)
            await session.flush()
        approval_id_str = f"appr_{existing_approval.id}"
        for violation in outcome.violations:
            if violation.decision == "require_approval":
                violation.approval_id = approval_id_str
        if plan is not None:
            gates = list(plan.approval_gates or [])
            budget_gate = {
                "gate_type": "finops_budget_gate",
                "status": "pending",
                "budget_id": outcome.matched_budget_id,
                "reason": reason,
            }
            if not any(g.get("gate_type") == "finops_budget_gate" for g in gates):
                gates.append(budget_gate)
            plan.approval_gates = gates
            plan.status = "requires_approval"
            steps = list(plan.steps or [])
            if not any(s.get("step_type") == "approval_gate" for s in steps):
                steps.insert(
                    1,
                    {
                        "step_id": "approval-gate",
                        "step_type": "approval_gate",
                        "status": "pending",
                        "gates": gates,
                    },
                )
            plan.steps = steps
            exec_plan_dict = dict(summary.get("execution_plan") or {})
            if exec_plan_dict:
                exec_plan_dict["approval_gates"] = gates
                exec_plan_dict["status"] = "requires_approval"
                exec_plan_dict["steps"] = steps
                summary["execution_plan"] = exec_plan_dict
        summary["run_state"] = {"status": "awaiting_approval"}
        summary["approval"] = {
            "status": "pending",
            "reviewer": "",
            "reason": reason,
            "expires_at": expires_at.isoformat(),
            "expired": False,
        }
        if result is not None and hasattr(result, "response"):
            result.response.status = "awaiting_approval"
            result.response.decision_reason = reason

    elif outcome.decision == "fail_closed":
        reason = outcome.reason or "Budget policy hard limit exceeded (fail_closed)."
        run.status = "denied"
        run.decision_reason = reason
        run.requires_human_review = True
        session.add(
            Incident(
                run_id=run.run_id,
                severity="high",
                code="finops_budget_exceeded",
                detail=reason,
            )
        )
        INCIDENTS_TOTAL.labels(severity="high", code="finops_budget_exceeded").inc()
        if plan is not None:
            plan.status = "blocked"
            exec_plan_dict = dict(summary.get("execution_plan") or {})
            if exec_plan_dict:
                exec_plan_dict["status"] = "blocked"
                summary["execution_plan"] = exec_plan_dict
        summary["run_state"] = {"status": "denied"}
        if result is not None and hasattr(result, "response"):
            result.response.status = "denied"
            result.response.decision_reason = reason

    run.evaluation_summary = summary
    run.result = {
        "response": {
            "run_id": run.run_id,
            "status": run.status,
            "decision_reason": run.decision_reason,
            "risk_score": run.risk_score,
            "policy_flags": run.policy_flags,
        },
        "execution_output": run.execution_output,
        "evaluation_summary": summary,
    }
    if result is not None and hasattr(result, "evaluation_summary"):
        result.evaluation_summary = summary


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
    if any(item.decision in {"fail_closed", "require_approval"} for item in violations):
        budget_status = "exceeded"
    elif violations:
        budget_status = "warning"
    else:
        budget_status = "within_budget"
    cost_delta = _money(actual - estimated)
    cache_savings, batch_savings = await _compute_savings_for_costs(session, costs)
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
        budget_policy_id=violations[0].budget_id if violations else "",
        budget_remaining=violations[0].remaining_budget_before if violations else Decimal("0"),
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
        cache_savings=cache_savings,
        batch_savings=batch_savings,
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
    monthly_limit = sum(
        (
            _decimal(item.hard_limit_amount or item.limit_amount or item.soft_limit_amount)
            for item in budgets
            if item.period == "monthly"
        ),
        Decimal("0"),
    )
    cache_savings, batch_savings = await _compute_savings_for_costs(session, costs)
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
        cache_savings=cache_savings,
        batch_savings=batch_savings,
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
