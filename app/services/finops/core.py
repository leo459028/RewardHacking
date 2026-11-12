"""Shared constants, default pricing, value/decimal helpers and summary dataclasses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

from app.models.finops import (
    BudgetViolation,
    CostEvent,
    UsageEvent,
)
from app.schemas.finops import (
    PolicyBurnRateForecastRecord,
)

FINOPS_CALCULATION_VERSION = "v1.6.1"


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
