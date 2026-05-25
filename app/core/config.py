from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
