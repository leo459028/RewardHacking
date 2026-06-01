from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PricingCatalogCreateRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    resource_type: str = Field(min_length=1, max_length=64)
    model_name: str = ""
    price_unit: str = Field(min_length=1, max_length=64)
    unit_price: float = Field(ge=0)
    currency: str = Field(default="USD", min_length=1, max_length=8)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PricingCatalogUpdateRequest(BaseModel):
    provider: str | None = None
    resource_type: str | None = None
    model_name: str | None = None
    price_unit: str | None = None
    unit_price: float | None = Field(default=None, ge=0)
    currency: str | None = None
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class PricingCatalogRecord(BaseModel):
    pricing_id: str
    provider: str
    resource_type: str
    model_name: str
    price_unit: str
    unit_price: float
    currency: str
    is_active: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class UsageEventCreateRequest(BaseModel):
    tenant_id: str = "default"
    user_id: str = ""
    run_id: str = ""
    trace_id: str = ""
    workflow_id: str = ""
    step_id: str = ""
    checkpoint_id: str = ""
    replay_attempt_id: str = ""
    provider: str = "internal"
    model_name: str = ""
    resource_type: str
    usage_unit: str
    quantity: float = Field(ge=0)
    phase: str = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)


class UsageEventRecord(BaseModel):
    usage_id: str
    tenant_id: str
    user_id: str
    run_id: str
    trace_id: str
    workflow_id: str
    step_id: str
    checkpoint_id: str
    replay_attempt_id: str
    provider: str
    model_name: str
    resource_type: str
    usage_unit: str
    quantity: float
    phase: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class CostEventRecord(BaseModel):
    cost_id: str
    usage_event_id: str
    pricing_catalog_id: str
    tenant_id: str
    run_id: str
    trace_id: str
    workflow_id: str
    resource_type: str
    model_name: str
    estimated_cost: float
    actual_cost: float
    currency: str
    allocation_tags: dict[str, Any] = Field(default_factory=dict)
    calculation_method: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class RunCostSummaryResponse(BaseModel):
    run_id: str
    tenant_id: str
    estimated_cost: float
    actual_cost: float
    cost_delta: float
    currency: str
    budget_status: str
    breakdown: dict[str, float] = Field(default_factory=dict)
    usage_events: list[UsageEventRecord] = Field(default_factory=list)
    cost_events: list[CostEventRecord] = Field(default_factory=list)


class TenantCostSummaryResponse(BaseModel):
    tenant_id: str
    today_cost: float
    month_to_date_cost: float
    budget_used_percent: float
    violation_count: int
    top_runs: list[dict[str, Any]] = Field(default_factory=list)
    top_models: list[dict[str, Any]] = Field(default_factory=list)
    top_workflows: list[dict[str, Any]] = Field(default_factory=list)


class BudgetPolicyCreateRequest(BaseModel):
    tenant_id: str = "default"
    scope: str = "tenant"
    policy_name: str = Field(min_length=1, max_length=128)
    period: str = "monthly"
    limit_amount: float = Field(ge=0)
    currency: str = "USD"
    resource_type: str = "all"
    actions: list[str] = Field(default_factory=lambda: ["record_violation", "warn"])
    metadata: dict[str, Any] = Field(default_factory=dict)


class BudgetPolicyUpdateRequest(BaseModel):
    policy_name: str | None = None
    period: str | None = None
    limit_amount: float | None = Field(default=None, ge=0)
    currency: str | None = None
    resource_type: str | None = None
    actions: list[str] | None = None
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class BudgetPolicyRecord(BaseModel):
    budget_id: str
    tenant_id: str
    scope: str
    policy_name: str
    period: str
    limit_amount: float
    currency: str
    resource_type: str
    actions: list[str] = Field(default_factory=list)
    is_active: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class BudgetViolationRecord(BaseModel):
    violation_id: str
    budget_id: str
    tenant_id: str
    user_id: str
    run_id: str
    workflow_id: str
    model_name: str
    violation_type: str
    severity: str
    actual_amount: float
    limit_amount: float
    currency: str
    decision: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
