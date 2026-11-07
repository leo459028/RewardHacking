# Observability, Metrics & SLOs

> **Observability Baseline**: `v1.5.7`
> **Metrics Engine**: Prometheus Client  
> **Endpoint**: `GET /v1/metrics`

RewardHacking provides deep observability across API gateways, background workers, policy decisions, and FinOps expenditures through structured metrics, distributed trace context propagation, and centralized audit queries.

---

## Prometheus Metrics Reference

| Metric Identifier | Type | Labels | Description |
| --- | --- | --- | --- |
| `http_requests_total` | Counter | `method`, `path`, `status` | Total incoming HTTP API requests |
| `http_request_duration_seconds` | Histogram | `method`, `path`, `status` | Ingress request latency distribution |
| `agent_requests_total` | Counter | `status` | Agent task submissions partitioned by status (`queued`, `awaiting_approval`, `denied`) |
| `agent_policy_risk_score` | Histogram | None | Distribution of computed policy risk scores (0.0 to 1.0) |
| `agent_policy_decisions_total` | Counter | `status` | Decisions categorized by policy outcome |
| `agent_prompt_guard_flags_total`| Counter | `flag` | Trigger counts for prompt injection & Classical Chinese guard flags |
| `agent_control_plane_steps_total`| Counter | `step_type`, `status` | Pre-execution steps generated during planning |
| `agent_worker_jobs_total` | Counter | `status` | Worker task executions partitioned by outcome (`completed`, `failed`) |
| `agent_worker_duration_seconds` | Histogram | None | Execution duration for bounded tool workers |
| `agent_worker_retries_total` | Counter | None | Total worker transient failure retry attempts |
| `agent_worker_dlq_total` | Counter | None | Total exhausted tasks routed to the Dead Letter Queue |
| `agent_evaluator_verdicts_total`| Counter | `verdict` | Evaluation outcome counts (`pass`, `review`) |
| `agent_incidents_total` | Counter | `severity`, `code` | Security incident creation count partitioned by severity |
| `agent_finops_usage_events_total` | Counter | `provider`, `model`, `resource_type`, `phase` | FinOps usage events ingested by provider, model, resource type, and phase |
| `agent_finops_cost_usd_total` | Counter | `tenant_id`, `provider`, `model`, `phase` | Cumulative pre-tax FinOps expenditure normalized to base currency (or USD when `FINOPS_BASE_CURRENCY=USD`) |
| `agent_finops_base_currency_cost_total` | Counter | `tenant_id`, `base_currency`, `source_currency`, `cost_type` | Cumulative settled/recorded FinOps expenditure in base currency (`base_currency_cost`) |
| `agent_finops_tax_total` | Counter | `tenant_id`, `base_currency`, `source_currency` | Cumulative FinOps tax amount converted into base currency |
| `agent_finops_ledger_exports_total` | Counter | `tenant_id`, `format` | Total tenant FinOps ledger export requests (`json`, `csv`) |
| `agent_finops_budget_violations_total` | Counter | `scope_type`, `severity`, `action_taken` | Triggered FinOps budget violations partitioned by scope, severity, and enforcement action |
| `agent_finops_pricing_imports_total` | Counter | `mode`, `status` | Pricing catalog import validations and applications (`dry_run`, `apply`) |
| `agent_finops_burn_rate_alerts_total` | Counter | `tenant_id`, `scope`, `burn_status` | Total burn-rate exhaustion alerts emitted (`elevated_burn`, `critical_burn`, `exhausted`) |
| `agent_finops_token_leases_total` | Counter | `tenant_id`, `action` | Dynamic Token Broker lease lifecycle transitions (`acquired`, `settled`, `released`, `expired`) |
| `agent_quota_rejections_total` | Counter | `tenant_id`, `quota_scope` | Total HTTP 429 rate-limit rejections partitioned by scope (`tenant_minute`, `tenant_high_risk_hour`, `user_minute`, `tool_minute`) |
| `agent_tool_breaker_transitions_total` | Counter | `tool_name`, `state`, `trigger` | Tool circuit breaker state transitions (`open`, `closed`; trigger `auto_worker_failure`, `manual`) |

| `agent_mcp_gateway_requests_total` | Counter | `mcp_server_id`, `transport`, `trust_tier`, `decision` | `v1.5.7` Safe MCP Gateway preflight decisions per server (`allowed`, `escalated`, `denied`) |
| `agent_mcp_schema_violations_total` | Counter | `tool_name`, `mcp_server_id`, `violation_type` | Tool argument violations (`schema_violation`, `injection`, `undeclared_tool`, `payload_too_large`, `type_error`) |
| `agent_mcp_argument_redactions_total` | Counter | `tool_name`, `redaction_type` | Sensitive argument redactions (`field_name`, `value_pattern`, `pii_email`) |

---

## Distributed Tracing (`X-Trace-Id`)

RewardHacking enforces end-to-end trace correlation:
1. Every ingress request extracts or generates an `X-Trace-Id`.
2. The trace ID is bound to:
   - FastAPI request context state.
   - Response header `X-Trace-Id`.
   - `execution_plans.trace_id` and `execution_plan_steps.trace_id`.
   - `run_checkpoints.trace_id` and `replay_attempts.trace_id`.
   - Kafka message metadata.
3. Operators can retrieve the full chronological timeline using `GET /v1/runs/{run_id}/trace` or export the complete bundle via `GET /v1/runs/{run_id}/replay-bundle`.

---

## Service Level Objectives (SLOs)

The system tracks operational compliance against key performance targets visible on `/dashboard/observability`:

| Objective | Target | Measurement Metric |
| --- | --- | --- |
| **Worker Execution Latency** | $< 300\text{ seconds}$ | Time from `queued` to worker `completed` |
| **Incident Response Time** | $< 900\text{ seconds}$ | Time from incident creation to human resolution |
| **Approval Turnaround** | $< 3600\text{ seconds}$ (TTL) | Time awaiting human decision before fail-closed timeout |
| **DLQ Ratio** | $< 0.1\%$ | Ratio of `agent_worker_dlq_total` to total worker jobs |
