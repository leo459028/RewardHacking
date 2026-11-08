# Project Documentation Overview

> **Current Version**: `v1.5.12`
> **Previous Version**: `v1.5.11`

This directory contains the comprehensive technical documentation for the **RewardHacking Guardrail** platform.

---

## Release Baseline: `v1.5.12`

### Release Evolution

- **`v1.5.12` (Current)**: Judge Activation & Provider Adapters. The rule-based judge stays the default; any LLM judge needs a successful connection test for its exact configuration fingerprint (and, for Dashboard profiles, an explicit activation; environment-selected profiles are auto-probed at startup). Dashboard profile management (`/dashboard/judge-profiles`, templates, drafts, status labels, key status, editing gate, same-origin forms), `*_API_KEY` allowlist, HTTPS for external hosts and metadata-address blocking, a native Anthropic adapter, per-profile fallback chains, Redis circuit breakers and per-profile daily token caps. Alembic head `202611120001`. See [`docs/evaluation.md`](evaluation.md) §15.
- **`v1.5.11` (Historical)**: Real Local & Cloud Judges. Named judge profiles (YAML / JSON, keys only by environment-variable name), structured-output fallback (`json_schema` → `json_object` → prompt-only), reasoning stripping and `reasoning_effort`, bounded retries, background triage with pending markers and sweep, profile-based cost attribution with zero-cost local pricing, exact-match internal hosts, tenant consent for external judges, multi-profile benchmarks with latency and invalid-output rates, and profile / probe / sweep APIs and Dashboard controls. Verified against Qwen3.5-2B in LM Studio on CPU. Alembic head `202611100001`. See [`docs/evaluation.md`](evaluation.md) §14.
- **`v1.5.10` (Historical)**: LLM-as-Judge Triage (Shadow) & Quota-Capped Review Sampling. Adds pluggable `deterministic` / `openai_compatible` judge providers (re-redaction, nonce-bounded evidence, strict output schema, external-endpoint opt-in, timeout fallback, same-family warning) that only produce advisory `triage_priority` (`P1`–`P3`) and `triage_score` on reviews (`judge_assessments`, `jdg_`); the judge never writes verdicts, priorities or statuses. Rule-passed outcomes get one recorded sampling decision (`review_sampling_decisions`, `smp_`): a uniform random audit slice for an unbiased miss-rate estimate and a threshold slice for shadow-detector hits and high risk, with daily and share caps and counted skips. Benchmark runs measure triage precision@K (`rule_only` / `judge_only` / `combined`). Readiness gains advisory `evaluation_judge_health` and `evaluation_sampling_health`. Alembic head `202611090001`. See [`docs/evaluation.md`](evaluation.md).
- **`v1.5.9` (Historical)**: Labeled Benchmark & Detector Measurement. Adds versioned labeled samples (`benchmark_samples`, seed corpus `seed-v1.5.9`, review export, per-finding labels, optional Inspect JSON import), side-effect-free benchmark runs with AUROC, TPR at FPR 1% / 5% (Wilson intervals, insufficient-sample flags) and audit-budget curves (`/v1/benchmark/*`, `/dashboard/benchmark`), the shadow `special_casing` detector (`SP-001`–`SP-004`), six honeypot impossible-task probes with server-side run mapping, monitor-only per-agent / per-model cheat rates, and DSSE / in-toto CI attestation verification against tenant trust roots (`ci_attested` assurance, `CE-009` / `CE-010`). Readiness gains the advisory `evaluation_benchmark_gate`. Alembic head `202611080001`. See [`docs/evaluation.md`](evaluation.md).
- **`v1.5.8` (Historical)**: Agent Outcome Ingestion, Evidence-Based Reward-Hacking Detectors & Post-Execution Review Queue. Adds `POST /v1/runs/{run_id}/outcome` (diff, test/lint reports, final message, provenance, redaction, append-only and capped submissions), the `app/services/evaluation/` engine (taxonomy, claim extraction, versioned detectors with shadow mode, deterministic aggregation, `evaluator_version`), five evidence detectors (test / grader / CI tampering, claim–evidence mismatch, error suppression, scope), and a post-execution review queue (`review_required`, `rejected`, `/dashboard/reviews`) that is separate from pre-execution approval and never re-executes runs. Alembic head `202611070001`. See [`docs/evaluation.md`](evaluation.md).
- **`v1.5.7` (Historical)**: Safe MCP Gateway MVP, Tool Schema Guardrails & Per-MCP-Server FinOps Attribution. Adds the MCP Server Registry with trust tiers, transport/external-network guardrails, tenant allow-lists, per-server quotas and breakers (`/v1/control-plane/mcp-servers`), `AgentRequest.tool_arguments` JSON Schema validation with per-field injection scan profiles and recursive sensitive-argument redaction (`POST /v1/control-plane/tools/{name}/validate-arguments`), dynamic executor registry closure with worker-side hard-deny re-checks, and `UsageEvent.mcp_server_id` / `tool_breakdown` / `mcp_server_breakdown` / `scope="mcp_server"` FinOps attribution with zero migrations (`app/services/mcp_gateway.py`).
- **`v1.5.6` (Historical)**: FinOps Burn-Rate Forecasting, Dynamic Token Broker, Per-User/Per-Tool Quotas & Interactive Governance Portals. Adds real-time tenant/project burn-rate and time-to-exhaustion forecasting (`GET /v1/cost/tenants/{tenant_id}/forecast`, `finops_burn_rate_warning`), pre-execution token budget leasing (`lease_...`) with concurrent sibling-lease reservation in `evaluate_budget_policies()` and automatic/manual lease release (`POST /v1/cost/leases/{run_id}/release`), per-user and per-tool Redis sliding-window rate limits, automatic worker-failure circuit-breaker tripping and manual trip/reset endpoints (`POST /v1/control-plane/tools/{name}/breaker/open|close`), and interactive management forms across `/dashboard/governance`, `/dashboard/control-plane`, and `/dashboard/costs`.
- **`v1.5.5` (Historical)**: FinOps Settled Ledger Correctness, Multi-Currency FX & Tax Accounting, and Reconciliation Ledger Export. Eliminates estimated/actual double-counting via settled cost deduplication (`_settled_cost_events`), activates multi-currency FX conversion (`fx_rate`, `fx_rate_source`, `base_currency_cost`) and tax accounting (`tax_rate`, `tax_inclusive`, `tax_amount`) with pre-tax base-currency budget enforcement, adds `cost_center`/`org_id` allocation attribution (`finops_missing_cost_center`), supports manual reconciliation adjustments (`POST /v1/cost/adjustments`), and provides RFC 4180 CSV/JSON tenant ledger exports (`GET /v1/cost/tenants/{tenant_id}/export`).
- **`v1.5.4` (Historical)**: Model-Aware FinOps Routing, Step-Level Execution Audit Closure & Interactive Operations Recovery Portal. Added dynamic `provider`/`model_name`/`pricing_mode`/`project_id` routing and catalog pricing in submit/worker flows, `user`/`project`/`model` budget scope isolation, `dry_run_only` enforcement, `failed_retry` cost accounting, automatic `ExecutionPlanStep` lifecycle synchronization (`healthy` plan health), and interactive Run Replay/Resume, Worker DLQ Requeue/Resolve, and Stale Approval TTL sweep endpoints and UI controls.
- **`v1.5.3` (Historical)**: Active FinOps Budget Enforcement, Context-Aware Pricing Engine & FinOps Governance Portal. Added pre-execution and runtime budget enforcement (`block`, `require_approval`, `degrade_model`, `warn_only`) across `run`/`tenant`/`workflow`/`user` scopes and `per_run`/`daily`/`monthly` windows, context-tier pricing resolution, cache/batch savings calculation, `/dashboard/admin/finops/budgets` governance portal, violation triage lifecycle (`acknowledge`, `resolve`, `ignore`), stale source guardrails, FinOps Prometheus metrics, and Pydantic V2 `PlainSerializer` zero-warning serialization.
- **`v1.5.2` (Historical)**: Open-Source Readiness, Internationalization & OpenAI Codex for OSS Alignment. Complete documentation rewrite in English, standardized open-source licensing (Apache-2.0), community governance documents (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`), automated secret scanning, and operations release gate version synchronization.
- **`v1.5.1` (Historical)**: Agent FinOps Core / Pricing Import Admin. Expanded schema for pricing, usage, cost, budget, and violation entities. Added official model pricing seed ingestion (`app/seeds/model_pricing_seed.yaml`), dry-run validation, versioned pricing change logs, source document audit trails, and Admin FinOps pricing dashboard.
- **`v1.5.0` (Historical)**: Agent FinOps Core / Cost Ledger. Introduced `pricing_catalog`, `usage_events`, `cost_events`, `budget_policies`, and `budget_violations`, along with FinOps REST APIs and `/dashboard/costs`.
- **`v1.4.0` (Historical)**: Checkpoint / Resume / Replay MVP. Added submit checkpoints (`run_checkpoints`), resume flow re-dispatching to workers, dry-run replay attempts, and execution replay tracking.
- **`v1.3.0` (Historical)**: Trace & Replay Readiness. Propagated `X-Trace-Id` to execution plans and step audits, and provided reproducible replay bundle export APIs.
- **`v1.2.0` (Historical)**: Control Plane Execution Plan. Added workflow/tool registries, execution plan generation, and `/dashboard/control-plane`.
- **`v1.1.0` (Historical)**: Classical Chinese Prompt Guard. Added adversarial prompt detection for classical/literary Chinese jailbreaks and credential harvesting.
- **`v1.0.0` (Historical)**: Production Readiness Gate. Hardened release gate checklist with safety regression verification, HTTP Prometheus metrics, and red-team test suite.
- **`v0.6.0` - `v0.8.0` (Historical)**: Tenant Governance Center, hashed API keys, Redis quotas, and Operations Gate baseline.
- **`v0.1.0` - `v0.5.0` (Historical)**: Foundational guarded asynchronous execution, Kafka worker queues, retry/DLQ, Redis run states, human approval TTL gates, and evaluator transcripts.

---

## System Positioning

RewardHacking is a production-grade safety guardrail and governance control plane designed to encapsulate autonomous AI agents within an enforceable security boundary:
- **Pre-execution Ingestion**: Performs prompt sanitization, Classical Chinese prompt guard analysis, and multi-factor risk scoring.
- **Deterministic Policy Engine**: Evaluates constraints, tools, and prompts to make fail-closed `allow`, `escalate`, or `deny` decisions.
- **Control Plane Execution Planning**: Emits auditable execution plans capturing selected tools, steps, and approval gates before task queueing.
- **Asynchronous Bounded Execution**: Queues low-risk requests via Kafka (`agent.requests`) to background workers with tool circuit breakers and retry DLQs.
- **Human-in-the-Loop Approval Gates**: Quarantines destructive or policy-violating actions into a TTL-guarded approval queue (`/dashboard/approvals`).
- **Traceability & Checkpoint Replay**: Links all requests, plans, and audit steps to an `X-Trace-Id`, enabling checkpoint recovery and replay bundles.
- **Evidence-Based Evaluation & Measurement**: Deterministic, versioned detectors over agent outcome evidence, a post-execution review queue, and a labeled benchmark that measures detection rates at low false-positive rates, advisory LLM-judge triage that only reorders review work, and quota-capped review sampling of rule-passed runs (monitor-only signals).
- **Agent FinOps Ledger**: Enforces immutable usage and cost tracking across runs, tenants, and models.
- **Operational Dashboard**: Jinja2-rendered real-time views for runs, approvals, control plane, governance, operations, and costs.

---

## Documentation Index

| Document | Purpose |
| --- | --- |
| [System Architecture](architecture.md) | Architectural topology, component boundaries, and request dataflow |
| [REST API Specification](api.md) | Complete OpenAPI endpoints, schemas, headers, and status codes |
| [Operational Dashboard Guide](dashboard.md) | User interface walkthrough, approval reviews, and navigation map |
| [Configuration Reference](configuration.md) | Environment variables, `.env` settings, and production recommendations |
| [Deployment & Hardening](deployment.md) | Docker Compose setup, container configurations, and production readiness |
| [Developer Guide](development.md) | Local development workflow, code style, and contribution standards |
| [Testing Guide](testing_guide.md) | Pytest test suite, adversarial red-team corpus, and CI verification |
| [Manual QA & Browser Testing Guide](manual_test.md) | Step-by-step interactive browser walkthroughs and manual QA checklists |
| [Database Data Model](data_model.md) | SQLAlchemy schemas, audit trail models, and data lifecycles |
| [Kafka Topics & Schemas](kafka_topics.md) | Event topic designs, payloads, and consumer guarantees |
| [Redis Keyspace](redis_keys.md) | State conventions, rate-limiting quotas, circuit breakers, and TTLs |
| [Database Migrations](migrations.md) | Alembic migration history, schema revisions, and upgrade procedures |
| [Project Roadmap](roadmap.md) | Evolutionary milestones and future roadmap items (v1.6+) |
| [Agent FinOps Core Reference](finops_core.md) | Model pricing catalogs, usage/cost ledgers, and budget enforcement |
| [Evidence-Based Evaluation](evaluation.md) | Outcome evidence, detector rules, review queue, benchmark metrics, honeypots, CI attestation, judge triage and review sampling |
| [Production Runbooks](runbook.md) | Operational runbooks, DLQ mitigation, and emergency procedures |
| [Observability & Metrics](observability.md) | Prometheus metrics, SLO definitions, and alert rules |
| [Model Pricing Seed Reference](pricing/model_pricing_seed.md) | Official model catalog YAML specification and ingestion |
| [Release Verification Report](test_report.md) | Real-world test execution records and release verification evidence |
