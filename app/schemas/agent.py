from typing import Any, Literal
from pydantic import BaseModel, Field


class AgentConstraints(BaseModel):
    no_external_network: bool = True
    requires_human_approval: bool = False
    max_tool_calls: int = 8
    allow_destructive_actions: bool = False
    allow_pii_access: bool = False


class AgentRequest(BaseModel):
    user_id: str
    task_type: Literal["code_fix", "analysis", "data_query", "workflow", "other"] = "other"
    prompt: str = Field(min_length=1)
    goal: str
    allowed_tools: list[str] = Field(default_factory=list)
    constraints: AgentConstraints = Field(default_factory=AgentConstraints)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    run_id: str
    status: Literal["accepted", "denied", "escalated", "queued", "completed"]
    decision_reason: str
    risk_score: float
    policy_flags: list[str] = Field(default_factory=list)
