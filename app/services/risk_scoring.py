from __future__ import annotations

from app.schemas.agent import AgentRequest


def compute_risk_score(request: AgentRequest, prompt_flags: list[str]) -> float:
    score = 0.10

    if prompt_flags:
        score += min(0.30, 0.10 * len(prompt_flags))

    if request.constraints.requires_human_approval:
        score += 0.10

    if not request.constraints.no_external_network:
        score += 0.10

    if request.constraints.allow_destructive_actions:
        score += 0.20

    if request.constraints.allow_pii_access:
        score += 0.15

    if request.constraints.max_tool_calls > 12:
        score += 0.10

    if request.task_type in {"workflow", "other"} and len(request.allowed_tools) > 8:
        score += 0.05

    score = max(0.0, min(1.0, score))
    return round(score, 3)
