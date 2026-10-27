# Project Roadmap: Evolutionary Milestones

> **Roadmap Baseline**: `v1.5.3`
> **Status**: Active Open-Source Track (`v1.5.x` Continuous Governance & FinOps Series)
> **Next Target**: `v1.5.4` (Model-Aware FinOps Routing, Step Audit Closure & Recovery Portal)

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
| `v1.5.3` | **Current** | **Active FinOps Governance & Budget Enforcement** | Multi-scope/period active budget enforcement (`block`, `require_approval`, `degrade_model`), context-tier pricing, savings accounting, and FinOps Budget Governance Portal. |
| `v1.5.4` | **Next** | **Model-Aware FinOps Routing, Step Audit Closure & Recovery Portal** | Dynamic catalog model routing & re-pricing, `user`/`project` scope & `dry_run_only` enforcement, `failed_retry` cost accounting, `ExecutionPlanStep` runtime sync, and interactive Run Replay/Resume, DLQ & Approval TTL recovery UI. |
| `v1.5.5` | Planned | FinOps Burn-Rate Forecasting, Multi-Currency FX & Ledger Export | Real-time tenant/project burn-rate forecasting, exhaustion alerts, multi-currency FX conversion (`fx_rate`, `base_currency_cost`), tax accounting, and CSV/JSON audit ledger exports. |
| `v1.5.6` | Planned | Interactive Governance Portal, Fine-Grained Quotas & Token Broker | Interactive Tenant/API-Key/Control-Plane CRUD portals, per-user and per-tool sliding-window quotas, manual circuit-breaker controls, and pre-execution token budget leasing (Dynamic Token Broker). |
| `v1.5.7` | Planned | Safe MCP Gateway MVP & Tool Schema Guardrails | Model Context Protocol (MCP) server registry, tool input/output JSON schema validation, argument sanitization, and per-MCP-server cost and risk attribution (`mcp_server_id`). |
| `v1.5.8+`| Planned | Pluggable LLM-as-Judge, Evaluation Benchmarks & OTLP Exporter | Configurable external/local judge providers, red-team dataset benchmark runner with precision/recall tracking, OpenTelemetry (OTLP) span export, and automated audit retention purge drills. |
| `v1.6.0+`| Long-Term | Isolated Sandbox Runtime & Stateful Graph Replay | Ephemeral container/microVM tool sandboxing (e.g., gVisor / Firecracker) replacing simulated bounded execution, and stateful graph-level time-travel execution. |

---

## Current Release: `v1.5.3` Active FinOps Governance & Budget Enforcement

The `v1.5.3` milestone upgrades Agent FinOps Core from passive cost tracking into an active pre-execution and runtime governance engine:
1. **Active Multi-Scope & Multi-Period Budget Enforcement**: Enforces `run`, `tenant`, `workflow`, and `user` budgets across `per_run`, `daily`, and `monthly` windows with `block`, `require_approval`, `degrade_model`, and `warn_only` actions.
2. **Context-Aware Tiered Pricing & Savings Engine**: Evaluates `context_tier`, `minimum_billable_quantity`, `free_quota_Included`, and calculates `cache_savings` and `batch_savings`.
3. **FinOps Budget Governance Portal & Violation Triage**: Provides `/dashboard/admin/finops/budgets`, violation `acknowledge`/`resolve`/`ignore` actions, manual pricing change logs, and stale pricing source warnings.
4. **Zero-Warning Pydantic V2 Serialization & FinOps Observability**: Eliminates Pydantic V2 deprecation warnings via `PlainSerializer`, normalizes plain decimal formatting across UI templates, and adds FinOps Prometheus metrics and Operations readiness checks.

---

## Active `v1.5.x` Evolution Track (`v1.5.4` – `v1.5.8+`)

Rather than jumping directly to a monolithic `v1.6.0` rewrite, the `v1.5.x` release train delivers focused, verifiable governance and FinOps closures in incremental minor releases:

### `v1.5.4` (Next Target): Model-Aware FinOps Routing, Step Audit Closure & Recovery Portal
- **Catalog-Driven Model Routing & Degradation Re-Pricing**: Connects `AgentRequest` model selection and `ExecutionPlan.model_route` directly to the 149-row official pricing catalog during both pre-execution estimation and worker completion, including automatic cost recalculation when `degrade_model` routes to a fallback tier.
- **Full-Scope Budget Closure & Failed-Retry Costing**: Completes `user`, `project`, and `model` scope filtering, implements the `dry_run_only` enforcement action, and records `cost_type="failed_retry"` when worker attempts fail or retry.
- **Step-Level Execution Audit Synchronization**: Synchronizes `ExecutionPlan` and `ExecutionPlanStep` (`approval-gate`, `tool-execution`, `evaluation`) statuses, durations, and summaries upon human approval decisions and background worker completion.
- **Interactive Operations Recovery Portal**: Adds one-click **Trigger Dry-Run Replay** and **Resume from Checkpoint** forms on `/dashboard/runs/{run_id}`, **DLQ Requeue / Resolve** actions on `/dashboard/worker`, and **Expire Stale Approvals** TTL sweeping on `/dashboard/approvals`.

### `v1.5.5` (Planned): FinOps Burn-Rate Forecasting, Multi-Currency FX & Ledger Export
- **Burn-Rate & Exhaustion Forecasting**: Computes hourly/daily tenant and project burn rates, projected end-of-period spend, and estimated time-to-hard-limit exhaustion warnings on `/dashboard/costs`.
- **Multi-Currency FX & Tax Accounting**: Activates `fx_rate`, `fx_rate_source`, `base_currency_cost`, and `tax_amount` normalization across multi-currency pricing catalog entries and tenant cost summaries.
- **Financial Reconciliation & CSV/JSON Audit Exports**: Provides downloadable CSV and JSON ledger exports (`GET /v1/cost/tenants/{tenant_id}/export`) with cost-center and invoice reconciliation tags.

### `v1.5.6` (Planned): Interactive Governance Portal, Fine-Grained Quotas & Token Broker
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
