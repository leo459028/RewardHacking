from pydantic import BaseModel, Field


class EvaluationResult(BaseModel):
    run_id: str
    goal_alignment: float = Field(ge=0.0, le=1.0)
    shortcut_risk: float = Field(ge=0.0, le=1.0)
    hallucination_risk: float = Field(ge=0.0, le=1.0)
    policy_violation_risk: float = Field(ge=0.0, le=1.0)
    final_score: float = Field(ge=0.0, le=1.0)
    verdict: str
    explanation: str
