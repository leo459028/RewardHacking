# Observability, Metrics & SLOs

> **Observability Baseline**: `v1.5.3`
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
| `agent_finops_cost_usd_total` | Counter | `tenant_id`, `provider`, `model`, `phase` | Cumulative FinOps expenditure in USD partitioned by tenant, provider, model, and phase |
| `agent_finops_budget_violations_total` | Counter | `scope_type`, `severity`, `action_taken` | Triggered FinOps budget violations partitioned by scope, severity, and enforcement action |
| `agent_finops_pricing_imports_total` | Counter | `mode`, `status` | Pricing catalog import validations and applications (`dry_run`, `apply`) |

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
