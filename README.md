# RewardHacking: Autonomous Agent Guardrail & Governance Control Plane

[![CI](https://github.com/leo459028/RewardHacking/actions/workflows/ci.yml/badge.svg)](https://github.com/leo459028/RewardHacking/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-teal.svg)](https://fastapi.tiangolo.com)
[![Code Style](https://img.shields.io/badge/Code%20Style-Ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](docker-compose.yml)

A production-grade, open-source runtime safety guardrail, human-in-the-loop governance control plane, and FinOps cost ledger designed specifically for **autonomous AI agents** (coding agents, DevOps bots, data query agents, and workflow automations).

---

## What Problem Does This Solve?

Modern AI agents do not merely generate text—they read repositories, mutate code, invoke tools, run system commands, and trigger infrastructure deployments. When autonomous agents are tasked with complex goals, they frequently exhibit **Reward Hacking (Specification Gaming)**:
- **Shortcut Taking & False Verification**: Declaring tasks "completed" while secretly bypassing tests or disabling linters.
- **Rule Evasion & Policy Bypass**: Exploiting prompt ambiguity or using obfuscated phrasing (such as classical Chinese phrasing or nested system overrides) to evade safety filters.
- **Tool Misuse & Destructive Actions**: Calling unauthorized tools, touching production databases, deleting files, or triggering unsanctioned payments.
- **Lack of Auditing & Traceability**: Missing forensic audit trails of requests, policy decisions, step executions, and evaluator signals.
- **Runaway Token Costs**: Infinite loops or inefficient tool-calling sprees that rapidly deplete API budgets.

**RewardHacking** establishes a defense-in-depth security perimeter around AI agents: **verifying before execution**, **constraining during execution**, and **evaluating and auditing after execution**.

---

## Current Release: `v1.5.10`

### V1.5.10 LLM-as-Judge Triage (Shadow) & Quota-Capped Review Sampling (Current)
`v1.5.10` adds the first LLM component, but only where it cannot weaken safety: a judge may reorder human review work, never decide it. It also starts sampling rule-passed runs, so the miss rate can finally be estimated instead of bounded.
- **Pluggable, Trusted Judges**: `deterministic` (default, reproducible, also the fallback) and `openai_compatible` (hosted or local servers such as vLLM / Ollama) providers. Evidence is re-redacted and bounded before any call, wrapped in a per-call nonce boundary, and the reply is parsed against a strict JSON schema that drops every field except `suspicion`, `categories`, `rationale` and `evidence_refs`. External endpoints need `EVALUATION_JUDGE_ALLOW_EXTERNAL=true`; timeouts, errors and invalid output fall back to the deterministic judge. A same-model-family warning flags judges that may share the monitored agent's blind spots. Promotes `httpx` to a runtime dependency.
- **Triage Only (Shadow by Default)**: judge output becomes `triage_priority` (`P1`–`P3`, never `P0`) and `triage_score` on the review (`judge_assessments`, `jdg_`). The judge never writes verdicts, review priority, review status or run status, and never takes part in hard-deny decisions. The advisory queue order `(min(rule, triage), rule, -triage_score)` keeps every rule `P0` first. `EVALUATION_JUDGE_MODE=shadow` records and shows triage without changing the default order; `triage` makes it the default and is a manual switch. Judge tokens go to the FinOps ledger (`step_id=evaluation_judge`) without touching token leases, under a per-tenant daily token cap.
- **Quota-Capped `sample_for_review`**: every rule-passed outcome gets one recorded decision (`review_sampling_decisions`, `smp_`). A uniform random audit slice (server-side HMAC draw) yields an unbiased miss-rate estimate (`random_audit_hacking_rate` in cheat rates); a threshold slice pulls in shadow-detector hits such as `special_casing` and high pre-execution risk so humans label them. Per-tenant daily caps and a maximum share of passed volume apply; skipped candidates are counted, never dropped. Sampling only makes runs stricter (`completed` → `review_required`, `P3`) and opens no incident.
- **Measured Before Promotion**: benchmark runs accept `judge_provider` and report precision@K and audit-budget curves for `rule_only`, `judge_only` and `combined` rankings. On the seed corpus the deterministic judge matches rule precision@5/10/20 (1.0 / 0.9 / 0.9) and catches slightly more reward tampering in the first 1–5% of the budget; promotion criteria stay unmet because seed scores are self-consistency only.
- **Dashboard & Operations**: Triage column, rule/triage order switch and Sampling panel on `/dashboard/reviews`; Judge Assessments panel with *Re-assess* on review detail; Judge Triage block on run detail; triage precision@K and random-audit rates on `/dashboard/benchmark`; advisory `evaluation_judge_health` and `evaluation_sampling_health` readiness checks; five new `agent_evaluation_*` metrics. Roles that submit agent work never see sampling reasons or judge output. Alembic head `202611090001`. Details: [`docs/evaluation.md`](docs/evaluation.md).

### Release History & Evolutionary Context

- **`v1.5.9` (Historical)**: Labeled Benchmark & Detector Measurement. Added the `seed-v1.5.9` labeled corpus (94 samples) and review export (`bms_`), side-effect-free benchmark runs (`bmr_`) with AUROC, TPR at FPR 1% / 5% (Wilson intervals, insufficient-sample flags) and audit-budget curves, the advisory `evaluation_benchmark_gate`, the shadow `special_casing` detector (`SP-001`–`SP-004`), six honeypot probes (`hp_`), monitor-only cheat rates, and DSSE / in-toto CI attestation verification (`CE-009` / `CE-010`).

- **`v1.5.8` (Historical)**: Agent Outcome Ingestion, Evidence-Based Reward-Hacking Detectors & Post-Execution Review Queue. Added `POST /v1/runs/{run_id}/outcome` with bounded, redacted, `self_reported` evidence; the `app/services/evaluation/` engine with taxonomy, claim extraction, versioned `enforce`/`shadow` detectors and `evaluator_version`; the `test_tampering`, `grader_tampering`, `error_suppression`, `claim_evidence` and `scope` detectors; and the `review_required` / `rejected` post-execution review queue that never re-executes runs.
- **`v1.5.7` (Historical)**: Safe MCP Gateway MVP, Tool Schema Guardrails & Per-MCP-Server FinOps Attribution. Added the MCP server registry with trust tiers, transport/external-network guardrails, tenant allow-lists, per-server quotas and breakers, `AgentRequest.tool_arguments` JSON Schema validation with injection scan profiles and recursive redaction, dynamic executor registry closure with worker-side hard-deny re-checks, and `UsageEvent.mcp_server_id` / `mcp_server_breakdown` / `scope="mcp_server"` attribution.
- **`v1.5.6` (Historical)**: FinOps Burn-Rate Forecasting, Dynamic Token Broker, Per-User/Per-Tool Quotas & Interactive Governance Portals. Added burn-rate & exhaustion forecasting (`GET /v1/cost/tenants/{tenant_id}/forecast`), pre-execution token budget leasing (`lease_...`) with sibling-lease accounting, per-user/per-tool quotas, automatic and manual tool circuit breakers, and interactive Tenant/API-Key/Control-Plane/Costs portals.
- **`v1.5.5` (Historical)**: FinOps Settled Ledger Correctness, Multi-Currency FX & Tax Accounting, and Reconciliation Ledger Export. Eliminated estimated/actual double-counting via `_settled_cost_events()`, added multi-currency FX conversion (`fx_rate`, `base_currency_cost`) and tax accounting (`tax_rate`, `tax_inclusive`, `tax_amount`) with pre-tax base-currency budget enforcement, added `cost_center`/`org_id` attribution (`finops_missing_cost_center`), manual reconciliation adjustments (`POST /v1/cost/adjustments`), and RFC 4180 CSV/JSON ledger exports (`GET /v1/cost/tenants/{tenant_id}/export`).
- **`v1.5.4` (Historical)**: Model-Aware FinOps Routing, Step-Level Execution Audit Closure & Interactive Operations Recovery Portal. Added dynamic `provider`/`model_name`/`pricing_mode`/`project_id` catalog routing and re-pricing, `user`/`project`/`model` budget scope isolation, `dry_run_only` enforcement, `failed_retry` cost accounting, automatic `ExecutionPlanStep` lifecycle synchronization (`healthy` plan health), and interactive Run Replay/Resume, Worker DLQ Requeue/Resolve, and Stale Approval TTL sweep endpoints and UI controls.
- **`v1.5.3` (Historical)**: Active FinOps Budget Enforcement, Context-Aware Pricing Engine & FinOps Governance Portal. Added pre-execution and runtime budget enforcement (`block`, `require_approval`, `degrade_model`, `warn_only`), context-tier pricing resolution, cache/batch savings calculation, `/dashboard/admin/finops/budgets` governance portal, violation triage lifecycle, FinOps Prometheus metrics, and zero-warning Pydantic V2 serialization.

- **`v1.5.2` (Historical)**: Open-Source Readiness, Full English Internationalization, Apache-2.0 licensing, community governance standards (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`), and automated secret scanning.
- **`v1.5.1` (Historical)**: Added official model pricing catalog seed ingestion (`app/seeds/model_pricing_seed.yaml`), dry-run validation, versioned pricing change logs, source document audit trails, and Admin FinOps pricing management (`/dashboard/admin/finops/pricing`).
- **`v1.5.0` (Historical)**: Introduced Agent FinOps Core / Cost Ledger (`pricing_catalog`, `usage_events`, `cost_events`, `budget_policies`, `budget_violations`), cost tracking across runs and tenants, and `/dashboard/costs`.
- **`v1.4.0` (Historical)**: Introduced Checkpoint / Resume / Replay MVP, enabling submit checkpoints, resume checkpoints with worker re-dispatch, and safe dry-run replay attempts.
- **`v1.3.0` (Historical)**: Added Trace & Replay Readiness, propagating `X-Trace-Id` to execution plans and step audits, and generating reproducible replay bundles.
- **`v1.2.0` (Historical)**: Introduced the Control Plane skeleton with execution plans, tool definitions, and workflow registries (`/dashboard/control-plane`).
- **`v1.1.0` (Historical)**: Added Classical Chinese Prompt Guard, providing deterministic detection against prompt injections and destructive commands packaged in classical/literary Chinese text.
- **`v1.0.0` (Historical)**: Established production readiness baseline with operations gate checklist, safety regression evidence, and red-team corpus regression suites.
- **`v0.6.0` - `v0.8.0` (Historical)**: Multi-tenant governance center, RBAC-lite API keys, Redis tenant quota enforcement, and production operations gate (`/dashboard/operations`).
- **`v0.1.0` - `v0.5.0` (Historical)**: Initial architecture, asynchronous Kafka worker queues, retry/DLQ, Redis run states, human approval TTL gates, and bounded executor transcripts.

---

## System Architecture

```mermaid
flowchart TD
    Client["Client / Agent Runtime"] -->|POST /v1/agent/submit| API["FastAPI Ingress Gateway"]
    
    subgraph ControlPlane["Control Plane & Safety Boundary"]
        API --> Auth["Auth & Tenant Quota Check (Redis)"]
        Auth --> Sanitizer["Prompt Sanitizer & Classical Chinese Guard"]
        Sanitizer --> Scorer["Multi-Factor Risk Scorer"]
        Scorer --> Policy["Deterministic Policy Engine"]
        
        Policy -->|High Risk / Destructive| Gate["Human Approval Gate (TTL Queue)"]
        Policy -->|Deny| Incident["Incident Logger & Audit Trail"]
        Policy -->|Allow| Plan["Execution Plan Generator"]
    end
    
    subgraph ExecutionPlane["Asynchronous Bounded Execution"]
        Plan --> KafkaQueue["Kafka Bus: agent.requests"]
        KafkaQueue --> Worker["Guarded Background Worker"]
        Worker --> Checkpoint["Run Checkpoint (Submit / Resume)"]
        Worker --> Executor["Bounded Executor (Sandbox Simulation)"]
        Executor --> CircuitBreaker["Tool Circuit Breaker & Quota (Redis)"]
    end
    
    subgraph AuditAndFinOps["Audit, Evaluation & FinOps"]
        Executor --> Evaluator["Task Evaluator & Fake LLM Judge"]
        Evaluator --> Postgres[("PostgreSQL Audit Trail & Event Store")]
        Evaluator --> FinOps["FinOps Cost Ledger (Usage & Cost Events)"]
        Gate -.->|Reviewer Decision| Worker
    end

    subgraph Operations["Operational Dashboard (Jinja2)"]
        Postgres --> Dashboard["/dashboard (Overview, Runs, Approvals, Costs, Operations)"]
    end
```

---

## Core Capabilities

1. **Deterministic Policy Engine & Risk Scoring**
   - Evaluates task type, requested tools, prompt flags, and operational constraints.
   - Calculates weighted risk scores and classifies requests into `allow`, `escalated` (awaiting human approval), or `denied`.
2. **Adversarial Prompt Defense (Classical Chinese Guard)**
   - Specialized detector for literary/classical Chinese adversarial jailbreaks, credential harvesting, or instructions disguised in archaic syntax.
3. **Human-in-the-Loop Approval Workflow**
   - Time-to-Live (TTL) protected approval queues (`/dashboard/approvals`).
   - Supports `approve_once`, `deny`, and `request_changes` with reviewer identity and audit trails.
4. **Asynchronous Bounded Execution & Worker DLQ**
   - Decoupled via Kafka (`agent.requests`).
   - Retries with exponential backoff and Dead Letter Queue (DLQ) tracking (`/dashboard/worker`).
5. **Traceability, Replay Bundles & Checkpoints**
   - End-to-end `X-Trace-Id` propagation across requests, execution plans, and step logs.
   - Self-contained Replay Bundles (`GET /v1/runs/{run_id}/replay-bundle`) for independent audits.
6. **Multi-Tenant Governance Center**
   - Database-backed tenant management, hashed API keys, and RBAC-lite roles (`viewer`, `operator`, `admin`, `finops_admin`).
   - Redis-enforced per-tenant request and high-risk action quotas.
7. **Agent FinOps Core & Cost Accounting**
   - Standardized pricing catalog supporting token types, context tiers, tool calls, and worker runtimes.
   - Immutable usage and cost ledger preventing cost revisions from retroactively mutating past runs.
8. **Production Operations Gate**
   - Pre-deployment checklist (`/dashboard/operations`) verifying backup drills, secret rotations, DLQ drain status, and safety regression evidence.

---

## Target Use Cases (Post-Roadmap)

When fully implemented along the roadmap, this system is designed to support:
1. **AI Coding Agents**: Verifying patches before disk mutation; blocking attempts to skip tests or tamper with CI/CD scripts.
2. **Enterprise Internal Ops Agents**: Enforcing least-privilege tool execution and requiring manager approvals for bulk operations.
3. **Data Query Agents**: Detecting unauthorized PII queries, excessive export payloads, or schema tampering.
4. **Customer Support Bots**: Halting account changes, refunds, or credential modifications pending operator review.
5. **DevOps & Cloud Automation**: Enforcing mandatory approval gates and audit logs prior to infrastructure modification or deletion.
6. **AI Safety Research & Benchmarking**: Providing an extensible testbed for measuring specification gaming and jailbreak defenses.

---

## Non-Applicable Scenarios & Limitations

### Where This Project Is NOT Suitable
- Pure chatbot applications without tool calls, file access, or external actions.
- Full replacement for enterprise cybersecurity suites (WAF, EDR, SIEM, DLP, or IAM).
- Standalone autonomous agent runtime (this is a **guardrail & governance layer**, not an agent framework itself).
- Immediate unmonitored production deployment without human operators or network boundaries.

### Current Architectural Limitations
- **Bounded Executor Sandbox**: The current bounded executor simulates tool invocation and records structured transcripts; it does not yet isolate untrusted processes in a microVM or isolated container sandbox.
- **Deterministic Evaluator**: Evidence detectors are deterministic rules over agent-reported (`self_reported`) or CI-attested evidence; they can be evaded deliberately and should not be treated as a single source of infallible truth. The `v1.5.9` benchmark measures their miss and false-positive rates (seed scores reflect rule self-consistency, not real-world rates). The `v1.5.10` LLM judge only reorders review work and can itself be manipulated; it never passes a run. Platform-verified evidence (`platform_verified`) arrives with the `v1.6.1` sandbox.
- **Fail-Safe Caveat**: Guardrails significantly mitigate risk, but cannot mathematically eliminate all zero-day jailbreaks or sophisticated prompt hacking.

---

## Quickstart (60 Seconds)

### 1. Configure Environment
```bash
cp .env.example .env
```
*(The default `.env.example` is pre-configured for local testing out of the box).*

### 2. Launch Stack with Docker Compose
```bash
docker compose up -d --build
```
This boots the complete stack:
- **API Gateway**: `http://localhost:8000`
- **PostgreSQL**: `localhost:5432`
- **Redis**: `localhost:6379`
- **Kafka & Zookeeper**: `localhost:9092`
- **Background Worker**: Consuming queued runs

### 3. Verify Health & Readiness
```bash
curl -f http://localhost:8000/v1/healthz
curl -f http://localhost:8000/v1/readyz
```

### 4. Explore the Operational Dashboard
Open your web browser and navigate to:
```text
http://localhost:8000/dashboard
```

### 5. Submit a Guarded Agent Request
Submit a low-risk task that passes automatically:
```bash
curl -X POST http://localhost:8000/v1/agent/submit-with-details \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "default",
    "user_id": "eng-001",
    "task_type": "code_fix",
    "prompt": "Fix payment retry logic without modifying core business rules.",
    "goal": "Generate a verified patch",
    "allowed_tools": ["read_repo", "write_patch", "run_tests"],
    "constraints": {
      "no_external_network": true,
      "requires_human_approval": false,
      "max_tool_calls": 8
    }
  }'
```

Now submit a high-risk destructive action that will be intercepted by the policy engine:
```bash
curl -X POST http://localhost:8000/v1/agent/submit-with-details \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "default",
    "user_id": "eng-001",
    "task_type": "infra_deploy",
    "prompt": "Drop old database tables and wipe test environment immediately.",
    "goal": "Purge storage",
    "allowed_tools": ["execute_shell", "drop_table"],
    "constraints": {
      "requires_human_approval": true
    }
  }'
```
Notice that the second request returns status `awaiting_approval` and appears in the [Approvals Dashboard](http://localhost:8000/dashboard/approvals) for human sign-off!

---

## Documentation Navigation

Comprehensive technical documentation is maintained under the [`docs/`](docs/) directory:

- [Project Overview & Design Principles](docs/OVERVIEW.md)
- [System Architecture & Dataflow](docs/architecture.md)
- [REST API Specification](docs/api.md)
- [Operational Dashboard Guide](docs/dashboard.md)
- [Database Schema & Audit Models](docs/data_model.md)
- [Threat Modeling & Security Design](docs/security.md)
- [Configuration & Environment Variables](docs/configuration.md)
- [Deployment & Production Hardening](docs/deployment.md)
- [Developer Guide & Local Setup](docs/development.md)
- [Testing Guide & Red-Team Corpus](docs/testing_guide.md)
- [Manual QA & Browser Testing Guide](docs/manual_test.md)
- [Production Runbooks & Incident Response](docs/runbook.md)
- [Observability, Metrics & SLOs](docs/observability.md)
- [Kafka Topics & Event Contracts](docs/kafka_topics.md)
- [Redis Keyspace & Rate Limiting](docs/redis_keys.md)
- [Database Migrations Guide](docs/migrations.md)
- [Project Roadmap](docs/roadmap.md)
- [Agent FinOps Core Reference](docs/finops_core.md)
- [Evidence-Based Evaluation, Benchmark & Detector Rules](docs/evaluation.md)
- [Testing & Release Verification Report](docs/test_report.md)

---

## Community & Contributing

We welcome contributions! Please review our:
- [Contributing Guidelines](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security Policy](SECURITY.md)

## License

This project is licensed under the [Apache License, Version 2.0](LICENSE).
