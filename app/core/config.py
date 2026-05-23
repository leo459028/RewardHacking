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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
