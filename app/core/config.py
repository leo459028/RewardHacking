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
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
