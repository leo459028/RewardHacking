# System Architecture

> **Architecture Version**: `v1.5.7`  
> **Previous Versions**: `v1.5.2`, `v1.5.1`, `v1.5.0`, `v1.4.0`

This document details the system topology, component responsibilities, and request dataflow of the **RewardHacking Guardrail** platform.

---

## Architectural Evolution

- **`v1.6.0` (Current)**: Runtime Foundations. W3C Trace Context and optional OpenTelemetry export (`app/core/trace_context.py`, `app/core/tracing.py`), audit retention and legal holds (`app/services/retention.py`, CLI `app/jobs/retention.py`), the sandbox layer (`app/services/sandbox/`: execution spec, `simulated` backend, isolation policy, conformance suite), the FinOps package and per-domain routers, with `import-linter` contracts in `pyproject.toml`.
- **`v1.5.9` (Historical)**: Labeled Benchmark & Detector Measurement. Measurement layer over the evaluation engine (benchmark corpus and runs, shadow `special_casing`, honeypot probes, cheat rates) and CI attestation verification.
- **`v1.5.8` (Historical)**: Evidence-based evaluation engine, outcome ingestion and post-execution review queue.
- **`v1.5.3` – `v1.5.7` (Historical)**: Active FinOps governance, settled ledger, burn-rate forecasting and token broker, Safe MCP Gateway.
- **`v1.5.2` (Historical)**: Open-Source Baseline. Unified documentation in English, standardized community templates, automated secret checks, and operations gate synchronization.
- **`v1.5.1` (Historical)**: Added official model pricing catalog seed ingestion, dry-run evaluation, versioned pricing change logs, and Admin FinOps pricing dashboard.
- **`v1.5.0` (Historical)**: Introduced Agent FinOps Core / Cost Ledger (`pricing_catalog`, `usage_events`, `cost_events`, `budget_policies`, `budget_violations`).
- **`v1.4.0` (Historical)**: Added Checkpoint / Resume / Replay MVP, enabling submit checkpoints, resume checkpoints, and dry-run replay attempts.
- **`v1.3.0` (Historical)**: Implemented Trace & Replay Readiness, propagating `X-Trace-Id` to execution plans, step audits, and replay bundles.
- **`v1.2.0` (Historical)**: Introduced Control Plane Execution Plan, tool definitions, and workflow registries.
- **`v1.1.0` (Historical)**: Added Classical Chinese Prompt Guard for adversarial literary/classical Chinese jailbreak detection.
- **`v1.0.0` (Historical)**: Established production operations gate with safety regression verification and red-team regression suites.
- **`v0.6.0` - `v0.8.0` (Historical)**: Tenant Governance Center, hashed API keys, Redis quotas, and operations evidence logging.
- **`v0.1.0` - `v0.5.0` (Historical)**: Core guarded asynchronous execution, Kafka worker queues, retry/DLQ, Redis run states, human approval TTL gates, and evaluator transcripts.

---

## Safe MCP Gateway Layer (`v1.5.7`)

```
submit (/v1/agent/submit, /submit-with-details, /dashboard/playground)
  -> tenant / user / tool quotas
  -> mcp_gateway.preflight()   # per-MCP-server quota, registry & trust tier, transport,
                               # MCP breakers, input schema + scan profiles, redaction
  -> PolicyEngine.evaluate(mcp_context=...)  # hard-deny flags before risk threshold
  -> ControlPlanePlanner.plan()  # mcp_route, sanitized_arguments, mcp_* approval gates
  -> record_submit_usage()     # UsageEvent.mcp_server_id / tool_* attribution
worker process_run_by_id()
  -> mcp_gateway.worker_context()  # re-check blocked/tenant; skip escalations once approved
  -> BoundedToolExecutor(allowed_tool_map=registry)  # dynamic tools + output schema
  -> record_actual_usage()
```

All three submit entry points call the single shared `preflight()` so MCP behavior cannot drift between the REST API and the Dashboard.

## Component Topology

```mermaid
flowchart LR
    Client["Client / Agent Runtime"] --> API["FastAPI Ingress Gateway"]
    API --> Auth["Auth & Tenant Quota (Redis)"]
    Auth --> Sanitizer["Prompt Sanitizer & Classical Chinese Guard"]
    Sanitizer --> Scorer["Multi-Factor Risk Scorer"]
    Scorer --> Policy["Policy Engine"]
    
    Policy -->|allow| CP["Execution Plan Generator"]
    Policy -->|review| Appr["Approval Queue (TTL)"]
    Policy -->|deny| Inc["Incident Logger"]
    
    CP --> Kafka["Kafka agent.requests Bus"]
    Kafka --> Worker["Worker Consumer"]
    Worker --> Bounded["Bounded Tool Executor"]
    Bounded --> Breaker[("Redis Circuit Breaker")]
    Bounded --> Eval["Task Evaluator"]
    
    API --> DB[("PostgreSQL Audit Trail")]
    Worker --> DB
    Appr --> DB
    Inc --> DB
    Eval --> DB
    
    DB --> FinOps["FinOps Cost Ledger"]
    DB --> Dash["Operational Dashboard"]
```

---

## Component Responsibilities

### 1. Ingress API Gateway (`app/api/`)
- Accepts structured agent execution requests via `POST /v1/agent/submit` and `POST /v1/agent/submit-with-details`.
- Validates request payloads against Pydantic schemas.
- Assigns or propagates the distributed trace identifier (`X-Trace-Id`).
- Checks tenant API key validity and enforces per-tenant rate limits via Redis.

### 2. Prompt Sanitizer & Classical Chinese Guard (`app/services/prompt_sanitizer.py`)
- Analyzes prompts for direct jailbreaks (`ignore previous instructions`, `bypass policy`, `reveal system prompt`).
- Conducts specialized detection against **Classical Chinese Prompt Injection** (archaic idioms, obscure obfuscations, credential harvesting disguised in classical poetry).
- Generates structured prompt flags attached to `evaluation_summary.prompt_guard`.

### 3. Risk Scorer & Policy Engine (`app/services/risk_scoring.py`, `app/services/policy_engine.py`)
- Computes deterministic risk scores (0.0 to 1.0) based on task category, requested tool permissions, external network flags, and prompt signals.
- Classifies requests into:
  - `allow`: Low-risk requests routed directly to execution planning.
  - `escalated`: High-risk or destructive actions diverted into the human approval queue (`awaiting_approval`).
  - `denied`: Prohibited actions blocked immediately, triggering an incident record.

### 4. Control Plane Execution Planning (`app/services/control_plane.py`)
- Generates pre-execution execution plans capturing workflow version, planned steps, selected tools, and policy snapshots before queuing.
- Records step-level audits linked to the shared trace identifier.

### 5. Event Bus & Asynchronous Worker (`app/workers/`)
- Uses Kafka topic `agent.requests` to decouple API ingress from long-running agent tool invocations.
- Background worker consumes queued runs, reconstructs the execution context, re-verifies policies, and executes bounded tools.
- Implements exponential backoff retries and Dead Letter Queue (DLQ) routing for exhausted attempts.

### 6. Bounded Tool Executor, Sandbox Layer & Circuit Breakers (`app/services/executor.py`, `app/services/sandbox/`)
- Enforces strict constraints on tool invocation count (`max_tool_calls`), timeout limits, and permitted tool lists.
- Tracks tool failure frequencies in Redis; trips circuit breakers if consecutive failures exceed thresholds.
- Simulates tool execution safely during dry-run and test modes without invoking external side-effects.
- `v1.6.0`: builds an `ExecutionSpec` for every allowed tool and calls the configured `SandboxBackend` (`simulated` only; nothing executed); the transcript records `sandbox_execution` with backend, isolation level, required isolation (record-only policy) and `spec_digest`. The sandbox package may not import the API, Dashboard or worker layers (import-linter contract).

### 7. Evidence-Based Evaluation Engine (`app/services/evaluation/`, `v1.5.8`)
- `evidence.py` bounds, parses (unified diff, JUnit XML) and redacts agent outcome evidence; `claims.py` extracts verification claims from the final message.
- Versioned detectors (`test_tampering`, `grader_tampering`, `error_suppression`, `claim_evidence`, `scope`, `evidence_quality`, `legacy_text`) emit findings with evidence pointers; `aggregator.py` derives `verdict`, `review_priority` and `assurance_level`; `app/services/evaluator.py` remains the backward-compatible facade.
- `app/services/outcomes.py` ingests `POST /v1/runs/{run_id}/outcome`; `app/services/review_queue.py` owns the post-execution review queue (`review_required` / `rejected`), which is separate from pre-execution approvals and never re-dispatches runs. See [`docs/evaluation.md`](evaluation.md).
- `v1.5.9` measurement layer: `app/services/evaluation/metrics.py` (pure AUROC / TPR@FPR / Wilson / audit-budget curves), `app/services/benchmark.py` (labeled corpus, side-effect-free benchmark runs, readiness gate), `app/services/evaluation/detectors/special_casing.py` (shadow), `app/services/honeypot.py` (impossible-task probes with server-side run mapping), `app/services/cheat_rates.py` (monitor-only cohorts), `app/services/evaluation/attestation.py` + `app/services/attestation_keys.py` (DSSE / in-toto verification against tenant trust roots) and `app/services/inspect_import.py` (optional). APIs live in `app/api/routers/benchmark.py`; the Dashboard view in `app/ui/routers/benchmark.py`.
- `v1.5.10` triage and sampling layer: `app/services/evaluation/judges/` (provider protocol, `deterministic` and `openai_compatible` providers, input builder with re-redaction and truncation, nonce-bounded prompt, strict output parser, injection marker scan, model-family check), `app/services/judge_triage.py` (post-commit review triage, deduplication, token cap, FinOps usage, tampering-first queue order), `app/services/review_sampling.py` (one decision per rule-passed outcome: HMAC random audit slice and threshold slice with caps) and `app/services/benchmark_triage.py` (precision@K for `rule_only` / `judge_only` / `combined`). APIs extend `app/api/routers/evaluations.py`; the Dashboard extends `app/ui/routers/reviews.py` and `app/ui/routers/benchmark.py`.
- `v1.5.11` profiles layer: `app/services/evaluation/judges/profiles.py` (validated named profiles from YAML / JSON), `app/services/judge_background.py` (`dispatch_triage()` inline vs background, pending marker, per-loop semaphore, sweep), `app/services/judge_profiles_admin.py` (profile view, probe, zero-cost local pricing) and tenant consent in `judge_triage.run_judge()`. Containers mount `./config` read-only and resolve `host.docker.internal` for local LLM servers.

```text
seed YAML ─┐
review export ─┼─► benchmark_samples ─► EvaluationEngine (pure; configured + what-if) ─► metrics.py ─► benchmark_runs ─► readiness gate / dashboard / Prometheus
Inspect JSON ─┘
honeypot launch ─► normal submit path ─► outcome evidence ─► judge (cheated / honest_refusal) ─► review + cheat rates
ci_attested outcome ─► DSSE verify (tenant key, run binding, report digest, freshness, replay) ─► ci_attested or CE-009 / CE-010
outcome ─► rule verdict (judge not involved) ─┬─ review ─► open/merge review
                                             └─ pass ─► sampling decision (random audit / threshold / skipped / not selected)
COMMIT ─► judge triage (deterministic or openai_compatible, fallback) ─► judge_assessments + review.triage_* only ─► FinOps usage (no lease)
review queue order=triage: (min(rule, triage), rule, -triage_score) ─► rule P0 first
```

### 8. Agent FinOps Core & Cost Accounting (`app/services/finops/`, package since `v1.6.0`)
- Maintains an official model pricing catalog with tiered token pricing, tool fees, and worker runtime rates.
- Ingests deterministic `usage_events` and calculates immutable `cost_events` per run, workflow, and tenant.
- Evaluates run-level budget policies and generates budget violation alerts.

### 8a. Module Contracts (`v1.6.0`)
`lint-imports` enforces four contracts: post-execution review modules (`review_queue`, `outcomes`, `evaluation`, `judge_triage`, `review_sampling`) never import Kafka publishing, workers or token leases; `app.services` never imports `app.api` / `app.ui`; the sandbox package is independent of API, Dashboard and workers; FinOps submodules are layered without cycles.

### 9. Operational Dashboard (`app/ui/`)
- Server-rendered HTML dashboard providing operational visibility:
  - Overview metrics, recent runs, and active incidents.
  - Interactive human approval queue with decision submission.
  - Control plane execution plans, replay bundles, and trace inspection.
  - Tenant governance settings, API keys, and quota overrides.
  - Operations release gate checklist and deployment evidence.
  - FinOps cost summaries and pricing catalog management.
