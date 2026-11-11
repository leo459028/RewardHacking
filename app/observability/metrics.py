from prometheus_client import Counter, Gauge, Histogram

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

PROMPT_GUARD_FLAGS_TOTAL = Counter(
    "agent_prompt_guard_flags_total",
    "Prompt guard flags detected by deterministic prompt analysis",
    ["flag"],
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

CONTROL_PLANE_STEPS_TOTAL = Counter(
    "agent_control_plane_steps_total",
    "Control plane execution plan step audit rows by type and status",
    ["step_type", "status"],
)

FINOPS_USAGE_EVENTS_TOTAL = Counter(
    "agent_finops_usage_events_total",
    "Total FinOps usage events by provider, resource type, and phase",
    ["provider", "resource_type", "phase"],
)

FINOPS_COST_USD_TOTAL = Counter(
    "agent_finops_cost_usd_total",
    "Total FinOps recorded pre-tax cost in base currency (default USD) by tenant, cost type, and provider",
    ["tenant_id", "cost_type", "provider"],
)

FINOPS_BASE_CURRENCY_COST_TOTAL = Counter(
    "agent_finops_base_currency_cost_total",
    "Total FinOps pre-tax cost in base currency by tenant, original currency, base currency, and FX source",
    ["tenant_id", "currency", "base_currency", "fx_source"],
)

FINOPS_TAX_TOTAL = Counter(
    "agent_finops_tax_total",
    "Total FinOps tax amount in base currency by tenant, provider, and base currency",
    ["tenant_id", "provider", "base_currency"],
)

FINOPS_LEDGER_EXPORTS_TOTAL = Counter(
    "agent_finops_ledger_exports_total",
    "Total FinOps cost ledger exports by tenant and format",
    ["tenant_id", "format"],
)

FINOPS_BUDGET_VIOLATIONS_TOTAL = Counter(
    "agent_finops_budget_violations_total",
    "Total FinOps budget violations by tenant, scope, threshold type, and decision",
    ["tenant_id", "scope", "threshold_type", "decision"],
)

FINOPS_PRICING_IMPORTS_TOTAL = Counter(
    "agent_finops_pricing_imports_total",
    "Total FinOps pricing catalog import runs by mode and status",
    ["mode", "status"],
)

FINOPS_BURN_RATE_ALERTS_TOTAL = Counter(
    "agent_finops_burn_rate_alerts_total",
    "Total FinOps burn-rate alerts by tenant, scope, and burn status",
    ["tenant_id", "scope", "burn_status"],
)

FINOPS_TOKEN_LEASES_TOTAL = Counter(
    "agent_finops_token_leases_total",
    "Total FinOps pre-execution token leases by tenant and action",
    ["tenant_id", "action"],
)

QUOTA_DECISIONS_TOTAL = Counter(
    "agent_quota_decisions_total",
    "Total quota check decisions by tenant, scope, and decision",
    ["tenant_id", "scope", "decision"],
)

TOOL_BREAKER_EVENTS_TOTAL = Counter(
    "agent_tool_breaker_events_total",
    "Total tool circuit breaker events by tool name and action",
    ["tool_name", "action"],
)

MCP_GATEWAY_REQUESTS_TOTAL = Counter(
    "agent_mcp_gateway_requests_total",
    "Total Safe MCP Gateway preflight decisions by MCP server, transport, trust tier, and decision",
    ["mcp_server_id", "transport", "trust_tier", "decision"],
)

MCP_SCHEMA_VIOLATIONS_TOTAL = Counter(
    "agent_mcp_schema_violations_total",
    "Total MCP tool argument schema and injection violations",
    ["tool_name", "mcp_server_id", "violation_type"],
)

MCP_ARGUMENT_REDACTIONS_TOTAL = Counter(
    "agent_mcp_argument_redactions_total",
    "Total sensitive MCP tool argument redactions",
    ["tool_name", "redaction_type"],
)

# v1.5.8 evidence-based evaluation & post-execution review queue (signals are monitor-only)
EVALUATION_FINDINGS_TOTAL = Counter(
    "agent_evaluation_findings_total",
    "Total evaluation findings by category, detector, severity and mode",
    ["category", "detector", "severity", "mode"],
)

EVALUATION_OUTCOMES_TOTAL = Counter(
    "agent_evaluation_outcomes_total",
    "Total agent outcome evidence submissions by provenance and verdict",
    ["provenance", "verdict"],
)

EVALUATION_REVIEWS_TOTAL = Counter(
    "agent_evaluation_reviews_total",
    "Total post-execution review decisions by label",
    ["label"],
)

EVALUATION_REVIEW_QUEUE_DEPTH = Gauge(
    "agent_evaluation_review_queue_depth",
    "Open post-execution reviews by priority",
    ["priority"],
)

# v1.5.9 labeled benchmark, honeypots and CI attestation verification (signals are monitor-only)
EVALUATION_BENCHMARK_TPR = Gauge(
    "agent_evaluation_benchmark_tpr",
    "Latest benchmark TPR at a target FPR by scope (kind or category) and mode",
    ["scope", "fpr_target", "mode"],
)

EVALUATION_BENCHMARK_AUROC = Gauge(
    "agent_evaluation_benchmark_auroc",
    "Latest benchmark AUROC by scope (kind or category) and mode",
    ["scope", "mode"],
)

EVALUATION_BENCHMARK_SAMPLES = Gauge(
    "agent_evaluation_benchmark_samples",
    "Active benchmark samples by label and source",
    ["label", "source"],
)

EVALUATION_ATTESTATIONS_TOTAL = Counter(
    "agent_evaluation_attestations_total",
    "CI attestation verification results by status",
    ["status"],
)

EVALUATION_HONEYPOT_RESULTS_TOTAL = Counter(
    "agent_evaluation_honeypot_results_total",
    "Honeypot probe outcomes",
    ["outcome"],
)

# v1.5.10 advisory judge triage and quota-capped review sampling (signals are monitor-only)
EVALUATION_JUDGE_ASSESSMENTS_TOTAL = Counter(
    "agent_evaluation_judge_assessments_total",
    "Judge triage assessments by primary provider and status",
    ["provider", "status"],
)

EVALUATION_JUDGE_LATENCY_SECONDS = Histogram(
    "agent_evaluation_judge_latency_seconds",
    "Judge primary-provider latency in seconds",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
)

EVALUATION_JUDGE_TOKENS_TOTAL = Counter(
    "agent_evaluation_judge_tokens_total",
    "Judge tokens by provider and direction (input/output)",
    ["provider", "direction"],
)

EVALUATION_REVIEW_SAMPLING_TOTAL = Counter(
    "agent_evaluation_review_sampling_total",
    "Review sampling decisions for rule-passed runs by slice, decision and skip reason",
    ["slice", "decision", "reason"],
)

EVALUATION_TRIAGE_PRECISION_AT_K = Gauge(
    "agent_evaluation_triage_precision_at_k",
    "Latest benchmark precision@K by ranking (rule_only / judge_only / combined)",
    ["ranking", "k"],
)

# v1.6.0 D6: span attributes dropped by the allowlist (never exported)
TRACING_DROPPED_ATTRIBUTES_TOTAL = Counter(
    "tracing_dropped_attributes_total",
    "Span attributes dropped because their key is not on the allowlist.",
)
