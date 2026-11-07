from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import io
import re
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
from app.models.governance_event import GovernanceEvent
from app.models.incident import Incident
from app.observability.metrics import (
    FINOPS_BASE_CURRENCY_COST_TOTAL,
    FINOPS_BUDGET_VIOLATIONS_TOTAL,
    FINOPS_BURN_RATE_ALERTS_TOTAL,
    FINOPS_COST_USD_TOTAL,
    FINOPS_LEDGER_EXPORTS_TOTAL,
    FINOPS_TAX_TOTAL,
    FINOPS_TOKEN_LEASES_TOTAL,
    FINOPS_USAGE_EVENTS_TOTAL,
    INCIDENTS_TOTAL,
)
from app.schemas.agent import AgentRequest
from app.schemas.finops import (
    BudgetPolicyCreateRequest,
    BudgetPolicyUpdateRequest,
    CostAdjustmentCreateRequest,
    CostEventRecord,
    PolicyBurnRateForecastRecord,
    PricingCatalogCreateRequest,
    PricingCatalogUpdateRequest,
    TenantBurnRateForecastResponse,
    TenantCostExportResponse,
    TenantTokenLeasesResponse,
    TokenBudgetLeaseRecord,
    UsageEventCreateRequest,
)
from app.services.run_state import RunStateStore

FINOPS_CALCULATION_VERSION = "v1.5.8"
_ATTRIBUTION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ALLOCATION_TAG_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_VALID_ENVIRONMENTS = {"dev", "staging", "prod"}


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
    settled_cost: Decimal = Decimal("0")
    total_tax_amount: Decimal = Decimal("0")
    total_billed_base_cost: Decimal = Decimal("0")
    budget_policy_id: str = ""
    budget_remaining: Decimal = Decimal("0")
    currency_breakdown: dict[str, Decimal] = field(default_factory=dict)
    provider_breakdown: dict[str, Decimal] = field(default_factory=dict)
    model_breakdown: dict[str, Decimal] = field(default_factory=dict)
    tool_breakdown: dict[str, Decimal] = field(default_factory=dict)
    mcp_server_breakdown: dict[str, Decimal] = field(default_factory=dict)
    step_breakdown: dict[str, Decimal] = field(default_factory=dict)
    cost_type_breakdown: dict[str, Decimal] = field(default_factory=dict)
    resource_breakdown: dict[str, Decimal] = field(default_factory=dict)
    breakdown: dict[str, Decimal] = field(default_factory=dict)
    replay_cost: Decimal = Decimal("0")
    resume_incremental_cost: Decimal = Decimal("0")
    failed_retry_cost: Decimal = Decimal("0")
    cache_savings: Decimal = Decimal("0")
    batch_savings: Decimal = Decimal("0")
    token_lease_status: str = ""
    token_lease_id: str = ""
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
    period_start: datetime | None = None
    period_end: datetime | None = None
    timezone: str = "UTC"
    base_currency: str = "USD"
    total_tax_amount: Decimal = Decimal("0")
    total_billed_base_cost: Decimal = Decimal("0")
    adjustment_total: Decimal = Decimal("0")
    reconciled_month_to_date_cost: Decimal = Decimal("0")
    hourly_burn_rate: Decimal = Decimal("0")
    daily_burn_rate: Decimal = Decimal("0")
    projected_month_end_cost: Decimal = Decimal("0")
    burn_status: str = "normal"
    burn_rate_forecasts: list[PolicyBurnRateForecastRecord] = field(default_factory=list)
    active_lease_count: int = 0
    active_leased_tokens: int = 0
    active_leased_base_cost: Decimal = Decimal("0")
    currency_breakdown: dict[str, Decimal] = field(default_factory=dict)
    cost_center_breakdown: dict[str, Decimal] = field(default_factory=dict)
    provider_breakdown: dict[str, Decimal] = field(default_factory=dict)
    resource_type_breakdown: dict[str, Decimal] = field(default_factory=dict)
    environment_breakdown: dict[str, Decimal] = field(default_factory=dict)
    project_breakdown: dict[str, Decimal] = field(default_factory=dict)
    top_runs: list[dict] = field(default_factory=list)
    top_models: list[dict] = field(default_factory=list)
    top_workflows: list[dict] = field(default_factory=list)
    top_tools: list[dict] = field(default_factory=list)
    top_users: list[dict] = field(default_factory=list)
    mcp_server_breakdown: dict[str, Decimal] = field(default_factory=dict)
    top_mcp_servers: list[dict] = field(default_factory=list)
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
    is_settled = getattr(item, "is_settled", True)
    pre_tax_base = getattr(item, "pre_tax_base_cost", None)
    if pre_tax_base is None:
        pre_tax_base = _pre_tax_base_cost(item)
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
        "pre_tax_base_cost": pre_tax_base,
        "base_currency_cost": item.base_currency_cost,
        "fx_rate": item.fx_rate,
        "fx_rate_source": item.fx_rate_source,
        "is_settled": bool(is_settled),
        "billing_period_start": item.billing_period_start,
        "billing_period_end": item.billing_period_end,
        "provider_invoice_id": item.provider_invoice_id,
        "calculation_method": item.calculation_method,
        "calculation_version": item.calculation_version,
        "allocation_tags": item.allocation_tags,
        "metadata": item.metadata_payload,
        "mcp_server_id": str((item.allocation_tags or {}).get("mcp_server_id") or (item.metadata_payload or {}).get("mcp_server_id") or ""),
        "tool_id": str((item.allocation_tags or {}).get("tool_id") or (item.metadata_payload or {}).get("tool_id") or ""),
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


def _token_estimate(text: str) -> int:
    return max(1, len(text.split()) * 4)


def _resolve_run_allocation(route: dict, meta: dict) -> tuple[dict[str, object], list[str]]:
    raw_alloc = route.get("allocation")
    if isinstance(raw_alloc, dict):
        warnings = list(route.get("attribution_warnings") or [])
        return raw_alloc, warnings
    return extract_allocation(meta)


_TOOL_RISK_ORDER = {"": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _mcp_tool_attribution(request: AgentRequest, plan: ExecutionPlan | None) -> dict[str, object]:
    """Resolve per-tool / per-MCP-server attribution for the step_2_tools usage event (v1.5.7)."""
    route = dict((plan.model_route if plan is not None else {}) or {})
    mcp_route = dict(route.get("mcp_route") or {})
    selected = list((plan.selected_tools if plan is not None else []) or [])
    tool_names = [str(item.get("name")) for item in selected if item.get("name")]
    if not tool_names:
        tool_names = list(request.allowed_tools or ["summarize"])
    tool_mcp_map = {str(k): str(v) for k, v in dict(mcp_route.get("tool_mcp_map") or {}).items()}
    if not tool_mcp_map:
        gateway_map = dict(((request.metadata or {}).get("mcp_gateway") or {}).get("tool_mcp_map") or {})
        tool_mcp_map = {str(k): str(v) for k, v in gateway_map.items()}
    for name in tool_names:
        tool_mcp_map.setdefault(name, "mcp-core-local")
    tool_mcp_map = {name: tool_mcp_map[name] for name in tool_names}
    mcp_servers = list(dict.fromkeys(tool_mcp_map.values()))
    primary = str(mcp_route.get("primary_mcp_server_id") or (mcp_servers[0] if mcp_servers else "mcp-core-local"))
    risk = ""
    for item in selected:
        level = str(item.get("risk_level") or "")
        if _TOOL_RISK_ORDER.get(level, 0) > _TOOL_RISK_ORDER.get(risk, 0):
            risk = level
    primary_tool = tool_names[0] if tool_names else "summarize"
    return {
        "mcp_server_id": primary,
        "mcp_servers": mcp_servers,
        "tool_mcp_map": tool_mcp_map,
        "tools": tool_names,
        "tool_id": primary_tool[:128],
        "tool_name": primary_tool[:128],
        "tool_provider": primary[:128],
        "tool_risk_level": (risk or "low")[:32],
    }


def _mcp_usage_fields(attr: dict[str, object]) -> dict[str, object]:
    return {
        "tool_id": attr["tool_id"],
        "tool_name": attr["tool_name"],
        "tool_provider": attr["tool_provider"],
        "mcp_server_id": str(attr["mcp_server_id"])[:128],
        "tool_risk_level": attr["tool_risk_level"],
    }


def _mcp_usage_metadata(attr: dict[str, object]) -> dict[str, object]:
    return {
        "mcp_server_id": attr["mcp_server_id"],
        "mcp_servers": list(attr["mcp_servers"]),  # type: ignore[arg-type]
        "tool_mcp_map": dict(attr["tool_mcp_map"]),  # type: ignore[arg-type]
        "tools": list(attr["tools"]),  # type: ignore[arg-type]
        "tool_id": attr["tool_id"],
    }


async def record_submit_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    result: object | None = None,
    state_store: RunStateStore | None = None,
) -> BudgetEnforcementOutcome:
    if run.status == "denied":
        return BudgetEnforcementOutcome(decision="allow")
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
    alloc, _ = _resolve_run_allocation(route, meta)
    alloc_cost_center = str(alloc.get("cost_center") or "")
    alloc_org_id = str(alloc.get("org_id") or "")
    alloc_env = str(alloc.get("environment") or "dev")
    explicit_env = str(alloc.get("explicit_environment") or "")
    custom_tags = dict(alloc.get("allocation_tags") or {})
    alloc_meta_extra: dict[str, object] = {}
    if explicit_env:
        alloc_meta_extra["explicit_environment"] = explicit_env
    if custom_tags:
        alloc_meta_extra["allocation_tags"] = custom_tags

    tool_count = max(1, len(request.allowed_tools or ["summarize"]))
    mcp_attr = _mcp_tool_attribution(request, plan)
    input_tokens = _token_estimate(request.prompt + " " + request.goal)
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
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
            metadata={"source": "submit", "pricing_mode": routed_pricing_mode, **alloc_meta_extra},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
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
            **_mcp_usage_fields(mcp_attr),
            metadata={**_mcp_usage_metadata(mcp_attr), "requested_tools": request.allowed_tools, **alloc_meta_extra},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
            user_id=run.user_id,
            run_id=run.run_id,
            trace_id=trace_id,
            workflow_id=workflow_id,
            step_id="step_3_replay_bundle",
            resource_type="storage",
            usage_unit="artifact_mb",
            quantity=Decimal("0.01"),
            phase="estimated",
            metadata={"source": "replay_bundle_placeholder", **alloc_meta_extra},
        ),
    ]
    outcome = await _record_events_and_budget(
        session,
        run=run,
        plan=plan,
        result=result,
        events=events,
        enforce_active_guardrails=True,
    )
    if run.status == "denied" or outcome.decision == "fail_closed":
        await release_run_budget_lease(
            session,
            state_store,
            run_id=run.run_id,
            tenant_id=run.tenant_id,
            reason=outcome.reason or run.decision_reason or "finops_fail_closed",
        )
    elif run.status in {"queued", "awaiting_approval"}:
        await acquire_run_budget_lease(
            session,
            state_store,
            run=run,
            plan=plan,
        )
    await session.commit()
    return outcome


async def record_actual_usage(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    is_failed_retry: bool = False,
    state_store: RunStateStore | None = None,
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
    alloc, _ = _resolve_run_allocation(route, meta)
    alloc_cost_center = str(alloc.get("cost_center") or "")
    alloc_org_id = str(alloc.get("org_id") or "")
    alloc_env = str(alloc.get("environment") or "dev")
    explicit_env = str(alloc.get("explicit_environment") or "")
    custom_tags = dict(alloc.get("allocation_tags") or {})

    usage_status = "failed" if is_failed_retry else "success"
    cost_meta_base: dict[str, object] = {"source": "worker"}
    if is_failed_retry:
        cost_meta_base["cost_type"] = "failed_retry"
    if explicit_env:
        cost_meta_base["explicit_environment"] = explicit_env
    if custom_tags:
        cost_meta_base["allocation_tags"] = custom_tags
    transcript = run.evaluation_summary.get("execution_event", {}).get("payload", {}).get("tool_transcript", [])
    output_tokens = _token_estimate(run.execution_output or "worker_attempt")
    mcp_attr = _mcp_tool_attribution(request, plan)
    events = [
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
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
            **_mcp_usage_fields(mcp_attr),
            metadata={**cost_meta_base, **_mcp_usage_metadata(mcp_attr), "completed_tools": len(transcript)},
        ),
        UsageEventCreateRequest(
            tenant_id=run.tenant_id,
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
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
            org_id=alloc_org_id,
            project_id=routed_project_id,
            cost_center=alloc_cost_center,
            environment=alloc_env,
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
    if run.status in {"completed", "failed", "review_required", "rejected"}:
        await settle_run_budget_lease(session, state_store, run=run, plan=plan)
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


def _resolve_period_bounds(
    policy: BudgetPolicy,
    now: datetime,
) -> tuple[datetime, datetime, Decimal] | None:
    if policy.scope == "run" or policy.period == "per_run":
        return None
    now_utc = _ensure_utc(now) or datetime.now(timezone.utc)
    p_start = _ensure_utc(policy.period_start)
    p_end = _ensure_utc(policy.period_end)
    if p_start is not None and p_end is not None and p_end > p_start:
        total_hours = Decimal(str(max(1.0, (p_end - p_start).total_seconds() / 3600.0)))
        return p_start, p_end, total_hours

    if policy.period == "daily":
        start = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
    elif policy.period == "weekly":
        monday = (now_utc - timedelta(days=now_utc.weekday())).date()
        start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        end = start + timedelta(days=7)
    elif policy.period == "quarterly":
        q_month = ((now_utc.month - 1) // 3) * 3 + 1
        start = datetime(now_utc.year, q_month, 1, tzinfo=timezone.utc)
        end = (
            datetime(now_utc.year + 1, 1, 1, tzinfo=timezone.utc)
            if q_month == 10
            else datetime(now_utc.year, q_month + 3, 1, tzinfo=timezone.utc)
        )
    elif policy.period == "yearly":
        start = datetime(now_utc.year, 1, 1, tzinfo=timezone.utc)
        end = datetime(now_utc.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        start = datetime(now_utc.year, now_utc.month, 1, tzinfo=timezone.utc)
        end = (
            datetime(now_utc.year + 1, 1, 1, tzinfo=timezone.utc)
            if now_utc.month == 12
            else datetime(now_utc.year, now_utc.month + 1, 1, tzinfo=timezone.utc)
        )

    if p_start is not None and p_start > start:
        start = p_start
    if p_end is not None and p_end < end and p_end > start:
        end = p_end
    total_hours = Decimal(str(max(1.0, (end - start).total_seconds() / 3600.0)))
    return start, end, total_hours


def _matching_costs_for_policy(
    tenant_costs: list[CostEvent],
    policy: BudgetPolicy,
    now: datetime,
    run_id: str = "",
    bounds: tuple[datetime, datetime] | None = None,
) -> list[CostEvent]:
    matching_costs: list[CostEvent] = []
    for item in tenant_costs:
        if item.cost_type == "adjustment":
            continue
        if bounds is not None:
            dt = _ensure_utc(item.created_at) or now
            if dt < bounds[0] or dt > bounds[1] or dt > now:
                continue
        else:
            if not _cost_in_period(item, policy, now, run_id):
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
            target_tool = policy.tool_id or policy.scope_id
            if target_tool and target_tool not in _cost_tools(item):
                continue
        elif policy.scope == "mcp_server":
            target_server = policy.scope_id
            if target_server and target_server not in _cost_mcp_servers(item):
                continue
            if not target_server and not _cost_mcp_servers(item):
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
    return matching_costs


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
    settings = get_settings()
    base_currency = settings.finops_base_currency
    tenant_costs = await _list_cost_events(session, tenant_id=run.tenant_id)
    run_costs = [c for c in tenant_costs if c.run_id == run.run_id and c.cost_type != "adjustment"]
    settled_run_costs = _settled_cost_events(run_costs)
    run_estimated = _money(sum(_cost_for_basis(c, "estimated") for c in settled_run_costs))
    workflow_id = (
        plan.workflow_id
        if plan is not None
        else (run_costs[0].workflow_id if run_costs else run.task_type)
    )
    allowed_tools = list((run.request_payload or {}).get("allowed_tools") or [])
    run_mcp_servers = set(
        str(item)
        for item in (
            ((plan.model_route or {}).get("mcp_route") or {}).get("mcp_servers")
            if plan is not None
            else (((run.request_payload or {}).get("metadata") or {}).get("mcp_gateway") or {}).get("mcp_servers")
        )
        or []
    )
    for item in run_costs:
        run_mcp_servers.update(_cost_mcp_servers(item))

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
        if policy.scope == "mcp_server":
            target_server = policy.scope_id
            if target_server and target_server not in run_mcp_servers:
                continue

        matching_costs = _matching_costs_for_policy(tenant_costs, policy, now, run_id=run.run_id)
        settled_matching = _settled_cost_events(matching_costs)
        spent_before = _money(
            sum(_cost_for_basis(c, policy.budget_basis) for c in settled_matching if c.run_id != run.run_id)
        )
        current_run_amount = _money(
            sum(_cost_for_basis(c, policy.budget_basis) for c in settled_matching if c.run_id == run.run_id)
        )
        total_amount = _money(spent_before + current_run_amount)

        policy_curr = (policy.currency or base_currency).strip().upper()
        limit_fx_rate = Decimal("1")
        if policy_curr != base_currency:
            limit_fx_rate = settings.finops_fx_rates.get(policy_curr, Decimal("1"))

        hard_limit = _money(_money(policy.hard_limit_amount) * limit_fx_rate)
        soft_limit = _money(_money(policy.soft_limit_amount) * limit_fx_rate)
        if hard_limit == Decimal("0") and soft_limit == Decimal("0") and policy.limit_amount:
            hard_limit = _money(_money(policy.limit_amount) * limit_fx_rate)

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
                currency=base_currency,
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
                    "policy_currency": policy_curr,
                    "limit_fx_rate": _decimal_json(limit_fx_rate),
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
                f"{format_decimal_plain(total_amount)} > {format_decimal_plain(limit_used)} {base_currency}."
            )

    forecast_resp = _compute_burn_rate_forecasts_from_data(
        policies,
        tenant_costs,
        tenant_id=run.tenant_id,
        now=now,
        record_metrics=True,
    )
    if forecast_resp.burn_status in {"elevated_burn", "critical_burn"}:
        if "finops_burn_rate_warning" not in outcome.policy_flags:
            outcome.policy_flags.append("finops_burn_rate_warning")

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
            "routing_reason": f"{FINOPS_CALCULATION_VERSION.upper()} FinOps budget soft-limit degraded model route to {tier} tier.",
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
        await release_run_budget_lease(
            session,
            None,
            run_id=run.run_id,
            tenant_id=run.tenant_id,
            reason=reason,
        )

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


def _total_cost(item: CostEvent) -> Decimal:
    return _decimal(item.estimated_cost) + _decimal(item.actual_cost)


def _breakdown(costs: list[CostEvent], attr: str) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for item in costs:
        key = str(getattr(item, attr, "") or "")
        if not key:
            continue
        values[key] = values.get(key, Decimal("0")) + _pre_tax_base_cost(item)
    return {key: _money(value) for key, value in values.items()}


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


def _cost_matches_mcp_server(item: CostEvent, mcp_server_id: str | None) -> bool:
    if not mcp_server_id:
        return True
    return mcp_server_id in _cost_mcp_servers(item)


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
