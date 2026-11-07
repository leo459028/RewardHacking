from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
import re

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_FINOPS_FX_RATES: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "CNY": Decimal("0.14"),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "JPY": Decimal("0.0067"),
    "TWD": Decimal("0.031"),
    "HKD": Decimal("0.128"),
    "SGD": Decimal("0.74"),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "reward-hacking-guardrail"
    app_env: str = "dev"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    postgres_dsn: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9092"

    kafka_request_topic: str = "agent.requests"
    kafka_decision_topic: str = "agent.decisions"
    kafka_execution_topic: str = "agent.executions"
    kafka_evaluation_topic: str = "agent.evaluations"
    kafka_incident_topic: str = "agent.incidents"

    default_max_tool_calls: int = 8
    default_risk_threshold: float = 0.65
    worker_max_attempts: int = 3
    worker_retry_backoff_seconds: int = 30
    approval_ttl_seconds: int = 3600
    worker_completion_latency_target_seconds: int = 300
    incident_response_target_seconds: int = 900
    llm_judge_enabled: bool = False
    auth_enabled: bool = False
    api_keys: str = "demo-key:default"
    dashboard_auth_enabled: bool = False
    dashboard_username: str = "admin"
    dashboard_password: str = "admin"
    quota_enabled: bool = True
    tenant_request_limit_per_minute: int = 60
    tenant_high_risk_limit_per_hour: int = 10
    user_request_limit_per_minute: int = 30
    tool_call_limit_per_minute: int = 60
    tool_breaker_failure_threshold: int = 3
    tool_breaker_ttl_seconds: int = 1800
    mcp_server_call_limit_per_minute: int = 90
    mcp_schema_enforcement_mode: str = "deny"
    mcp_internal_host_suffixes: str = ".internal.local,.mcp.internal"
    mcp_tool_arguments_max_depth: int = 8
    mcp_tool_arguments_max_bytes: int = 65536
    audit_retention_days: int = 90
    operations_evidence_max_age_days: int = 30
    secret_rotation_max_age_days: int = 90
    release_gate_max_queued_age_minutes: int = 15
    release_gate_fail_on_bootstrap_keys_in_prod: bool = True
    pricing_source_stale_days: int = 30
    finops_budget_enforcement_enabled: bool = True
    finops_base_currency: str = "USD"
    finops_default_tax_rate: Decimal = Decimal("0")
    finops_fx_rates: dict[str, Decimal] = Field(default_factory=lambda: dict(DEFAULT_FINOPS_FX_RATES))
    finops_fx_rates_as_of: str = "2026-10-01"
    finops_export_max_rows: int = 10000
    finops_burn_rate_window_hours: int = 24
    finops_burn_rate_min_elapsed_hours: Decimal = Decimal("6")
    finops_burn_rate_min_samples: int = 2
    finops_token_lease_ttl_seconds: int = 7200

    @model_validator(mode="after")
    def _validate_finops_currency_settings(self) -> Settings:
        base = (self.finops_base_currency or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", base):
            raise ValueError("FINOPS_BASE_CURRENCY must be a 3-letter uppercase ISO currency code.")
        self.finops_base_currency = base
        if self.finops_default_tax_rate < Decimal("0"):
            raise ValueError("FINOPS_DEFAULT_TAX_RATE must be >= 0.")
        normalized: dict[str, Decimal] = {}
        for code, rate in (self.finops_fx_rates or {}).items():
            norm_code = str(code).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", norm_code):
                raise ValueError(f"Invalid currency code in FINOPS_FX_RATES: {code!r}")
            dec_rate = Decimal(str(rate))
            if dec_rate <= Decimal("0"):
                raise ValueError(f"FX rate for {norm_code} must be > 0.")
            normalized[norm_code] = dec_rate
        if base in normalized and normalized[base] != Decimal("1"):
            raise ValueError(f"FX rate for base currency {base} must be 1.")
        normalized[base] = Decimal("1")
        self.finops_fx_rates = normalized
        if self.finops_burn_rate_window_hours < 1:
            raise ValueError("FINOPS_BURN_RATE_WINDOW_HOURS must be >= 1.")
        if self.finops_burn_rate_min_elapsed_hours < Decimal("0"):
            raise ValueError("FINOPS_BURN_RATE_MIN_ELAPSED_HOURS must be >= 0.")
        if self.finops_burn_rate_min_samples < 1:
            raise ValueError("FINOPS_BURN_RATE_MIN_SAMPLES must be >= 1.")
        if self.finops_token_lease_ttl_seconds < self.approval_ttl_seconds:
            raise ValueError("FINOPS_TOKEN_LEASE_TTL_SECONDS must be >= APPROVAL_TTL_SECONDS.")
        if self.user_request_limit_per_minute < 1:
            raise ValueError("USER_REQUEST_LIMIT_PER_MINUTE must be >= 1.")
        if self.tool_call_limit_per_minute < 1:
            raise ValueError("TOOL_CALL_LIMIT_PER_MINUTE must be >= 1.")
        if self.tool_breaker_failure_threshold < 1:
            raise ValueError("TOOL_BREAKER_FAILURE_THRESHOLD must be >= 1.")
        if self.tool_breaker_ttl_seconds < 1:
            raise ValueError("TOOL_BREAKER_TTL_SECONDS must be >= 1.")
        if self.mcp_server_call_limit_per_minute < 1:
            raise ValueError("MCP_SERVER_CALL_LIMIT_PER_MINUTE must be >= 1.")
        if self.mcp_schema_enforcement_mode not in {"deny", "escalate"}:
            raise ValueError("MCP_SCHEMA_ENFORCEMENT_MODE must be 'deny' or 'escalate'.")
        if self.mcp_tool_arguments_max_depth < 1:
            raise ValueError("MCP_TOOL_ARGUMENTS_MAX_DEPTH must be >= 1.")
        if self.mcp_tool_arguments_max_bytes < 1:
            raise ValueError("MCP_TOOL_ARGUMENTS_MAX_BYTES must be >= 1.")
        for suffix in self.mcp_internal_host_suffix_list:
            if not suffix.startswith("."):
                raise ValueError("MCP_INTERNAL_HOST_SUFFIXES entries must start with '.'.")
        return self

    @property
    def mcp_internal_host_suffix_list(self) -> list[str]:
        return [item.strip().lower() for item in self.mcp_internal_host_suffixes.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
