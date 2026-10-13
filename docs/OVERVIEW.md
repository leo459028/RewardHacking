# Project Documentation Overview

> **Current Version**: `v1.5.2`  
> **Previous Version**: `v1.5.1`

This directory contains the comprehensive technical documentation for the **RewardHacking Guardrail** platform.

---

## Release Baseline: `v1.5.2`

### Release Evolution

- **`v1.5.2` (Current)**: Open-Source Readiness, Internationalization & OpenAI Codex for OSS Alignment. Complete documentation rewrite in English, standardized open-source licensing (Apache-2.0), community governance documents (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`), automated secret scanning, and operations release gate version synchronization.
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
- **Independent Task Evaluation**: Uses evaluation rubrics to detect reward hacking, shortcuts, and fabricated completions.
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
| [Database Data Model](data_model.md) | SQLAlchemy schemas, audit trail models, and data lifecycles |
| [Kafka Topics & Schemas](kafka_topics.md) | Event topic designs, payloads, and consumer guarantees |
| [Redis Keyspace](redis_keys.md) | State conventions, rate-limiting quotas, circuit breakers, and TTLs |
| [Database Migrations](migrations.md) | Alembic migration history, schema revisions, and upgrade procedures |
| [Project Roadmap](roadmap.md) | Evolutionary milestones and future roadmap items (v1.6+) |
| [Agent FinOps Core Reference](v1.5.1_finops_core.md) | Model pricing catalogs, usage/cost ledgers, and budget enforcement |
| [Production Runbooks](runbook.md) | Operational runbooks, DLQ mitigation, and emergency procedures |
| [Observability & Metrics](observability.md) | Prometheus metrics, SLO definitions, and alert rules |
| [Model Pricing Seed Reference](pricing/model_pricing_seed.md) | Official model catalog YAML specification and ingestion |
| [Release Verification Report](test_report.md) | Real-world test execution records and release verification evidence |
