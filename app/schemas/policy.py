from enum import Enum
from pydantic import BaseModel, Field


class Decision(str, Enum):
    allow = "allow"
    deny = "deny"
    escalate = "escalate"


class PolicyDecision(BaseModel):
    decision: Decision
    reason: str
    risk_score: float = Field(ge=0.0, le=1.0)
    flags: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
