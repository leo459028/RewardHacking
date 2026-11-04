# Project Roadmap: Evolutionary Milestones

> **Roadmap Baseline**: `v1.5.5`
> **Status**: Active Open-Source Track (`v1.5.x` Continuous Governance & FinOps Series)
> **Next Target**: `v1.5.6` (FinOps Burn-Rate Forecasting, Interactive Governance Portal, Fine-Grained Quotas & Token Broker)

This roadmap outlines the past development trajectory, the active `v1.5.x` incremental release track (`v1.5.4`, `v1.5.5`, `v1.5.6`, `v1.5.7`...), and long-term architectural milestones for the **RewardHacking** platform.

---

## Release Cadence & Timeline

| Release | Status | Focus Area | Description |
| --- | --- | --- | --- |
| `v0.2.0` | Completed | Synchronous Prototype | Initial API endpoints, SQLite/Postgres audit trail, and server-rendered HTML dashboard. |
| `v0.3.0` | Completed | Guarded Async Execution MVP | Decoupled Kafka worker queues, Redis run state, bounded executor, and human approval queues. |
| `v0.4.0` | Completed | Execution Hardening | Worker retries with DLQ, approval TTL timers, tool execution audit tables, and circuit breakers. |
| `v0.5.0` | Completed | Evaluator & Observability | Task-specific evaluator rubrics, synthetic judge signals, Prometheus metrics, and SLO tracking. |
| `v0.6.0` | Completed | Tenant Governance MVP | API key auth, Dashboard Basic Auth, Redis sliding-window tenant quotas, and Governance Dashboard. |
| `v0.7.0` | Completed | Tenant Governance Center | DB-backed tenant settings, hashed API keys, RBAC-lite roles, and tenant quota overrides. |
| `v0.8.0` | Completed | Operations Gate | Operations Dashboard, deployment readiness checks, operational evidence logging, and `X-Trace-Id`. |
| `v1.0.0` | Completed | Production Readiness Baseline | Fail-closed release gates in `APP_ENV=prod`, safety regression evidence, and red-team regression corpus. |
| `v1.1.0` | Completed | Classical Chinese Prompt Guard | Specialized deterministic detector for literary and classical Chinese adversarial jailbreaks. |
| `v1.2.0` | Completed | Control Plane Execution Plan | Pre-execution planning, workflow definitions, and tool registry snapshots before task queueing. |
| `v1.3.0` | Completed | Trace & Replay Readiness | Step-level audit persistence, distributed trace propagation, and self-contained Replay Bundle exports. |
| `v1.4.0` | Completed | Checkpoint / Resume / Replay MVP | Submit checkpoints, resume checkpoint re-dispatching, and non-destructive dry-run replay attempts. |
| `v1.5.0` | Completed | Agent FinOps Core / Cost Ledger | Standardized model pricing catalog, immutable usage/cost ledger, and budget violation tracking. |
| `v1.5.1` | Completed | Pricing Seed Importer & Admin UI | Ingestion of official model pricing YAML seeds, dry-run validation, and Admin FinOps pricing portal. |
| `v1.5.2` | Completed | Open-Source & Codex OSS Baseline | Full English internationalization, Apache-2.0 licensing, community governance standards, secret scan automation. |
| `v1.5.3` | Completed | Active FinOps Governance & Budget Enforcement | Multi-scope/period active budget enforcement (`block`, `require_approval`, `degrade_model`), context-tier pricing, savings accounting, and FinOps Budget Governance Portal. |
| `v1.5.4` | Completed | Model-Aware FinOps Routing, Step Audit Closure & Recovery Portal | Dynamic catalog model routing & re-pricing, `user`/`project` scope & `dry_run_only` enforcement, `failed_retry` cost accounting, `ExecutionPlanStep` runtime sync, and interactive Run Replay/Resume, DLQ & Approval TTL recovery UI. |
| `v1.5.5` | **Current** | **FinOps Settled Ledger, Multi-Currency FX, Tax & Reconciliation Export** | Settled cost deduplication (eliminating estimated/actual double-counting), multi-currency FX conversion (`fx_rate`, `base_currency_cost`), tax accounting, `cost_center` attribution, manual adjustments (`cost_adj_`), and CSV/JSON ledger exports. |
| `v1.5.6` | **Next** | **Burn-Rate Forecasting, Interactive Governance Portal, Fine-Grained Quotas & Token Broker** | Real-time tenant/project burn-rate forecasting & exhaustion alerts, interactive Tenant/API-Key/Control-Plane CRUD portals, per-user and per-tool sliding-window quotas, manual circuit-breaker controls, and pre-execution token budget leasing (Dynamic Token Broker). |
| `v1.5.7` | Planned | Safe MCP Gateway MVP & Tool Schema Guardrails | Model Context Protocol (MCP) server registry, tool input/output JSON schema validation, argument sanitization, and per-MCP-server cost and risk attribution (`mcp_server_id`). |
| `v1.5.8+`| Planned | Pluggable LLM-as-Judge, Evaluation Benchmarks & OTLP Exporter | Configurable external/local judge providers, red-team dataset benchmark runner with precision/recall tracking, OpenTelemetry (OTLP) span export, and automated audit retention purge drills. |
| `v1.6.0+`| Long-Term | Isolated Sandbox Runtime & Stateful Graph Replay | Ephemeral container/microVM tool sandboxing (e.g., gVisor / Firecracker) replacing simulated bounded execution, and stateful graph-level time-travel execution. |

---

## Current Release: `v1.5.5` FinOps Settled Ledger, Multi-Currency FX, Tax & Reconciliation Export

The `v1.5.5` milestone upgrades Agent FinOps Core into an audit-ready multi-currency financial reconciliation ledger:
1. **Settled Ledger Correctness (Zero Double-Counting)**: Deduplicates pre-execution `estimated` cost events once `actual` or `failed_retry` worker cost events arrive for the same `(run_id, replay_attempt_id, resume_attempt_id, resource_type, usage_unit)` group.
2. **Multi-Currency FX Normalization & Tax Accounting**: Normalizes foreign-currency pricing rows (`TWD`, `CNY`, `EUR`, `JPY`, `GBP`) into `FINOPS_BASE_CURRENCY` (`USD` by default) using `fx_rate`, `tax_rate`, `tax_inclusive`, `tax_amount`, and `base_currency_cost`, with pre-tax base-currency budget evaluation and fail-open `missing_fx_rate` violation tracking.
3. **Cost-Center Attribution & Manual Adjustments**: Extracts `cost_center`, `org_id`, `business_unit`, and `environment` into `ExecutionPlan.model_route["allocation"]` and `CostEvent.allocation_tags`, and enables `POST /v1/cost/adjustments` (`cost_adj_...`) for credits, discounts, and invoice true-ups.
4. **Reconciliation CSV/JSON Ledger Export**: Provides `GET /v1/cost/tenants/{tenant_id}/export?format=csv|json` with formula-injection protection and interactive dashboard export and adjustment tools on `/dashboard/costs`.

---

## Active `v1.5.x` Evolution Track (`v1.5.6` – `v1.5.8+`)

Rather than jumping directly to a monolithic `v1.6.0` rewrite, the `v1.5.x` release train delivers focused, verifiable governance and FinOps closures in incremental minor releases:

### `v1.5.6` (Next Target): Burn-Rate Forecasting, Interactive Governance Portal, Fine-Grained Quotas & Token Broker
- **Burn-Rate & Exhaustion Forecasting**: Computes hourly/daily tenant and project burn rates, projected end-of-period spend, and estimated time-to-hard-limit exhaustion warnings on `/dashboard/costs`.
- **Interactive Tenant, API Key & Control-Plane Portals**: Upgrades `/dashboard/governance` and `/dashboard/control-plane` from read-only views into full management portals for tenant quotas, API key issuance/revocation, and tool/workflow registration.
- **Per-User & Per-Tool Quotas & Circuit Breakers**: Extends Redis sliding-window rate limits to individual users and tools, with interactive circuit-breaker trip/reset controls.
- **Dynamic Token Broker (Pre-Execution Budget Leasing)**: Reserves estimated token/cost leases in Redis/Postgres while runs are queued or running to prevent concurrent requests from overshooting shared tenant budgets.

### `v1.5.7` (Planned): Safe MCP Gateway MVP & Tool Schema Guardrails
- **MCP Server Registry & Trust Tiers**: Registers Model Context Protocol (MCP) servers, versions, transport policies, and risk levels in the Control Plane.
- **MCP Schema Boundary Validation & Cost Attribution**: Enforces strict input/output JSON schema checks, redacts sensitive arguments before tool invocation, and tracks per-MCP-server usage and costs via `UsageEvent.mcp_server_id`.

### `v1.5.8+` (Planned): Pluggable LLM-as-Judge, Evaluation Benchmarks & OTLP Exporter
- **Pluggable Judge & Red-Team Benchmark Suite**: Supports optional external/local LLM judge providers alongside the deterministic rubric evaluator, with batch red-team corpus evaluation and precision/recall drift metrics.
- **OpenTelemetry (OTLP) Exporter & Retention Automation**: Exports distributed traces (`trace_id`, `span_id`) to OTLP collectors (Jaeger, Tempo, LangSmith) and automates scheduled audit retention purges and operational drill evidence.

---

## Long-Term Horizon: `v1.6.0` and Beyond

- **MicroVM & Container Process Sandboxing**: Replacing simulated bounded executor transcripts with isolated ephemeral execution environments (e.g., Firecracker microVMs or gVisor sandboxes) for real external command and patch execution.
- **Stateful Graph Time-Travel & Live Replay**: Extending dry-run replay bundles into full stateful workflow graph checkpointing and isolated sandbox re-execution.
