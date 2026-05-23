from prometheus_client import Counter, Histogram

REQUESTS_TOTAL = Counter(
    "agent_requests_total",
    "Total agent requests",
    ["status"],
)

POLICY_RISK = Histogram(
    "agent_policy_risk_score",
    "Policy risk score distribution",
    buckets=(0.0, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0),
)

EVALUATION_SCORE = Histogram(
    "agent_evaluation_final_score",
    "Final evaluation score distribution",
    buckets=(0.0, 0.25, 0.5, 0.72, 0.85, 1.0),
)
