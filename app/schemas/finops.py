from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Provider(str, Enum):
    openai = "openai"
    google = "google"
    anthropic = "anthropic"
    deepseek = "deepseek"
    kimi = "kimi"
    minimax = "minimax"
    zai = "zai"
    internal = "internal"
    custom = "custom"


class BillingPlatform(str, Enum):
    direct_api = "direct_api"
    azure = "azure"
    vertex_ai = "vertex_ai"
    bedrock = "bedrock"
    openrouter = "openrouter"
    cloud_marketplace = "cloud_marketplace"
    subscription = "subscription"
    custom = "custom"


class ResourceType(str, Enum):
    model = "model"
    tool = "tool"
    runtime = "runtime"
    storage = "storage"
    network = "network"
    search = "search"
    container = "container"
    managed_agent = "managed_agent"
    human_review = "human_review"
    evaluation = "evaluation"
    image = "image"
    audio = "audio"
    video = "video"


class ResourceSubtype(str, Enum):
    chat_completion = "chat_completion"
    embedding = "embedding"
    rerank = "rerank"
    web_search = "web_search"
    grounding = "grounding"
    code_execution = "code_execution"
    container_runtime = "container_runtime"
    file_extraction = "file_extraction"
    image_generation = "image_generation"
    audio_generation = "audio_generation"
    video_generation = "video_generation"
    context_cache_storage = "context_cache_storage"
    asr = "asr"
    other = "other"


class Modality(str, Enum):
    text = "text"
    image = "image"
    audio = "audio"
    video = "video"
    multimodal = "multimodal"


class PricingMode(str, Enum):
    standard = "standard"
    batch = "batch"
    flex = "flex"
    priority = "priority"
    fast = "fast"
    promotion = "promotion"
    contract = "contract"
    tenant_override = "tenant_override"
    manual = "manual"


class TokenType(str, Enum):
    input = "input"
    output = "output"
    cached_input = "cached_input"
    cache_write = "cache_write"
    cache_write_5m = "cache_write_5m"
    cache_write_1h = "cache_write_1h"
    cache_storage = "cache_storage"
    reasoning = "reasoning"
    audio_input = "audio_input"
    audio_output = "audio_output"
    image_input = "image_input"
    image_output = "image_output"
    video_input = "video_input"
    video_output = "video_output"
    tool_result = "tool_result"
    not_applicable = "not_applicable"


class PriceUnit(str, Enum):
    per_1m_tokens = "per_1m_tokens"
    per_1m_tokens_hour = "per_1m_tokens_hour"
    per_1k_calls = "per_1k_calls"
    per_call = "per_call"
    per_hour = "per_hour"
    per_minute = "per_minute"
    per_second = "per_second"
    per_image = "per_image"
    per_video = "per_video"
    per_gb_month = "per_gb_month"
    per_gb_second = "per_gb_second"
    per_container_session = "per_container_session"


class UsageStatus(str, Enum):
    success = "success"
    failed = "failed"
    timeout = "timeout"
    cancelled = "cancelled"
    skipped = "skipped"


class BudgetPeriod(str, Enum):
    per_run = "per_run"
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    quarterly = "quarterly"
    yearly = "yearly"


class BudgetBasis(str, Enum):
    estimated = "estimated"
    actual = "actual"
    gross = "gross"
    net = "net"
    base_currency = "base_currency"


class BudgetAction(str, Enum):
    record_violation = "record_violation"
    warn = "warn"
    notify = "notify"
    require_approval = "require_approval"
    fail_closed = "fail_closed"
    degrade_model = "degrade_model"
    restrict_tools = "restrict_tools"
    cap_output_tokens = "cap_output_tokens"
    dry_run_only = "dry_run_only"


class ViolationSeverity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ThresholdType(str, Enum):
    soft = "soft"
    hard = "hard"


class ResolutionStatus(str, Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    ignored = "ignored"


class ScopeType(str, Enum):
    system_default = "system_default"
    tenant_override = "tenant_override"
    contract = "contract"
    manual = "manual"


class CostType(str, Enum):
    estimated = "estimated"
    actual = "actual"
    adjustment = "adjustment"


class FinOpsBaseModel(BaseModel):
    model_config = ConfigDict(use_enum_values=True, json_encoders={Decimal: str})


class PricingCatalogCreateRequest(FinOpsBaseModel):
    pricing_id: str | None = None
    provider: Provider = Provider.internal
    provider_display_name: str = ""
    billing_platform: BillingPlatform = BillingPlatform.direct_api
    provider_region: str = "global"
    billing_region: str = "global"
    resource_type: ResourceType
    resource_subtype: ResourceSubtype = ResourceSubtype.other
    model_name: str = ""
    model_alias: str = ""
    model_version: str = ""
    model_family: str = ""
    model_tier: str = ""
    modality: Modality = Modality.text
    pricing_mode: PricingMode = PricingMode.standard
    service_tier: str = "standard"
    token_type: TokenType = TokenType.not_applicable
    price_unit: PriceUnit
    unit_price: Decimal = Field(ge=0)
    currency: str = Field(default="USD", min_length=1, max_length=8)
    tax_included: bool = False
    discount_rate: Decimal = Field(default=Decimal("0"), ge=0)
    multiplier: Decimal = Field(default=Decimal("1"), ge=0)
    minimum_billable_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    free_quota_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    free_quota_period: str = ""
    input_threshold_tokens_min: int | None = None
    input_threshold_tokens_max: int | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    cache_ttl_seconds: int | None = None
    cache_storage_unit_price: Decimal = Field(default=Decimal("0"), ge=0)
    supports_cache: bool | None = None
    supports_batch: bool | None = None
    supports_tools: bool | None = None
    supports_thinking: bool | None = None
    supports_streaming: bool | None = None
    supports_json_mode: bool | None = None
    supports_multimodal: bool | None = None
    source_url: str = "manual://api"
    source_type: str = "manual"
    source_confidence: str = "manual"
    source_checked_at: datetime = Field(default_factory=_now_utc)
    pricing_version: str = "manual"
    effective_from: datetime = Field(default_factory=_now_utc)
    effective_to: datetime | None = None
    scope_type: ScopeType = ScopeType.manual
    scope_id: str | None = None
    is_system_default: bool = False
    is_tenant_override: bool = False
    supersedes_pricing_id: str | None = None
    import_batch_id: str | None = None
    change_reason: str = ""
    created_by: str = ""
    updated_by: str = ""
    is_active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class PricingCatalogUpdateRequest(FinOpsBaseModel):
    provider: Provider | None = None
    provider_display_name: str | None = None
    billing_platform: BillingPlatform | None = None
    provider_region: str | None = None
    billing_region: str | None = None
    resource_type: ResourceType | None = None
    resource_subtype: ResourceSubtype | None = None
    model_name: str | None = None
    model_alias: str | None = None
    model_version: str | None = None
    model_family: str | None = None
    model_tier: str | None = None
    modality: Modality | None = None
    pricing_mode: PricingMode | None = None
    service_tier: str | None = None
    token_type: TokenType | None = None
    price_unit: PriceUnit | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    tax_included: bool | None = None
    discount_rate: Decimal | None = Field(default=None, ge=0)
    multiplier: Decimal | None = Field(default=None, ge=0)
    minimum_billable_quantity: Decimal | None = Field(default=None, ge=0)
    free_quota_quantity: Decimal | None = Field(default=None, ge=0)
    free_quota_period: str | None = None
    input_threshold_tokens_min: int | None = None
    input_threshold_tokens_max: int | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    cache_ttl_seconds: int | None = None
    cache_storage_unit_price: Decimal | None = Field(default=None, ge=0)
    supports_cache: bool | None = None
    supports_batch: bool | None = None
    supports_tools: bool | None = None
    supports_thinking: bool | None = None
    supports_streaming: bool | None = None
    supports_json_mode: bool | None = None
    supports_multimodal: bool | None = None
    source_url: str | None = None
    source_type: str | None = None
    source_confidence: str | None = None
    source_checked_at: datetime | None = None
    pricing_version: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    scope_type: ScopeType | None = None
    scope_id: str | None = None
    is_system_default: bool | None = None
    is_tenant_override: bool | None = None
    supersedes_pricing_id: str | None = None
    import_batch_id: str | None = None
    change_reason: str | None = None
    updated_by: str | None = None
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class PricingCatalogRecord(PricingCatalogCreateRequest):
    pricing_id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, json_encoders={Decimal: str})


class UsageEventCreateRequest(FinOpsBaseModel):
    usage_id: str | None = None
    tenant_id: str = "default"
    org_id: str = ""
    project_id: str = ""
    environment: str = "dev"
    cost_center: str = ""
    user_id: str = ""
    workflow_id: str = ""
    workflow_version: str = ""
    run_id: str = ""
    step_id: str = ""
    attempt_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    checkpoint_id: str = ""
    replay_attempt_id: str = ""
    resume_attempt_id: str = ""
    provider: Provider = Provider.internal
    billing_platform: BillingPlatform = BillingPlatform.direct_api
    provider_request_id: str = ""
    provider_response_id: str = ""
    model_name: str = ""
    model_version: str = ""
    pricing_id: str = ""
    resource_type: ResourceType
    resource_subtype: ResourceSubtype = ResourceSubtype.other
    usage_unit: str
    quantity: Decimal = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    audio_input_tokens: int = Field(default=0, ge=0)
    audio_output_tokens: int = Field(default=0, ge=0)
    image_input_count: int = Field(default=0, ge=0)
    image_output_count: int = Field(default=0, ge=0)
    video_input_seconds: Decimal = Field(default=Decimal("0"), ge=0)
    tool_id: str = ""
    tool_name: str = ""
    tool_provider: str = ""
    tool_call_count: int = Field(default=0, ge=0)
    mcp_server_id: str = ""
    tool_risk_level: str = ""
    dry_run: bool = False
    external_side_effect: bool = False
    container_seconds: Decimal = Field(default=Decimal("0"), ge=0)
    worker_seconds: Decimal = Field(default=Decimal("0"), ge=0)
    storage_gb_seconds: Decimal = Field(default=Decimal("0"), ge=0)
    network_egress_gb: Decimal = Field(default=Decimal("0"), ge=0)
    phase: str = "manual"
    status: UsageStatus = UsageStatus.success
    error_code: str = ""
    error_message_hash: str = ""
    latency_ms: int = Field(default=0, ge=0)
    queue_wait_ms: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    idempotency_key: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class UsageEventRecord(UsageEventCreateRequest):
    usage_id: str
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, json_encoders={Decimal: str})


class CostEventRecord(FinOpsBaseModel):
    cost_id: str
    usage_event_id: str
    pricing_catalog_id: str
    pricing_snapshot: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str
    org_id: str
    project_id: str
    cost_center: str
    environment: str
    user_id: str
    run_id: str
    workflow_id: str
    step_id: str
    checkpoint_id: str
    replay_attempt_id: str
    resume_attempt_id: str
    trace_id: str
    provider: str
    billing_platform: str
    resource_type: str
    resource_subtype: str
    model_name: str
    model_version: str
    cost_type: str
    usage_unit: str
    quantity: Decimal
    unit_price: Decimal
    gross_cost: Decimal
    discount_rate: Decimal
    discount_amount: Decimal
    multiplier: Decimal
    net_cost: Decimal
    tax_amount: Decimal
    estimated_cost: Decimal
    actual_cost: Decimal
    currency: str
    base_currency: str
    base_currency_cost: Decimal
    fx_rate: Decimal
    fx_rate_source: str
    billing_period_start: datetime | None = None
    billing_period_end: datetime | None = None
    provider_invoice_id: str
    calculation_method: str
    calculation_version: str
    allocation_tags: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, json_encoders={Decimal: str})


class RunCostSummaryResponse(FinOpsBaseModel):
    run_id: str
    tenant_id: str
    base_currency: str = "USD"
    estimated_cost: Decimal
    actual_cost: Decimal
    cost_delta: Decimal
    cost_delta_percent: Decimal = Decimal("0")
    currency: str
    budget_status: str
    budget_policy_id: str = ""
    budget_remaining: Decimal = Decimal("0")
    provider_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    model_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    tool_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    step_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    cost_type_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    resource_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    breakdown: dict[str, Decimal] = Field(default_factory=dict)
    replay_cost: Decimal = Decimal("0")
    resume_incremental_cost: Decimal = Decimal("0")
    failed_retry_cost: Decimal = Decimal("0")
    usage_events: list[UsageEventRecord] = Field(default_factory=list)
    cost_events: list[CostEventRecord] = Field(default_factory=list)


class TenantCostSummaryResponse(FinOpsBaseModel):
    tenant_id: str
    period_start: datetime | None = None
    period_end: datetime | None = None
    timezone: str = "UTC"
    base_currency: str = "USD"
    today_cost: Decimal
    month_to_date_cost: Decimal
    budget_used_percent: Decimal
    violation_count: int
    provider_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    resource_type_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    environment_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    project_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    top_runs: list[dict[str, Any]] = Field(default_factory=list)
    top_models: list[dict[str, Any]] = Field(default_factory=list)
    top_workflows: list[dict[str, Any]] = Field(default_factory=list)
    top_tools: list[dict[str, Any]] = Field(default_factory=list)
    top_users: list[dict[str, Any]] = Field(default_factory=list)
    failed_run_cost: Decimal = Decimal("0")
    replay_cost: Decimal = Decimal("0")
    cache_savings: Decimal = Decimal("0")
    batch_savings: Decimal = Decimal("0")


class BudgetPolicyCreateRequest(FinOpsBaseModel):
    budget_id: str | None = None
    tenant_id: str = "default"
    scope: str = "tenant"
    scope_id: str = ""
    project_id: str = ""
    environment: str = "prod"
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = "1"
    period: BudgetPeriod = BudgetPeriod.monthly
    period_start: datetime | None = None
    period_end: datetime | None = None
    timezone: str = "UTC"
    limit_amount: Decimal | None = Field(default=None, ge=0)
    soft_limit_amount: Decimal | None = Field(default=None, ge=0)
    hard_limit_amount: Decimal | None = Field(default=None, ge=0)
    currency: str = "USD"
    base_currency: str = "USD"
    resource_type: str = "all"
    provider: str = ""
    model_name: str = ""
    model_tier: str = ""
    tool_id: str = ""
    workflow_id: str = ""
    budget_basis: BudgetBasis = BudgetBasis.actual
    include_failed_runs: bool = True
    include_replay_runs: bool = True
    include_dev_environment: bool = False
    actions: list[BudgetAction] = Field(default_factory=lambda: [BudgetAction.record_violation, BudgetAction.warn])
    actions_on_soft_limit: list[BudgetAction] = Field(default_factory=lambda: [BudgetAction.record_violation, BudgetAction.warn])
    actions_on_hard_limit: list[BudgetAction] = Field(default_factory=lambda: [BudgetAction.record_violation, BudgetAction.fail_closed])
    notification_channels: list[str] = Field(default_factory=list)
    approval_required: bool = False
    approver_role: str = ""
    fallback_model_tier: str = ""
    effective_from: datetime = Field(default_factory=_now_utc)
    effective_to: datetime | None = None
    created_by: str = ""
    updated_by: str = ""
    is_active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class BudgetPolicyUpdateRequest(FinOpsBaseModel):
    scope: str | None = None
    scope_id: str | None = None
    project_id: str | None = None
    environment: str | None = None
    policy_name: str | None = None
    policy_version: str | None = None
    period: BudgetPeriod | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    timezone: str | None = None
    limit_amount: Decimal | None = Field(default=None, ge=0)
    soft_limit_amount: Decimal | None = Field(default=None, ge=0)
    hard_limit_amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    base_currency: str | None = None
    resource_type: str | None = None
    provider: str | None = None
    model_name: str | None = None
    model_tier: str | None = None
    tool_id: str | None = None
    workflow_id: str | None = None
    budget_basis: BudgetBasis | None = None
    include_failed_runs: bool | None = None
    include_replay_runs: bool | None = None
    include_dev_environment: bool | None = None
    actions: list[BudgetAction] | None = None
    actions_on_soft_limit: list[BudgetAction] | None = None
    actions_on_hard_limit: list[BudgetAction] | None = None
    notification_channels: list[str] | None = None
    approval_required: bool | None = None
    approver_role: str | None = None
    fallback_model_tier: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    updated_by: str | None = None
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class BudgetPolicyRecord(BudgetPolicyCreateRequest):
    budget_id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, json_encoders={Decimal: str})


class BudgetViolationRecord(FinOpsBaseModel):
    violation_id: str
    budget_id: str
    budget_policy_version: str
    tenant_id: str
    scope: str
    scope_id: str
    project_id: str
    environment: str
    user_id: str
    run_id: str
    step_id: str
    workflow_id: str
    provider: str
    model_name: str
    resource_type: str
    violation_type: str
    severity: str
    threshold_type: str
    estimated_amount: Decimal
    actual_amount: Decimal
    limit_amount: Decimal
    remaining_budget_before: Decimal
    overage_amount: Decimal
    overage_percent: Decimal
    currency: str
    decision: str
    recommended_action: str
    action_taken: str
    approval_id: str
    acknowledged_by: str
    acknowledged_at: datetime | None = None
    resolved_by: str
    resolved_at: datetime | None = None
    resolution_status: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, json_encoders={Decimal: str})


class PricingImportRequest(FinOpsBaseModel):
    file_path: str = "app/seeds/model_pricing_seed.yaml"
    created_by: str = "system"
    base_currency: str = "USD"
    strict: bool = False
    allow_warnings: bool = True


class PricingImportReportResponse(FinOpsBaseModel):
    import_batch_id: str = ""
    import_mode: str = "dry_run"
    status: str = "validated"
    source_file_name: str = ""
    source_file_hash: str = ""
    valid_rows: int = 0
    new_rows: int = 0
    unchanged_rows: int = 0
    rows_to_supersede: int = 0
    invalid_rows: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    missing_optional_fields: list[dict[str, Any]] = Field(default_factory=list)
    duplicate_keys: list[dict[str, Any]] = Field(default_factory=list)
    stale_sources: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_enum_values: list[dict[str, Any]] = Field(default_factory=list)


class PricingImportBatchRecord(FinOpsBaseModel):
    import_batch_id: str
    source_file_name: str
    source_file_hash: str
    import_mode: str
    status: str
    created_by: str
    created_at: datetime | None = None
    applied_at: datetime | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    error_details: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, json_encoders={Decimal: str})


class PricingChangeLogRecord(FinOpsBaseModel):
    change_id: str
    pricing_id_old: str
    pricing_id_new: str
    change_type: str
    old_unit_price: Decimal | None = None
    new_unit_price: Decimal | None = None
    currency: str
    change_reason: str
    source_url: str
    changed_by: str
    changed_at: datetime | None = None
    import_batch_id: str

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, json_encoders={Decimal: str})


class PricingSourceDocumentRecord(FinOpsBaseModel):
    source_id: str
    provider: str
    source_url: str
    source_type: str
    checked_at: datetime | None = None
    content_hash: str
    confidence: str
    notes: str
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, json_encoders={Decimal: str})
