# Production Runbooks & Incident Response

> **Runbook Version**: `v1.5.5`
> **Previous Versions**: `v1.5.4`, `v1.5.3`, `v1.5.2`, `v1.5.1`, `v1.5.0`, `v1.0.0`

This operational runbook provides triage instructions, troubleshooting procedures, and incident response playbooks for operators maintaining the **RewardHacking** platform.

---

## Quick Health Checks

```bash
# 1. API process liveness
curl -f http://localhost:8000/v1/healthz

# 2. PostgreSQL readiness
curl -f http://localhost:8000/v1/readyz

# 3. Prometheus metrics output
curl -s http://localhost:8000/v1/metrics | head -n 20

# 4. Container status
docker compose ps

# 5. Worker logs
docker compose logs --tail=100 -f worker
```

---

## Operational Incident Playbooks

### 1. API Gateway Startup Failure
- **Symptoms**: Container crashes on startup; `/v1/healthz` returns connection refused; traceback in logs.
- **Root Causes**:
  - Unapplied database migrations or PostgreSQL unreachable.
  - Missing environment variables in `.env`.
- **Mitigation Steps**:
  1. Inspect startup logs: `docker compose logs api`.
  2. Verify database connection string `POSTGRES_DSN`.
  3. Run migrations manually: `alembic upgrade head`.

---

### 2. Worker Dead Letter Queue (DLQ) Accumulation
- **Symptoms**: Elevated `agent_worker_dlq_total` metric; tasks accumulating in `/dashboard/worker`.
- **Root Causes**:
  - Repeated tool execution failures exceeding `WORKER_MAX_ATTEMPTS` (default: 3).
  - Downstream tool timeouts or persistent network failure.
  - Serialization errors during evaluator execution.
- **Mitigation Steps**:
  1. Query DLQ records: `GET /v1/worker/dlq` or open `/dashboard/worker`.
  2. Inspect the failure reason and tool transcript on the Run Detail page (`/dashboard/runs/{run_id}`).
  3. If transient tool issues have resolved, click **Requeue Run** on `/dashboard/worker` (`POST /v1/worker/dlq/{run_id}/requeue`) or resume from checkpoint via `/dashboard/runs/{run_id}` (`POST /v1/runs/{run_id}/resume`).
  4. If the failure is permanently triaged, click **Resolve DLQ** (`POST /v1/worker/dlq/{run_id}/resolve`) to clear the `worker_dlq` readiness gate.
  5. For stale pending approvals blocking the `expired_approvals` check, click **Expire Stale Approvals** on `/dashboard/approvals` (`POST /v1/approvals/expire-stale`).

---

### 3. Tool Circuit Breaker Tripped
- **Symptoms**: Low-risk runs are suddenly blocked with `tool_circuit_breaker_tripped` errors; tool status shows open in Redis.
- **Root Causes**:
  - Consecutive failures for a designated tool exceeded threshold within the sliding window.
- **Mitigation Steps**:
  1. Verify the health of the downstream tool or service.
  2. Reset the circuit breaker state key in Redis:
     ```bash
     redis-cli del "circuit_breaker:tool:{tool_name}"
     ```
  3. Re-verify with a test submission via `/dashboard/playground`.

---

### 4. Tenant Quota Exceeded (HTTP 429)
- **Symptoms**: Client requests fail with `429 Too Many Requests` (`quota_exceeded`).
- **Root Causes**:
  - Tenant exceeded `TENANT_REQUEST_LIMIT_PER_MINUTE` (default: 60) or `TENANT_HIGH_RISK_LIMIT_PER_HOUR` (default: 10).
- **Mitigation Steps**:
  1. Inspect tenant event history at `/dashboard/governance`.
  2. If legitimate traffic spike, adjust quota override via `POST /v1/tenants` or modify tenant settings in database.

---

### 5. Missing Model Pricing Catalog Data or Stale Pricing Sources
- **Symptoms**: Run Detail shows zero or missing values under `Cost Summary`; `/dashboard/costs` shows empty catalog; `/dashboard/admin/finops/pricing/sources` shows `stale` badge.
- **Mitigation Steps**:
  1. Navigate to `/dashboard/admin/finops/pricing/import`.
  2. Run a Dry Run validation of `app/seeds/model_pricing_seed.yaml` and inspect `stale_sources`.
  3. Click **Apply Import** or execute via CLI:
     ```bash
     python scripts/import_pricing_catalog.py --apply --file app/seeds/model_pricing_seed.yaml
     ```

---

### 6. FinOps Budget Overrun & Violation Triage
- **Symptoms**: Submissions are blocked (`status=denied`, `reason_code=budget_exceeded`), routed to human review (`status=awaiting_approval`, `reason_code=budget_requires_approval`), or degraded (`budget_model_degraded`); `finops_open_critical_violations` warns on `/dashboard/operations`.
- **Mitigation Steps**:
  1. Inspect open violations at `/dashboard/costs` or `GET /v1/budget-violations?status=open`.
  2. Acknowledge active triage via the inline **Acknowledge** button or `POST /v1/budget-violations/{id}/acknowledge`.
  3. Adjust or supersede budget policies at `/dashboard/admin/finops/budgets` (`POST /v1/budgets` with `supersede_existing=true`), then resolve the violation via **Resolve** (`POST /v1/budget-violations/{id}/resolve`).

---

### 7. Multi-Currency `missing_fx_rate` Violations, Manual Adjustments & Ledger Reconciliation (`v1.5.5`)
- **Symptoms**:
  - A `BudgetViolation` with `violation_type=missing_fx_rate` (`severity=warning`) appears on `/dashboard/costs`, and the `CostEvent` records `fx_rate_source=missing_fx_fallback_1_to_1`.
  - Finance requires applying promotional credits, negotiated discounts, or invoice true-ups (`invoice_id`) and exporting a monthly reconciliation ledger.
- **Mitigation & Reconciliation Steps**:
  1. **Fix Unmapped Currency (`missing_fx_rate`)**:
     - Open `/dashboard/admin/finops/pricing/{pricing_id}` and set an explicit **FX Rate to Base** on the pricing entry, or add the currency code to `FINOPS_FX_RATES` in `.env` and restart the `api` and `worker` containers.
     - Acknowledge and resolve the `missing_fx_rate` violation on `/dashboard/costs`.
  2. **Record Manual Cost Adjustments & Invoice True-Ups**:
     - Navigate to `/dashboard/costs` and submit the **Record Cost Adjustment** form (or call `POST /v1/cost/adjustments`) with a negative credit (e.g., `-0.005000`) or positive fee, `cost_center`, `invoice_id`, and check **Mark run events reconciled** if reconciling a specific `run_id`.
  3. **Export Reconciliation Ledger**:
     - Click **Export CSV** or **Export JSON** on `/dashboard/costs` (or call `GET /v1/cost/tenants/{tenant_id}/export?format=csv&settled_only=true`) to download the settled, formula-sanitized RFC 4180 CSV ledger for accounting import.
