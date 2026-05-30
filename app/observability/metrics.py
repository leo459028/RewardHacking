from prometheus_client import Counter, Histogram

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "HTTP requests by method, path, and status",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

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

POLICY_DECISIONS_TOTAL = Counter(
    "agent_policy_decisions_total",
    "Policy decisions by final request status",
    ["status"],
)

INCIDENTS_TOTAL = Counter(
    "agent_incidents_total",
    "Incidents by severity and code",
    ["severity", "code"],
)

WORKER_JOBS_TOTAL = Counter(
    "agent_worker_jobs_total",
    "Worker jobs by status",
    ["status"],
)

WORKER_DURATION_SECONDS = Histogram(
    "agent_worker_duration_seconds",
    "Worker job duration in seconds",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0, 30.0),
)

WORKER_RETRIES_TOTAL = Counter(
    "agent_worker_retries_total",
    "Worker retry attempts",
)

WORKER_DLQ_TOTAL = Counter(
    "agent_worker_dlq_total",
    "Worker dead-lettered jobs",
)

EVALUATOR_VERDICTS_TOTAL = Counter(
    "agent_evaluator_verdicts_total",
    "Evaluator verdict count",
    ["verdict"],
)
