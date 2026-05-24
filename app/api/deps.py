from app.core.config import get_settings
from app.infra.redis import get_redis
from app.services.events import EventPublisher, KafkaEventPublisher
from app.services.evaluator import Evaluator
from app.services.policy_engine import PolicyEngine
from app.services.orchestrator import AgentOrchestrator
from app.services.run_state import RedisRunStateStore, RunStateStore


def get_policy_engine() -> PolicyEngine:
    settings = get_settings()
    return PolicyEngine(risk_threshold=settings.default_risk_threshold)


def get_evaluator() -> Evaluator:
    return Evaluator()


def get_orchestrator() -> AgentOrchestrator:
    settings = get_settings()
    return AgentOrchestrator(
        policy_engine=PolicyEngine(risk_threshold=settings.default_risk_threshold),
        evaluator=Evaluator(),
    )


def get_state_store() -> RunStateStore:
    settings = get_settings()
    return RedisRunStateStore(get_redis(settings))


def get_event_publisher() -> EventPublisher:
    return KafkaEventPublisher(get_settings())
