# Operational Dashboard Guide

> **Dashboard Version**: `v1.5.2`  
> **Previous Versions**: `v1.5.1`, `v1.5.0`, `v1.4.0`  
> **Access URL**: `http://localhost:8000/dashboard`

RewardHacking includes a lightweight, server-rendered (Jinja2) web dashboard providing full observability, human-in-the-loop governance, and FinOps administration without requiring separate Node.js or React build pipelines.

---

## Navigation & Page Index

| Path | Page Title | Primary Functionality |
| --- | --- | --- |
| `/dashboard` | **Overview** | System summary: total runs, status distribution, average risk scores, active flags, and recent incidents. |
| `/dashboard/runs` | **Runs Explorer** | Searchable audit list of agent runs with filters for status, tenant, and task type. |
| `/dashboard/runs/{run_id}` | **Run Detail** | Comprehensive forensic dossier: policy decision, Classical Chinese prompt guard, execution plan, checkpoint timeline, replay bundle, step audit, tool transcripts, and FinOps cost summary. |
| `/dashboard/approvals` | **Approval Queue** | Actionable queue for high-risk / destructive runs. Operators can execute `approve_once`, `deny`, or `request_changes`. |
| `/dashboard/worker` | **Worker Operations** | Background worker telemetry: active attempts, retry backoffs, and Dead Letter Queue (DLQ). |
| `/dashboard/observability` | **Observability & SLOs** | Aggregated SLI/SLO trends, incident resolution rates, evaluator scores, and approval turnaround latencies. |
| `/dashboard/governance` | **Tenant Governance** | Multi-tenant overview: tenant quotas, active hashed API keys, and governance security audit logs. |
| `/dashboard/control-plane` | **Control Plane** | Workflow registry, registered tools, pre-execution plans, trace IDs, and resumable checkpoints. |
| `/dashboard/costs` | **FinOps Costs** | Tenant cost totals, top spenders, model expense rankings, and budget violation alerts. |
| `/dashboard/admin/finops/pricing` | **Pricing Catalog** | Admin management interface for the model pricing catalog. |
| `/dashboard/admin/finops/pricing/import` | **Pricing Import** | Dry-run validation and application of official model pricing YAML seeds. |
| `/dashboard/admin/finops/pricing/sources` | **Source Audit** | Versioned pricing change logs and official documentation source tracking. |
| `/dashboard/operations` | **Operations Gate** | Production release gate checklist, operational evidence logging (backup drills, secret rotation, safety regressions), and `v1.5.2` release snapshot management. |
| `/dashboard/playground` | **Interactive Playground** | Rapid experimentation console to submit test prompts and inspect guardrail verdicts in real time. |

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
4. Click **Create Release Gate Snapshot** with version `v1.5.2` to record the official gate sign-off in the audit trail.
