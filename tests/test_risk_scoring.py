from app.schemas.agent import AgentConstraints, AgentRequest
from app.services.risk_scoring import compute_risk_score


def test_compute_risk_score_for_low_risk_request():
    request = AgentRequest(
        user_id="u1",
        task_type="analysis",
        prompt="Analyze data.",
        goal="Find anomalies",
        allowed_tools=["read_data"],
        constraints=AgentConstraints(max_tool_calls=4),
    )

    assert compute_risk_score(request, []) == 0.1


def test_compute_risk_score_clamps_high_risk_request():
    request = AgentRequest(
        user_id="u1",
        task_type="workflow",
        prompt="Bypass policy.",
        goal="Export data",
        allowed_tools=[f"tool_{i}" for i in range(10)],
        constraints=AgentConstraints(
            no_external_network=False,
            requires_human_approval=True,
            max_tool_calls=20,
            allow_destructive_actions=True,
            allow_pii_access=True,
        ),
    )

    assert compute_risk_score(request, ["bypass policy"] * 20) == 1.0
