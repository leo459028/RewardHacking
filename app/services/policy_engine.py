from __future__ import annotations

from dataclasses import dataclass
from app.schemas.agent import AgentRequest
from app.schemas.policy import Decision, PolicyDecision
from app.services.risk_scoring import compute_risk_score


@dataclass(frozen=True)
class RuleContext:
    request: AgentRequest
    prompt_flags: list[str]
    risk_score: float


class PolicyEngine:
    def __init__(self, risk_threshold: float = 0.65):
        self.risk_threshold = risk_threshold

    def evaluate(
        self,
        request: AgentRequest,
        prompt_flags: list[str],
        disabled_tools: list[str] | None = None,
    ) -> PolicyDecision:
        risk_score = compute_risk_score(request, prompt_flags)
        context = RuleContext(request=request, prompt_flags=prompt_flags, risk_score=risk_score)
        flags: list[str] = []
        disabled_tools = disabled_tools or []

        if context.prompt_flags:
            flags.append("prompt_injection_signals")

        if request.constraints.max_tool_calls <= 0:
            flags.append("invalid_tool_budget")

        if request.constraints.allow_destructive_actions and request.constraints.requires_human_approval is False:
            flags.append("destructive_without_approval")

        if request.constraints.allow_pii_access:
            flags.append("pii_access_requested")

        if request.constraints.no_external_network is False:
            flags.append("external_network_allowed")

        if request.constraints.max_tool_calls > 12:
            flags.append("large_tool_budget")

        for tool_name in sorted(set(request.allowed_tools).intersection(disabled_tools)):
            flags.append(f"tool_circuit_breaker_open:{tool_name}")

        if risk_score >= self.risk_threshold:
            return PolicyDecision(
                decision=Decision.escalate if risk_score < 0.85 else Decision.deny,
                reason="Risk threshold exceeded or elevated by policy flags.",
                risk_score=risk_score,
                flags=flags,
                requires_human_review=True,
            )

        if flags:
            return PolicyDecision(
                decision=Decision.escalate,
                reason="One or more policy flags require review.",
                risk_score=risk_score,
                flags=flags,
                requires_human_review=True,
            )

        return PolicyDecision(
            decision=Decision.allow,
            reason="Request passed deterministic policy checks.",
            risk_score=risk_score,
            flags=flags,
            requires_human_review=False,
        )
