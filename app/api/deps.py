from app.core.config import get_settings
from app.services.evaluator import Evaluator
from app.services.policy_engine import PolicyEngine
from app.services.orchestrator import AgentOrchestrator


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
