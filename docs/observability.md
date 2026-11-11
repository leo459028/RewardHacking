# Observability, Metrics & SLOs

> **Observability Baseline**: `v1.6.0` (W3C Trace Context, optional OpenTelemetry export)
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
| `agent_prompt_guard_flags_total`| Counter | `flag` | Trigger counts for prompt-guard flags (English patterns, `classical_*`, `zh_*`, `sensitive_term_mention`, `classical_style_task_mismatch`) |
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
| `agent_evaluation_findings_total` | Counter | `category`, `detector`, `severity`, `mode` | `v1.5.8` evidence-based evaluation findings (monitor-only signals) |
| `agent_evaluation_outcomes_total` | Counter | `provenance`, `verdict` | Outcome evidence submissions (`self_reported`, `ci_attested`) |
| `agent_evaluation_reviews_total` | Counter | `label` | Post-execution review decisions (`confirmed_hacking`, `false_positive`, `accepted`, `needs_info`) |
| `agent_evaluation_review_queue_depth` | Gauge | `priority` | Open post-execution reviews by priority (`P0`–`P3`), refreshed on queue reads and decisions |
| `agent_evaluation_benchmark_tpr` | Gauge | `scope`, `fpr_target`, `mode` | `v1.5.9` latest benchmark TPR at a target FPR (`scope` = `overall`, kind or category; `mode` = `as_configured` \| `all_enforced`) |
| `agent_evaluation_benchmark_auroc` | Gauge | `scope`, `mode` | Latest benchmark AUROC |
| `agent_evaluation_benchmark_samples` | Gauge | `label`, `source` | Active benchmark samples, refreshed when samples are listed |
| `agent_evaluation_attestations_total` | Counter | `status` | CI attestation verification results (`verified`, `invalid_signature`, `replayed`, `result_mismatch`, `unsupported_type`, ...) |
| `agent_evaluation_honeypot_results_total` | Counter | `outcome` | Honeypot probe outcomes (`cheated`, `honest_refusal`, `inconclusive`) |
| `agent_evaluation_judge_assessments_total` | Counter | `provider`, `status` | `v1.5.10` judge triage assessments by primary provider and status (`ok`, `timeout`, `error`, `invalid_output`, `blocked_external`, `budget_exceeded`, `not_configured`) |
| `agent_evaluation_judge_latency_seconds` | Histogram | `provider` | Judge primary-provider latency (observed for network providers) |
| `agent_evaluation_judge_tokens_total` | Counter | `provider`, `direction` | Judge tokens (`input`, `output`) |
| `agent_evaluation_review_sampling_total` | Counter | `slice`, `decision`, `reason` | Sampling decisions for rule-passed runs (`threshold` / `random` / `none`; `sampled` / `skipped` / `not_selected`; skip reason or `none`) |
| `dashboard_logins_total` | Counter | `result` | `v1.6.1` Dashboard sign-in attempts: `success`, `unknown_user`, `wrong_password`, `lockout`, `locked`, `disabled` (internal detail; the sign-in page always shows one generic message) |
| `dashboard_access_denied_total` | Counter | `reason` | `v1.6.1` requests refused by the Dashboard access layer (`sign_in_required`, `password_change_required`, `missing_permission`, `tenant_scope`, `platform_view`, `permission_undeclared`, `csrf_token_invalid`, `reauth_required`, `reauth_failed`). Permission and signed-in CSRF refusals also write an `access.denied` governance event (at most one per session and permission per minute; CSRF uses the permission `csrf`); a refused sign-in form (`csrf_token_invalid` without a session) and re-authentication redirects write none |
| `agent_evaluation_triage_precision_at_k` | Gauge | `ranking`, `k` | Latest benchmark triage precision@K (`rule_only`, `judge_only`, `combined`; `5`, `10`, `20`, `budget_0.01`, `budget_0.05`, `budget_0.10`) |

---

## Distributed Tracing (W3C Trace Context, `v1.6.0`)

### Trace ids and propagation
1. **HTTP ingress**: a valid `traceparent` header ([W3C Trace Context Level 1](https://www.w3.org/TR/trace-context/)) is continued; otherwise a new trace starts. `tracestate` is passed through unchanged (at most 512 bytes). All-zero ids and version `ff` are invalid.
2. **`X-Trace-Id` stays backward compatible**: a client-supplied `X-Trace-Id` is echoed unchanged (v0.8 behaviour) and recorded as the span attribute `rh.legacy_trace_id`; without one, `X-Trace-Id` is `trace_<32 hex>`, the W3C trace id with a prefix, so existing `trace_id` columns map directly to the exported trace. Every response also carries `traceparent`.
3. **Kafka**: producers add `traceparent` (and `tracestate`) message headers; the consumer continues the producer span as its parent (same synchronous run flow). Without headers it recovers the trace id from `BaseEvent.trace_id` (`trace_<32 hex>`), else starts a new trace.
4. **Background judge triage** starts its own root span and **links** back to the triggering span (asynchronous work is not a child, so the request timeline is not stretched).
5. The trace id is stored on `execution_plans`, `execution_plan_steps`, `run_checkpoints`, `replay_attempts` and in the run request metadata. `GET /v1/runs/{run_id}/trace` and `GET /v1/runs/{run_id}/replay-bundle` return the audit timeline; Run Detail shows the trace id and, when `OTEL_TRACE_UI_URL_TEMPLATE` is set (for example `http://localhost:16686/trace/{trace_id}`), an *Open in Jaeger* link.

Propagation works without any optional dependency (`app/core/trace_context.py`).

### OpenTelemetry export (optional)
- Install the extra (`pip install .[otel]` or `requirements-otel.txt`; OpenTelemetry Python `1.45.1`; the Docker image installs it with `INSTALL_OTEL=1`) and set `OTEL_ENABLED=true`. Without the extra, or with `OTEL_ENABLED=false`, every tracing call is a no-op.
- Exporter: OTLP/HTTP only. Endpoint, service name and sampler come from the standard variables `OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`), `OTEL_SERVICE_NAME` and `OTEL_TRACES_SAMPLER`.
- Exported span ids are exactly the ids of the propagated trace context, so `traceparent` headers, database `trace_id` values and the exported trace always agree.
- `app/core/tracing.py` is the only module that imports OpenTelemetry.

### Spans
HTTP server (middleware), `kafka publish` (PRODUCER) / `kafka process` (CONSUMER), `worker process_run`, `policy.decide`, `executor.execute` with one `tool {name}` child per tool and `sandbox.run` below it, `evaluation.evaluate`, `judge triage` with a GenAI child, and `retention.plan` / `retention.apply`. Database queries are not instrumented.

### Attribute allowlist
`set_attributes()` keeps only allow-listed keys: `rh.run_id`, `rh.tenant_id`, `rh.review_id`, `rh.task_type`, `rh.status`, `rh.verdict`, `rh.priority`, `rh.tool_name`, `rh.mcp_server_id`, `rh.legacy_trace_id`, `rh.attempt`, `rh.tool_count`, `rh.judge.*` (profile, status, fallback source), `rh.sandbox.*`, `rh.retention.*`, standard `http.*` / `url.path`, `messaging.*` and the GenAI subset below. Other keys are dropped and counted (`tracing_dropped_attributes_total`; shown on `/dashboard/observability` and `GET /v1/observability/tracing`). Strings are truncated to 256 characters. **Prompts, goals, diffs, test reports, judge input / output and keys are never attached**; a canary test asserts that a secret string sent in a prompt, diff and judge response appears in no span name, attribute or event.

### GenAI semantic conventions (judge calls)
Verified on 2026-11-11 against the **OpenTelemetry GenAI semantic conventions, Client Inference span** (status *Development*), now maintained in [`open-telemetry/semantic-conventions-genai`](https://github.com/open-telemetry/semantic-conventions-genai) (`docs/gen-ai/client-inference.md`, referencing Semantic Conventions `v1.45.0`; the former opentelemetry.io page only points there):

| Item | Specification | RewardHacking |
| :--- | :--- | :--- |
| Span name | `{gen_ai.operation.name} {gen_ai.request.model}` | `chat {model}` |
| Span kind | `CLIENT` | `CLIENT` |
| `gen_ai.operation.name` | Required (`chat`) | `chat` |
| `gen_ai.provider.name` | Required | profile provider (`openai_compatible` or `anthropic`) |
| `gen_ai.request.model` / `gen_ai.response.model` | Conditionally required / recommended | set when known |
| `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` | Recommended | set from the provider usage block |
| `gen_ai.response.finish_reasons` | Recommended | not set (adapters do not return it yet) |
| `error.type` | Conditionally required on error | set by `Span.set_error()` |

Known deviation: for an OpenAI-compatible endpoint the real provider (LM Studio, Ollama, a cloud API) is unknown, so `gen_ai.provider.name` is `openai_compatible` rather than a well-known value. The conventions are still in development; attribute names may need to follow upstream changes.

### Exemplars
When export is enabled, the HTTP request duration and worker histograms attach a `trace_id` exemplar. Exemplars appear only in the OpenMetrics format (`Accept: application/openmetrics-text` on `GET /v1/metrics`); the default text format is unchanged.

### Jaeger (local)
```bash
docker compose --profile observability up -d
```
Set `OTEL_ENABLED=true` (and optionally `OTEL_TRACE_UI_URL_TEMPLATE=http://localhost:16686/trace/{trace_id}`) in `.env`, then `docker compose up -d --build api worker`. The `jaeger` service (`jaegertracing/jaeger:2.22.0`) receives OTLP/HTTP on `4318`; open `http://localhost:16686`. The api and worker appear as two services in the same trace.

### Status endpoint
`GET /v1/observability/tracing` returns `w3c_propagation`, `otel_enabled_setting`, `exporting`, `exporter_host` (host only, never the full endpoint or credentials), `sampler`, `service_name`, `trace_ui_url_template` and `dropped_attributes`.

---

## Service Level Objectives (SLOs)

The system tracks operational compliance against key performance targets visible on `/dashboard/observability`:

| Objective | Target | Measurement Metric |
| --- | --- | --- |
| **Worker Execution Latency** | $< 300\text{ seconds}$ | Time from `queued` to worker `completed` |
| **Incident Response Time** | $< 900\text{ seconds}$ | Time from incident creation to human resolution |
| **Approval Turnaround** | $< 3600\text{ seconds}$ (TTL) | Time awaiting human decision before fail-closed timeout |
| **DLQ Ratio** | $< 0.1\%$ | Ratio of `agent_worker_dlq_total` to total worker jobs |
