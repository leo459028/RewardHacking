# Operational Dashboard Guide

> **Dashboard Version**: `v1.5.7`
> **Previous Versions**: `v1.5.6`, `v1.5.5`, `v1.5.4`, `v1.5.3`, `v1.5.2`, `v1.5.1`, `v1.5.0`, `v1.4.0`
> **Access URL**: `http://localhost:8000/dashboard`

RewardHacking includes a lightweight, server-rendered (Jinja2) web dashboard providing full observability, human-in-the-loop governance, and FinOps administration without requiring separate Node.js or React build pipelines.

---

## Navigation & Page Index

| Path | Page Title | Primary Functionality |
| --- | --- | --- |
| `/dashboard` | **Overview** | System summary: total runs, status distribution, average risk scores, active flags, and recent incidents. |
| `/dashboard/runs` | **Runs Explorer** | Searchable audit list of agent runs with filters for status, tenant, and task type. |
| `/dashboard/runs/{run_id}` | **Run Detail** | Comprehensive forensic dossier: policy decision, Classical Chinese prompt guard, execution plan, checkpoint timeline, one-click **Trigger Dry-Run Replay** & **Resume from Checkpoint**, step audit, tool transcripts, MCP Server & Tool cost breakdowns, tool executions with `mcp_server_id` and `sanitized_arguments` (`v1.5.7`), FinOps cost summary (including Settled Total, Tax, Billed, Adjustments, Reconciled Total, FX rates, and Cost Center), and budget violations. |
| `/dashboard/approvals` | **Approval Queue** | Actionable queue for high-risk / destructive runs. Operators can execute `approve_once`, `deny`, `request_changes`, or **Expire Stale Approvals**. |
| `/dashboard/worker` | **Worker Operations** | Background worker telemetry: active attempts, retry backoffs, and Dead Letter Queue (DLQ) with inline **Requeue Run** and **Resolve DLQ** controls. |
| `/dashboard/observability` | **Observability & SLOs** | Aggregated SLI/SLO trends, incident resolution rates, evaluator scores, approval turnaround latencies, and FinOps summary. |
| `/dashboard/governance` | **Tenant Governance** | Interactive multi-tenant governance portal: Tenant Upsert form (`request_limit_per_minute`, `high_risk_limit_per_hour`), API Key Issuance form (with one-time plaintext key banner) & Revoke buttons, and governance security audit logs. |
| `/dashboard/control-plane` | **Control Plane** | Interactive Control Plane registry: Tool Registry Upsert form, Circuit Breaker status badges (`open`/`closed`), failure counters & inline **Trip Breaker** / **Reset Breaker** buttons, Workflow Registry Upsert form, **MCP Server Registry** (`v1.5.7`: Register / Update MCP Server form, trust tier / transport / external-network / quota table, inline **Trip MCP Breaker** / **Reset MCP Breaker**), tool `MCP Server ID` / `Required Args (CSV)` / `Sensitive Args (CSV)` fields and `MCP Server` / `Schema / Redaction` columns, pre-execution plans, live step completion (`3/3 completed`, `healthy`), and resumable checkpoints. |
| `/dashboard/costs` | **FinOps Costs** | Tenant settled cost totals (`Settled MTD`, `Tax`, `Billed`, `Adjustments`, `Reconciled MTD`), **Burn-Rate & Exhaustion Forecast** cards and policy table (`Hourly Burn`, `Projected Month-End`, `Burn Status`), **Dynamic Token Broker** active lease table with inline **Release Lease** buttons, **MCP Server Cost Breakdown** & **Top Tools** panels with `mcp_server_id` filter (`v1.5.7`), cost center & currency breakdowns, manual reconciliation adjustment form, **Export CSV** / **Export JSON** ledger downloads, model expense rankings, active budget policies, and inline budget violation triage (`Acknowledge`, `Resolve`, `Ignore`). |
| `/dashboard/admin/finops/pricing` | **Pricing Catalog** | Admin management interface for the model pricing catalog (`currency`, `fx_rate`, `tax_rate`, `tax_inclusive` editing on Pricing Detail). |
| `/dashboard/admin/finops/pricing/import` | **Pricing Import** | Dry-run validation, stale source detection, and application of official model pricing YAML seeds. |
| `/dashboard/admin/finops/pricing/sources` | **Source Audit** | Versioned pricing change logs, stale source badges, and official documentation source tracking. |
| `/dashboard/admin/finops/budgets` | **Budget Policies** | FinOps budget governance portal for creating, superseding, and disabling multi-scope/period budget policies. |
| `/dashboard/operations` | **Operations Gate** | Production release gate checklist (including FinOps pricing catalog and critical violation checks), operational evidence logging, the `mcp_gateway_registry_health` check, and `v1.5.7` release snapshot management. |
| `/dashboard/playground` | **Interactive Playground** | Rapid experimentation console with FinOps model routing (`provider`, `model_name`, `pricing_mode`, `project_id`, `cost_center`, `org_id`) and optional **Tool Arguments (JSON)** for MCP schema validation & redaction testing (`v1.5.7`, raw JSON is never echoed back) to submit test prompts and inspect guardrail verdicts in real time. |

---

## Operational Workflows

### 1. Reviewing Escalated Runs in the Approval Queue (`/dashboard/approvals`)
When an agent submits an operation with high risk (e.g. `destructive=true`, sensitive database mutation, or unauthorized network flags):
1. The policy engine flags the request as `escalated` and sets the run status to `awaiting_approval`.
2. The run appears on `/dashboard/approvals` with an active TTL countdown timer (default: 3600 seconds).
3. The human reviewer examines the prompt, proposed tools, and risk flags.
4. The reviewer selects one of three actions:
   - **Approve Once**: Transitions the run to `queued` and publishes it to Kafka for worker execution.
   - **Deny**: Permanently rejects the task, sets status to `denied`, and logs an incident event.
   - **Request Changes**: Rejects with notes for prompt revision.

### 2. Forensic Auditing via Run Detail (`/dashboard/runs/{run_id}`)
The Run Detail view aggregates the full end-to-end lifecycle of a single agent task:
- **Trace & Replay Section**: Displays the unified `X-Trace-Id` and provides a direct link to download the JSON Replay Bundle (`/v1/runs/{run_id}/replay-bundle`).
- **Checkpoints**: Displays timestamps and states for the original submission checkpoint and any subsequent resume checkpoints.
- **Cost Summary**: Breaks down estimated vs. actual expenses across prompt input tokens, output tokens, cached tokens, tool calls, and worker execution seconds.
- **Prompt Guard**: Shows detected adversarial signals (such as classical Chinese injection markers or bypass commands).

### 3. Production Operations Release Gate (`/dashboard/operations`)
In production environments (`APP_ENV=prod`), deployments must meet strict release criteria:
1. Verify that no unresolved blocking incidents exist.
2. Confirm the Dead Letter Queue (DLQ) is completely drained.
3. Submit required operational evidence:
   - Backup/restore drill verification.
   - Secret rotation compliance (< 90 days).
   - Deterministic safety regression test evidence.
4. Click **Create Release Gate Snapshot** with version `v1.5.5` to record the official gate sign-off in the audit trail.
