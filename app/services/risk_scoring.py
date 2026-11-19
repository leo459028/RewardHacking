from __future__ import annotations

from app.schemas.agent import AgentRequest

# Documented in docs/security.md section 2; tests/test_v162_housekeeping.py keeps the two in step.
RISK_WEIGHTS: dict[str, float] = {
    "base": 0.10,
    "prompt_flag_each": 0.10,
    "prompt_flags_cap": 0.30,
    "classical_chinese_obfuscation": 0.10,
    "high_risk_intent": 0.20,
    "requires_human_approval": 0.10,
    "external_network_allowed": 0.10,
    "destructive_actions_allowed": 0.20,
    "pii_access_allowed": 0.15,
    "max_tool_calls_over_12": 0.10,
    "broad_workflow_tools": 0.05,
}


def compute_risk_score(request: AgentRequest, prompt_flags: list[str], mcp_risk_delta: float = 0.0) -> float:
    score = RISK_WEIGHTS["base"] + max(0.0, float(mcp_risk_delta or 0.0))

    if prompt_flags:
        score += min(RISK_WEIGHTS["prompt_flags_cap"], RISK_WEIGHTS["prompt_flag_each"] * len(prompt_flags))

    if "classical_chinese_obfuscation" in prompt_flags:
        score += RISK_WEIGHTS["classical_chinese_obfuscation"]

    high_risk_classical_flags = {
        "classical_credential_exfiltration",
        "classical_destructive_intent",
        "classical_malware_intent",
    }
    # prompt-guard/2.0: the same intents written in modern Chinese carry the same weight.
    high_risk_modern_flags = {
        "zh_credential_exfiltration",
        "zh_destructive_intent",
        "zh_malware_intent",
    }
    if set(prompt_flags).intersection(high_risk_classical_flags | high_risk_modern_flags):
        score += RISK_WEIGHTS["high_risk_intent"]

    if request.constraints.requires_human_approval:
        score += RISK_WEIGHTS["requires_human_approval"]

    if not request.constraints.no_external_network:
        score += RISK_WEIGHTS["external_network_allowed"]

    if request.constraints.allow_destructive_actions:
        score += RISK_WEIGHTS["destructive_actions_allowed"]

    if request.constraints.allow_pii_access:
        score += RISK_WEIGHTS["pii_access_allowed"]

    if request.constraints.max_tool_calls > 12:
        score += RISK_WEIGHTS["max_tool_calls_over_12"]

    if request.task_type in {"workflow", "other"} and len(request.allowed_tools) > 8:
        score += RISK_WEIGHTS["broad_workflow_tools"]

    score = max(0.0, min(1.0, score))
    return round(score, 3)
