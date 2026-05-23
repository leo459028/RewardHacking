from __future__ import annotations

from app.core.config import get_settings
from app.services.evaluator import Evaluator
from app.services.policy_engine import PolicyEngine


async def run_worker() -> None:
    settings = get_settings()
    _policy_engine = PolicyEngine(risk_threshold=settings.default_risk_threshold)
    _evaluator = Evaluator()
    # Integrate AIOKafkaConsumer here and consume from agent.requests.
    # For each record:
    # - reconstruct request
    # - re-run policy checks
    # - execute bounded tools
    # - evaluate result
    # - persist to Postgres
    # - publish to Kafka
    return None
