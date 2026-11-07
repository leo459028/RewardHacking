# Agent FinOps Core & Cost Accounting Reference

> **Module**: Agent FinOps Core (`v1.5.10`, `FINOPS_CALCULATION_VERSION = "v1.5.10"`; no calculation changes since `v1.5.8`)
> **Schema Revision**: `202611090001` (Alembic Head; FinOps tables unchanged since `202606010004`)
> **Reference Seed**: `app/seeds/model_pricing_seed.yaml`

This document details the architectural design, database schemas, burn-rate forecasting, Dynamic Token Broker pre-execution leasing, settled ledger deduplication, multi-currency FX & tax accounting, cost-center attribution, manual reconciliation adjustments, ledger exports, model-aware routing, active budget enforcement, context-aware pricing resolution, administration APIs, and YAML seed ingestion mechanics of the **Agent FinOps Core** engine.

---

## Evaluation Judge Cost Attribution (`v1.5.10`)

LLM judge calls (review triage and LLM-judged benchmark runs) record actual usage through `record_usage_event()`: one event each for `input_tokens` and `output_tokens`, `resource_type="model"`, `resource_subtype="chat_completion"`, `provider="custom"`, `step_id="evaluation_judge"`, `phase="actual"`, `status` `success` / `timeout` / `failed`, and `metadata.cost_kind="evaluation_judge"` with `judge_version` and `review_id` or `benchmark_run_id` / `sample_id`. Review triage events carry the reviewed `run_id`; benchmark events have an empty `run_id`. Judge usage never acquires, settles or releases a run token lease and never re-evaluates run budgets. A judge model without a pricing row records a `missing_pricing_rule` violation (add a catalog row for the judge model). The per-tenant daily judge token cap (`EVALUATION_JUDGE_DAILY_TOKEN_CAP_PER_TENANT`) is enforced from `judge_assessments` before each call.

## Lease Settlement for Post-Execution Reviews (`v1.5.8`)

Runs that finish in `review_required` or `rejected` are execution-finished: `record_actual_usage()` records actual usage with status `success` and settles the token lease exactly like `completed`. Post-execution review decisions never acquire or release leases and never re-dispatch the run, so no duplicate cost is recorded.

## Per-MCP-Server & Per-Tool Attribution (`v1.5.7`)

- **Usage columns**: `record_submit_usage()` and `record_actual_usage()` keep writing exactly 3 events per phase (settlement key unchanged). The `step_2_tools` (`resource_type="tool"`, `usage_unit="tool_call"`) event now fills `mcp_server_id` (primary server), `tool_id` / `tool_name` (primary tool only, `String(128)` safe), `tool_provider` (primary server) and `tool_risk_level` (highest selected tool risk). The full tool list, `mcp_servers` and `tool_mcp_map` live in `metadata`.
- **Cost tags**: `CostEvent.allocation_tags["mcp_server_id"|"tool_id"]` and `metadata_payload["mcp_server_id"|"mcp_servers"|"tool_id"|"tools"|"tool_mcp_map"]`.
- **Breakdowns**: `run_cost_summary.tool_breakdown` and `mcp_server_breakdown`, `tenant_cost_summary.top_tools` (no `step_id` leakage), `mcp_server_breakdown` and `top_mcp_servers`. Each settled tool cost row is split evenly across its tools (the last tool absorbs rounding so totals stay exact); MCP server totals follow each tool's owning server.
- **Budgets**: `scope="mcp_server"` (`scope_id` = server id) matches tool cost rows attributed to that server; `scope="tool"` with `tool_id`/`scope_id` now matches the specific tool when accumulating `spent_before`.
- **Pricing**: a `PricingCatalog` row with `resource_type="tool"` and `model_name=<mcp_server_id>` (or `metadata.mcp_server_id`) is preferred for that server; otherwise the generic `internal/tool/per_call` price applies.
- **Filters & export**: `mcp_server_id` query parameter on `GET /v1/cost/tenants/{tenant_id}`, `/export` and `/dashboard/costs`; `TenantCostExportResponse.mcp_server_breakdown`; CSV ledger appends `mcp_server_id,tool_id` (37 columns, existing 35-column prefix unchanged).

## Core Mission & Functional Goals

1. **Burn-Rate & Exhaustion Forecasting (`v1.5.6`)**: Computes real-time hourly and daily burn rates (`max(period_average, recent_window_rate)`), projected period-end spend, and estimated hours to soft/hard limit exhaustion (`burn_status` in `normal | elevated_burn | critical_burn | exhausted`, plus `finops_burn_rate_warning` policy flag) via `GET /v1/cost/tenants/{tenant_id}/forecast` and `/dashboard/costs`.
2. **Dynamic Token Broker (`v1.5.6`)**: Acquires pre-execution token/cost budget leases (`lease_<uuid>`, status `active | settled | released | expired`) in Redis (`tenant:{id}:lease:{run_id}`) and `AgentRun.evaluation_summary["token_budget_lease"]` before task queueing, accounts for sibling active leases in `evaluate_budget_policies()` so concurrent submissions cannot overshoot shared budgets, and automatically settles or releases leases on worker completion, denial, or manual operator release (`POST /v1/cost/leases/{run_id}/release`).
3. **Immutable Cost Integrity, Settled Ledger Deduplication & Model-Aware Routing**: Captures exact snapshots of model pricing within `cost_events.pricing_snapshot`, dynamically resolves routed `provider`, `model_name`, `pricing_mode`, `service_tier`, and `project_id` from `ExecutionPlan.model_route`, and deduplicates pre-execution `estimated` rows via `_settled_cost_events` once `actual` or `failed_retry` events land for the same `(run_id, replay_attempt_id, resume_attempt_id, resource_type, usage_unit)` group.
4. **Multi-Currency FX Normalization & Tax Accounting (`v1.5.5`)**: Resolves `currency` (`TWD`, `USD`, `CNY`, `EUR`, `JPY`, `GBP`), `fx_rate`, `fx_rate_source`, `tax_rate`, `tax_inclusive`, `tax_amount`, and `base_currency_cost` (`FINOPS_BASE_CURRENCY`, default `USD`). All budget enforcement evaluates **pre-tax base-currency amounts** (unless `budget_basis="base_currency"` is explicitly configured), and unmapped foreign currencies record a fail-open `missing_fx_rate` `BudgetViolation`.
5. **Cost-Center Attribution, Manual Adjustments & Reconciliation Ledger Export (`v1.5.5`)**: Extracts `cost_center`, `org_id`, `business_unit`, and `environment` into `ExecutionPlan.model_route["allocation"]` and `CostEvent.allocation_tags` (recording `finops_missing_cost_center` when absent), supports manual reconciliation adjustments via `POST /v1/cost/adjustments` (`cost_adj_...`), and exports tenant cost ledgers in JSON or RFC 4180 CSV via `GET /v1/cost/tenants/{tenant_id}/export`.
6. **Active Pre-Execution & Runtime Budget Enforcement**: Evaluates `run`, `tenant`, `workflow`, `user`, `project`, and `model` budget policies across `per_run`, `daily`, `weekly`, `monthly`, `quarterly`, and `yearly` periods, triggering `fail_closed` (`denied`), `require_approval` (`awaiting_approval`), `degrade_model` (fallback tier routing), `dry_run_only` (`forced_dry_run=True`), or `warn`, plus `cost_type="failed_retry"` accounting on worker failures.
7. **Context-Aware Tiered Pricing & Savings Accounting**: Resolves `context_tier` (`<=128k`, `>128k`, `<=200k`, `>200k`), `minimum_billable_quantity`, `free_quota_Included`, and calculates `cache_savings` (cached input vs. standard input) and `batch_savings` (batch tier vs. standard tier).
8. **Standardized Pricing Seed & Stale Source Guardrail**: Parses official model rate cards from `app/seeds/model_pricing_seed.yaml` supporting dry-run validation, atomic batch applications, and stale source age checks (`PRICING_SOURCE_STALE_DAYS`).
9. **Administrative Governance & Violation Lifecycle**: Provides dedicated Admin REST endpoints (`/v1/admin/finops/pricing/*`, `/v1/budgets/*`, `/v1/budget-violations/*`, `/v1/cost/tenants/{id}/forecast`, `/v1/cost/tenants/{id}/leases`, `/v1/cost/leases/{run_id}/release`, `/v1/cost/adjustments`, `/v1/cost/tenants/{id}/export`) and web portals (`/dashboard/admin/finops/pricing`, `/dashboard/admin/finops/budgets`, `/dashboard/costs`) with role-based access control and manual pricing change log auditing.

---

## Dashboard Navigation & Administration

| Portal Route | Description |
| --- | --- |
| `/dashboard/admin/finops/pricing` | Interactive pricing catalog browser with filters for provider, model, token type, and active status. |
| `/dashboard/admin/finops/pricing/{pricing_id}` | Detailed inspector for a specific price entry, showing effective dates, source links, and change logs. |
| `/dashboard/admin/finops/pricing/import` | Seed importer interface with one-click **Dry Run** analysis, stale source warnings, and **Apply Import** buttons. |
| `/dashboard/admin/finops/pricing/sources` | Forensic audit log tracking upstream rate card URLs, stale status badges, and pricing revisions. |
| `/dashboard/admin/finops/budgets` | Budget policy governance portal for creating and disabling multi-scope/period budget policies. |
| `/dashboard/costs` | Tenant-wide financial dashboard displaying top cost drivers, cache/batch savings, and inline budget violation triage (`Acknowledge`, `Resolve`, `Ignore`). |

---

## Admin & Governance REST API Reference

| Method | Endpoint | Required Role | Description |
| --- | --- | --- | --- |
| `GET` | `/v1/admin/finops/pricing` | `reviewer`+ | Queries full pricing catalog with multi-dimensional filters |
| `GET` | `/v1/admin/finops/pricing/{id}` | `reviewer`+ | Retrieves full metadata for a pricing entry |
| `PATCH`| `/v1/admin/finops/pricing/{id}` | `finops_admin`+ | Updates administrative fields on a pricing entry and records `PricingChangeLog` |
| `POST` | `/v1/admin/finops/pricing/{id}/disable` | `finops_admin`+ | Soft-deletes / supersedes a pricing entry and records `PricingChangeLog` |
| `POST` | `/v1/admin/finops/pricing/import/dry-run` | `reviewer`+ | Evaluates YAML seed and stale sources without database commits |
| `POST` | `/v1/admin/finops/pricing/import/apply` | `finops_admin`+ | Commits YAML seed, creates import batch and change logs |
| `GET` | `/v1/admin/finops/pricing/imports` | `reviewer`+ | Lists historical import batches and reports |
| `GET` | `/v1/admin/finops/pricing/sources` | `reviewer`+ | Lists verified source documentation records |
| `GET` | `/v1/admin/finops/pricing/changes` | `reviewer`+ | Queries audit log of price modifications |
| `GET` | `/v1/budgets` | `viewer`+ | Lists budget policies with optional `scope_type`, `scope_id`, `active_only` filters |
| `POST` | `/v1/budgets` | `finops_admin`+ | Creates or supersedes a budget policy (`supersede_existing`) |
| `GET` | `/v1/budgets/{budget_id}` | `viewer`+ | Retrieves a single budget policy |
| `POST` | `/v1/budgets/{budget_id}/disable` | `finops_admin`+ | Disables an active budget policy |
| `GET` | `/v1/budget-violations` | `viewer`+ | Queries budget violations (`tenant_id`, `run_id`, `status`, `severity`) |
| `GET` | `/v1/budget-violations/{violation_id}` | `viewer`+ | Retrieves a single budget violation |
| `POST` | `/v1/budget-violations/{violation_id}/acknowledge` | `reviewer`+ | Transitions a budget violation to `acknowledged` |
| `POST` | `/v1/budget-violations/{violation_id}/resolve` | `reviewer`+ | Transitions a budget violation to `resolved` or `ignored` |
| `POST` | `/v1/cost/adjustments` | `finops_admin`+ | Records a manual reconciliation adjustment (`cost_type="adjustment"`, `cost_adj_...`) for credits, discounts, or invoice true-ups |
| `GET` | `/v1/cost/tenants/{tenant_id}/export` | `viewer`+ | Exports settled or full tenant cost ledger in JSON or RFC 4180 CSV (`format=json|csv`) |
| `GET` | `/v1/cost/tenants/{tenant_id}/forecast` | `viewer`+ | Computes real-time hourly/daily burn rate, projected period-end spend, and exhaustion warnings |
| `GET` | `/v1/cost/tenants/{tenant_id}/leases` | `viewer`+ | Lists Dynamic Token Broker leases (`active`, `settled`, `released`, `expired`) for a tenant |
| `POST` | `/v1/cost/leases/{run_id}/release` | `operator`+ | Manually releases an active token budget lease for a run |

---

## Model Pricing Seed Ingestion (`scripts/import_pricing_catalog.py`)

Operators can validate or ingest official rate card updates via the CLI:

```bash
# Validate seed syntax and calculate supersession diff without writing to database
python scripts/import_pricing_catalog.py --dry-run --file app/seeds/model_pricing_seed.yaml

# Apply the pricing update atomically and log the operator's identity
python scripts/import_pricing_catalog.py --apply --file app/seeds/model_pricing_seed.yaml --created-by "ops-team"
```

### Ingestion Semantics
- **Unchanged**: Rows matching active pricing records in keys, rates, units, and sources are skipped without modification.
- **Superseded**: When rates differ for an active model and token type, the existing row is marked inactive with `effective_to` set to now, and a new record referencing the old ID via `supersedes_pricing_id` is created.
- **Audit Logging**: An immutable batch report is recorded in `pricing_import_batches`, and individual diffs are saved to `pricing_change_logs`.
