from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
import re
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # v1.6.1 D9: DASHBOARD_AUTH_ENABLED only derives DASHBOARD_AUTH_MODE when the mode is unset (true -> session).
    dashboard_auth_enabled: bool = False
    # Deprecated since v1.6.1 (Basic Auth was removed; D14). Ignored; the operations check warns when still set.
    dashboard_username: str = "admin"
    dashboard_password: str = "admin"
    # v1.6.1 Enterprise Access (D9, D12, D13, D15, D17, D26): personal accounts and server-side sessions.
    # off = no sign-in (local development only; a synthetic local-admin principal) | session = sign-in required.
    dashboard_auth_mode: str = ""
    dashboard_cookie_secure: str = "auto"  # auto (Secure unless APP_ENV=dev) | true | false
    dashboard_session_idle_minutes: int = 30
    dashboard_session_max_hours: int = 8
    dashboard_reauth_max_age_seconds: int = 300
    dashboard_login_max_failures: int = 5
    dashboard_login_lockout_minutes: int = 15
    dashboard_password_min_length: int = 12
    # scrypt cost (OWASP Password Storage Cheat Sheet equivalent option N=2^15, r=8, p=3; about 32 MiB per hash)
    dashboard_password_scrypt_n: int = 32768
    dashboard_password_scrypt_r: int = 8
    dashboard_password_scrypt_p: int = 3
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
    # v1.6.0 D11 / D14 / D16: retention purge (plan -> apply). off | plan | apply; unknown values behave as off.
    audit_retention_purge_mode: str = "plan"
    audit_retention_purge_batch_size: int = 500
    audit_retention_plan_ttl_minutes: int = 60
    finops_ledger_retention_days: int = 2555  # report only; the FinOps ledger is never deleted
    # v1.6.0 D21 / D23: sandbox spec. "simulated" (nothing is executed) is the only backend in this release;
    # the isolation requirement is recorded in the tool transcript and enforced from v1.6.1.
    sandbox_backend: str = "simulated"
    sandbox_high_risk_threshold: float | None = None  # None -> DEFAULT_RISK_THRESHOLD
    sandbox_min_isolation_default: str = "container"
    sandbox_min_isolation_high_risk: str = "gvisor"
    sandbox_max_timeout_seconds: int = 300
    sandbox_max_output_bytes: int = 1048576
    # v1.6.0 D5: optional OpenTelemetry export (endpoint / service name / sampler use the standard OTEL_* variables)
    otel_enabled: bool = False
    otel_trace_ui_url_template: str = ""
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
    # v1.5.8 evidence-based evaluation & post-execution review queue
    evaluation_outcome_max_submissions: int = 3
    evaluation_max_diff_bytes: int = 524288
    evaluation_max_test_report_bytes: int = 1048576
    evaluation_max_final_message_bytes: int = 32768
    evaluation_max_file_content_bytes: int = 262144
    evaluation_lint_suppression_threshold: int = 3
    evaluation_review_p0_sla_hours: int = 4
    evaluation_detector_modes: dict[str, str] = Field(default_factory=dict)
    # v1.5.9 labeled benchmark, cheat-rate cohorts and CI attestation verification
    evaluation_benchmark_min_tpr: float = 0.90
    evaluation_benchmark_gate_fpr: float = 0.05
    evaluation_benchmark_gate_enforced: bool = False
    evaluation_benchmark_max_samples: int = 5000
    evaluation_cheat_rate_min_samples: int = 20
    evaluation_attestation_max_age_hours: int = 24
    evaluation_attestation_max_bytes: int = 262144
    evaluation_sigstore_enabled: bool = False
    evaluation_inspect_import_max_bytes: int = 5242880
    # v1.5.10 LLM-as-judge triage (advisory ordering only) and quota-capped review sampling
    evaluation_judge_mode: str = "shadow"
    evaluation_judge_provider: str = ""
    evaluation_judge_base_url: str = ""
    evaluation_judge_model: str = ""
    evaluation_judge_api_key: SecretStr = SecretStr("")
    evaluation_judge_allow_external: bool = False
    evaluation_judge_timeout_seconds: float = 8.0
    evaluation_judge_max_input_chars: int = 24000
    evaluation_judge_max_output_tokens: int = 600
    evaluation_judge_daily_token_cap_per_tenant: int = 200000
    evaluation_judge_benchmark_max_samples: int = 500
    evaluation_sampling_enabled: bool = True
    evaluation_sample_min_score: float = 0.5
    evaluation_sample_daily_cap_per_tenant: int = 20
    evaluation_sample_max_share: float = 0.05
    evaluation_sample_random_rate: float = 0.02
    evaluation_sample_random_daily_cap_per_tenant: int = 5
    evaluation_sample_secret: SecretStr = SecretStr("")
    # v1.5.11 named judge profiles (local and cloud judges are configuration, not code)
    evaluation_judge_profile: str = "default"
    evaluation_judge_profiles_file: str = "config/judge_profiles.yaml"
    evaluation_judge_profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    evaluation_judge_internal_hosts: str = "host.docker.internal"
    evaluation_judge_background_concurrency: int = 1
    evaluation_judge_pending_sweep_seconds: int = 600
    # v1.5.12 judge activation (probe + explicit enable), key-variable allowlist, breaker and Dashboard editing gate
    evaluation_judge_activation_refresh_seconds: int = 30
    evaluation_judge_probe_max_age_hours: int = 24
    evaluation_judge_key_env_allowlist: str = ""
    evaluation_judge_breaker_failure_threshold: int = 3
    evaluation_judge_breaker_ttl_seconds: int = 300
    evaluation_judge_dashboard_editing: str = "auto"

    @field_validator("sandbox_high_risk_threshold", mode="before")
    @classmethod
    def _empty_sandbox_threshold_is_unset(cls, value: Any) -> Any:
        # v1.6.0: ``SANDBOX_HIGH_RISK_THRESHOLD=`` (empty) falls back to DEFAULT_RISK_THRESHOLD, as documented.
        return None if isinstance(value, str) and not value.strip() else value

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
        for name in (
            "evaluation_outcome_max_submissions",
            "evaluation_max_diff_bytes",
            "evaluation_max_test_report_bytes",
            "evaluation_max_final_message_bytes",
            "evaluation_max_file_content_bytes",
            "evaluation_lint_suppression_threshold",
            "evaluation_review_p0_sla_hours",
            "evaluation_benchmark_max_samples",
            "evaluation_cheat_rate_min_samples",
            "evaluation_attestation_max_age_hours",
            "evaluation_attestation_max_bytes",
            "evaluation_inspect_import_max_bytes",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name.upper()} must be >= 1.")
        if not 0.0 < float(self.evaluation_benchmark_min_tpr) <= 1.0:
            raise ValueError("EVALUATION_BENCHMARK_MIN_TPR must be in (0, 1].")
        if not 0.0 < float(self.evaluation_benchmark_gate_fpr) < 1.0:
            raise ValueError("EVALUATION_BENCHMARK_GATE_FPR must be in (0, 1).")
        normalized_modes: dict[str, str] = {}
        for detector, mode in (self.evaluation_detector_modes or {}).items():
            mode_value = str(mode).strip().lower()
            if mode_value not in {"enforce", "shadow", "disabled"}:
                raise ValueError("EVALUATION_DETECTOR_MODES values must be 'enforce', 'shadow' or 'disabled'.")
            normalized_modes[str(detector).strip()] = mode_value
        self.evaluation_detector_modes = normalized_modes
        self.evaluation_judge_mode = (self.evaluation_judge_mode or "").strip().lower()
        if self.evaluation_judge_mode not in {"off", "shadow", "triage"}:
            raise ValueError("EVALUATION_JUDGE_MODE must be 'off', 'shadow' or 'triage'.")
        self.evaluation_judge_provider = (self.evaluation_judge_provider or "").strip().lower()
        if self.evaluation_judge_provider not in {"", "deterministic", "openai_compatible"}:
            raise ValueError("EVALUATION_JUDGE_PROVIDER must be 'deterministic' or 'openai_compatible'.")
        if float(self.evaluation_judge_timeout_seconds) <= 0:
            raise ValueError("EVALUATION_JUDGE_TIMEOUT_SECONDS must be > 0.")
        for name in (
            "evaluation_judge_max_input_chars",
            "evaluation_judge_max_output_tokens",
            "evaluation_judge_benchmark_max_samples",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name.upper()} must be >= 1.")
        for name in (
            "evaluation_judge_daily_token_cap_per_tenant",
            "evaluation_sample_daily_cap_per_tenant",
            "evaluation_sample_random_daily_cap_per_tenant",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name.upper()} must be >= 0.")
        for name in ("evaluation_sample_min_score", "evaluation_sample_max_share", "evaluation_sample_random_rate"):
            if not 0.0 <= float(getattr(self, name)) <= 1.0:
                raise ValueError(f"{name.upper()} must be in [0, 1].")
        self.evaluation_judge_profile = (self.evaluation_judge_profile or "default").strip().lower()
        for name in (
            "evaluation_judge_background_concurrency",
            "evaluation_judge_pending_sweep_seconds",
            "evaluation_judge_activation_refresh_seconds",
            "evaluation_judge_probe_max_age_hours",
            "evaluation_judge_breaker_failure_threshold",
            "evaluation_judge_breaker_ttl_seconds",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name.upper()} must be >= 1.")
        self.evaluation_judge_dashboard_editing = (self.evaluation_judge_dashboard_editing or "auto").strip().lower()
        if self.evaluation_judge_dashboard_editing not in {"auto", "off", "insecure_dev"}:
            raise ValueError("EVALUATION_JUDGE_DASHBOARD_EDITING must be 'auto', 'off' or 'insecure_dev'.")
        self._validate_dashboard_access()
        for name in self.judge_key_env_allowlist:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name):
                raise ValueError("EVALUATION_JUDGE_KEY_ENV_ALLOWLIST entries must be environment variable names (A-Z, 0-9, _).")
        return self

    def _validate_dashboard_access(self) -> None:
        """v1.6.1 D9 / D47: normalize the Dashboard access settings; an unset mode is derived from the old flag."""
        mode = (self.dashboard_auth_mode or "").strip().lower()
        if not mode:
            mode = "session" if self.dashboard_auth_enabled else "off"
        if mode not in {"off", "session"}:
            raise ValueError("DASHBOARD_AUTH_MODE must be 'off' or 'session'.")
        self.dashboard_auth_mode = mode
        secure = (self.dashboard_cookie_secure or "auto").strip().lower()
        if secure not in {"auto", "true", "false"}:
            raise ValueError("DASHBOARD_COOKIE_SECURE must be 'auto', 'true' or 'false'.")
        self.dashboard_cookie_secure = secure
        for name in (
            "dashboard_session_idle_minutes",
            "dashboard_session_max_hours",
            "dashboard_reauth_max_age_seconds",
            "dashboard_login_max_failures",
            "dashboard_login_lockout_minutes",
            "dashboard_password_scrypt_r",
            "dashboard_password_scrypt_p",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name.upper()} must be >= 1.")
        if self.dashboard_password_min_length < 12:
            raise ValueError("DASHBOARD_PASSWORD_MIN_LENGTH must be >= 12.")
        n = int(self.dashboard_password_scrypt_n)
        if n < 2 or n & (n - 1):
            raise ValueError("DASHBOARD_PASSWORD_SCRYPT_N must be a power of 2 greater than 1.")

    @property
    def dashboard_cookie_secure_effective(self) -> bool:
        """D15: ``auto`` sets the Secure cookie attribute everywhere except APP_ENV=dev."""
        if self.dashboard_cookie_secure == "auto":
            return (self.app_env or "").strip().lower() != "dev"
        return self.dashboard_cookie_secure == "true"

    @property
    def dashboard_legacy_credentials_set(self) -> bool:
        """D14: DASHBOARD_USERNAME / DASHBOARD_PASSWORD changed from their defaults although they are no longer used."""
        return (self.dashboard_username, self.dashboard_password) != ("admin", "admin")

    @property
    def judge_key_env_allowlist(self) -> frozenset[str]:
        return frozenset(item.strip() for item in (self.evaluation_judge_key_env_allowlist or "").split(",") if item.strip())

    @property
    def mcp_internal_host_suffix_list(self) -> list[str]:
        return [item.strip().lower() for item in self.mcp_internal_host_suffixes.split(",") if item.strip()]

    @property
    def judge_internal_host_list(self) -> list[str]:
        return [item.strip().lower().rstrip(".") for item in self.evaluation_judge_internal_hosts.split(",") if item.strip()]

    @property
    def effective_judge_provider(self) -> str:
        """Configured judge provider; the deprecated ``LLM_JUDGE_ENABLED`` alias selects ``openai_compatible`` (D4)."""
        if self.evaluation_judge_provider:
            return self.evaluation_judge_provider
        return "openai_compatible" if self.llm_judge_enabled else "deterministic"

    @property
    def judge_uses_legacy_alias(self) -> bool:
        return not self.evaluation_judge_provider and bool(self.llm_judge_enabled)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
