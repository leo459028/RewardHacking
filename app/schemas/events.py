from typing import Any, Literal
from pydantic import BaseModel, Field


class BaseEvent(BaseModel):
    event_id: str
    trace_id: str
    run_id: str
    event_type: str
    ts: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PolicyDecisionEvent(BaseEvent):
    event_type: Literal["policy.decision"] = "policy.decision"


class ExecutionEvent(BaseEvent):
    event_type: Literal["execution.result"] = "execution.result"


class EvaluationEvent(BaseEvent):
    event_type: Literal["evaluation.result"] = "evaluation.result"


class IncidentEvent(BaseEvent):
    event_type: Literal["incident"] = "incident"
