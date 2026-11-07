# Project Roadmap: Evolutionary Milestones

> **Roadmap Baseline**: `v1.5.7`
> **Status**: Active Open-Source Track (`v1.5.x` Continuous Governance & FinOps Series)
> **Next Target**: `v1.5.8` (Pluggable LLM-as-Judge, Evaluation Benchmarks & OTLP Exporter)

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
| `v1.5.5` | Completed | FinOps Settled Ledger, Multi-Currency FX, Tax & Reconciliation Export | Settled cost deduplication (eliminating estimated/actual double-counting), multi-currency FX conversion (`fx_rate`, `base_currency_cost`), tax accounting, `cost_center` attribution, manual adjustments (`cost_adj_`), and CSV/JSON ledger exports. |
| `v1.5.6` | Completed | Burn-Rate Forecasting, Interactive Governance Portal, Fine-Grained Quotas & Token Broker | Real-time tenant/project burn-rate forecasting & exhaustion alerts, interactive Tenant/API-Key/Control-Plane CRUD portals, per-user and per-tool sliding-window quotas, manual circuit-breaker controls, and pre-execution token budget leasing (Dynamic Token Broker). |
| `v1.5.7` | **Current** | **Safe MCP Gateway MVP & Tool Schema Guardrails** | MCP server registry with trust tiers & transport guardrails, tool input/output JSON schema validation with injection scan profiles, sensitive argument redaction, dynamic executor registry closure, and per-MCP-server cost and risk attribution (`mcp_server_id`). |
| `v1.5.8+`| **Next** | Pluggable LLM-as-Judge, Evaluation Benchmarks & OTLP Exporter | Configurable external/local judge providers, red-team dataset benchmark runner with precision/recall tracking, OpenTelemetry (OTLP) span export, and automated audit retention purge drills. |
| `v1.6.0+`| Long-Term | Isolated Sandbox Runtime & Stateful Graph Replay | Ephemeral container/microVM tool sandboxing (e.g., gVisor / Firecracker) replacing simulated bounded execution, and stateful graph-level time-travel execution. |

---

## Current Release: `v1.5.7` Safe MCP Gateway MVP & Tool Schema Guardrails

The `v1.5.7` milestone closes the structured tool-argument attack surface:
- **Safe MCP Server Registry, Trust Tiers & Transport Guardrails**: Registers Model Context Protocol servers (`GET/POST /v1/control-plane/mcp-servers`) with `transport` (`stdio | sse | streamable_http`), `trust_tier` (`trusted | verified | untrusted | blocked`), hostname-parsed `is_external_network` detection, tenant allow-lists and per-server quotas. Four builtin servers ship by default (`mcp-core-local`, `mcp-data-verified`, `mcp-deploy-verified`, `mcp-external-untrusted`). Blocked/disabled servers or disallowed tenants are hard-denied (zero cost, no lease); untrusted servers (+0.20 risk), external-network violations (+0.15 risk) and open MCP breakers (`circuit:mcp:{id}`) route runs to `awaiting_approval` with `mcp_trust_gate` / `mcp_network_gate` / `mcp_breaker_gate`.
- **Tool JSON Schema Guardrails, Injection Scan Profiles & Sensitive Argument Redaction**: `AgentRequest.tool_arguments` is validated by a dependency-free JSON Schema subset validator (`required`, `type`, `additionalProperties`, `min/maxLength`, `minimum/maximum`, `enum`, `pattern`, `items`) with depth/size limits. Per-field `x-scan-profile` (`path | shell | sql_destructive | sql | none`) blocks path traversal and shell/SQL injection without false positives on `query_data` `UNION SELECT` or `write_patch` diffs. Credentials, bearer tokens and emails are recursively redacted to `[REDACTED]` / `[REDACTED_EMAIL]` before `AgentRun`, `ExecutionPlan`, `ToolExecution` and replay bundles are persisted. `POST /v1/control-plane/tools/{tool_name}/validate-arguments` exposes the validator directly.
- **Dynamic Executor Registry Closure & Worker Re-Check**: The background worker now executes any tool registered in the Control Plane (no more false `blocked` transcripts for dynamically registered tools), validates simulated outputs against `output_schema`, and re-checks hard-deny MCP conditions at execution time (TOCTOU) while never re-escalating already-approved runs.
- **Per-MCP-Server FinOps Attribution**: Populates `UsageEvent.mcp_server_id` / `tool_*` columns, tags `CostEvent.allocation_tags` and metadata, fixes `run_cost_summary.tool_breakdown` and `tenant_cost_summary.top_tools` (no more `step_id` leakage), adds `mcp_server_breakdown` / `top_mcp_servers`, `scope="mcp_server"` and exact `scope="tool"` budgets, `mcp_server_id` filters on cost summary/export, and appends `mcp_server_id,tool_id` to the CSV ledger (37 columns). Zero Alembic migrations (head stays `202606010004`).

## Previous Release: `v1.5.6` Burn-Rate Forecasting, Interactive Governance Portal, Fine-Grained Quotas & Token Broker

The `v1.5.6` milestone closes the pre-execution financial and operational governance loop across four domains:
1. **Burn-Rate & Exhaustion Forecasting**: Computes hourly/daily tenant and project burn rates, projected end-of-period spend, and estimated time-to-hard-limit exhaustion warnings (`normal | elevated_burn | critical_burn | exhausted`, `finops_burn_rate_warning`) via `GET /v1/cost/tenants/{tenant_id}/forecast` and `/dashboard/costs`.
2. **Dynamic Token Broker (Pre-Execution Budget Leasing)**: Reserves estimated token/cost leases (`lease_...`) in Redis and `AgentRun.evaluation_summary["token_budget_lease"]` while runs are queued or awaiting approval so concurrent requests cannot overshoot shared budgets, settling on worker completion or releasing on denial/manual operator action (`POST /v1/cost/leases/{run_id}/release`).
3. **Per-User & Per-Tool Quotas & Self-Healing Circuit Breakers**: Enforces Redis sliding-window rate limits per user and per tool, automatically trips tool circuit breakers after consecutive worker failures, escalates runs requesting open-breaker tools (`tool_circuit_breaker_open` -> `awaiting_approval`), and provides manual trip/reset APIs (`POST /v1/control-plane/tools/{name}/breaker/open|close`).
4. **Interactive Tenant, API Key & Control-Plane Portals**: Upgrades `/dashboard/governance`, `/dashboard/control-plane`, and `/dashboard/costs` with interactive forms for Tenant upsert, API key issuance (with one-time plaintext banner) and revocation, Tool/Workflow registration, Circuit Breaker management, and Token Lease lifecycle controls.

---

## Active `v1.5.x` Evolution Track (`v1.5.8+`)

Rather than jumping directly to a monolithic `v1.6.0` rewrite, the `v1.5.x` release train delivers focused, verifiable governance and FinOps closures in incremental minor releases:

### `v1.5.7` (Completed): Safe MCP Gateway MVP & Tool Schema Guardrails
- **MCP Server Registry & Trust Tiers**: Registers Model Context Protocol (MCP) servers, versions, transport policies, and risk levels in the Control Plane.
- **MCP Schema Boundary Validation & Cost Attribution**: Enforces strict input/output JSON schema checks, redacts sensitive arguments before tool invocation, and tracks per-MCP-server usage and costs via `UsageEvent.mcp_server_id`.

### `v1.5.8+` (Next Target): Pluggable LLM-as-Judge, Evaluation Benchmarks & OTLP Exporter
- **Pluggable Judge & Red-Team Benchmark Suite**: Supports optional external/local LLM judge providers alongside the deterministic rubric evaluator, with batch red-team corpus evaluation and precision/recall drift metrics.
- **OpenTelemetry (OTLP) Exporter & Retention Automation**: Exports distributed traces (`trace_id`, `span_id`) to OTLP collectors (Jaeger, Tempo, LangSmith) and automates scheduled audit retention purges and operational drill evidence.

---

## Long-Term Horizon: `v1.6.0` and Beyond

- **MicroVM & Container Process Sandboxing**: Replacing simulated bounded executor transcripts with isolated ephemeral execution environments (e.g., Firecracker microVMs or gVisor sandboxes) for real external command and patch execution.
- **Stateful Graph Time-Travel & Live Replay**: Extending dry-run replay bundles into full stateful workflow graph checkpointing and isolated sandbox re-execution.
