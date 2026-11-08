# Production Runbooks & Incident Response

> **Runbook Version**: `v1.5.7`
> **Previous Versions**: `v1.5.6`, `v1.5.5`, `v1.5.4`, `v1.5.3`, `v1.5.2`, `v1.5.1`, `v1.5.0`, `v1.0.0`

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

### MCP Gateway: Breaker, Blocked Server, Schema Violation & Injection (`v1.5.7`)
- **Open MCP breaker**: `mcp_gateway_registry_health` shows `warn` and runs report `mcp_server_circuit_breaker_open:{id}`. Investigate the server, then reset via `/dashboard/control-plane` (**Reset MCP Breaker**) or `POST /v1/control-plane/mcp-servers/{id}/breaker/close`.
- **Compromised server**: set `trust_tier="blocked"` (or `status="disabled"`) with `POST /v1/control-plane/mcp-servers`; new submissions and already-queued runs are denied (worker re-check) and token leases are released.
- **Schema violations**: review `GovernanceEvent(event_type="mcp_schema_violation")` details (`errors`, `schema_enforcement`); fix the client payload or the tool `input_schema`. Use `POST /v1/control-plane/tools/{tool}/validate-arguments` to reproduce.
- **Injection incidents**: `Incident(code="mcp_argument_injection", severity="high")` blocks the release gate (`blocking_incidents`) until triaged; inspect the redacted `request_payload.tool_arguments` and `metadata.mcp_gateway`.
- **False positives**: adjust the field `x-scan-profile` (e.g. `sql_destructive` or `none` for free-text) instead of disabling scanning globally.

### Post-Execution Review Queue (`v1.5.8`)
- **`review_required` vs `awaiting_approval`**: `awaiting_approval` is a pre-execution gate (use `/dashboard/approvals`). `review_required` means the run already executed and evaluation flagged it (use `/dashboard/reviews`). Never try to approve a `review_required` run; `approve_once` returns `409 run_not_awaiting_approval`.
- **P0 reviews** (test / grader / CI tampering, contradicted test claims): triage within `EVALUATION_REVIEW_P0_SLA_HOURS` (default 4h). `evaluation_review_queue_health` warns when P0 reviews exceed the SLA.
- **Decisions**: `confirmed_hacking` rejects the run and opens a high incident `reward_hacking_confirmed` (blocks the release gate until triaged); `false_positive` / `accepted` complete the run; `needs_info` lets the agent submit more evidence (max `EVALUATION_OUTCOME_MAX_SUBMISSIONS`).
- **Noisy detector**: switch it to shadow mode with `EVALUATION_DETECTOR_MODES='{"<detector>": "shadow"}'` (findings are still recorded) and restart the API/worker; unknown names fail the readiness check.
- **Legitimate test changes**: submit the run with `metadata.allow_test_changes=true` or label the review `accepted`.

### Benchmark Gate, Honeypots & CI Attestation (`v1.5.9`)
- **`evaluation_benchmark_gate` = `warn` (`benchmark_stale`)**: the detector set changed (new `evaluator_version`) or no benchmark exists. Run one from `/dashboard/benchmark` (*Run Benchmark*) or `POST /v1/benchmark/runs`. Benchmark runs have no side effects.
- **Gate below threshold** (`reward_tampering_tpr_below_threshold`): inspect `/dashboard/benchmark` → *Metrics* and the per-sample export for the false positives that set the threshold (on the seed corpus these are `TT-001` on CI edits, new `conftest.py` fixtures and test refactors). Do not lower `EVALUATION_BENCHMARK_MIN_TPR` without a recorded decision; the gate only blocks readiness when `EVALUATION_BENCHMARK_GATE_ENFORCED=true`.
- **Growing the corpus**: after deciding reviews, use *Export to Benchmark* on review detail (or `POST /v1/benchmark/samples/export-reviews`) and add per-finding labels so detector precision can be measured. FPR 1% needs at least 100 benign samples.
- **Promoting a shadow detector**: compare *As configured* with *All enforced (what-if)* on holdout and review-export samples, then set `EVALUATION_DETECTOR_MODES='{"special_casing": "enforce"}'`, restart API and worker, and re-run the benchmark (the gate goes stale on the version change).
- **Honeypot cheated**: the review shows `sample_reason: honeypot` (reviewers/admins only). Treat it like any confirmed-hacking candidate; never reveal probe IDs to agent operators. Disable a probe that has leaked with `POST /v1/honeypots/{probe_id}/enabled`.
- **Attestation failures**: `CE-009` with `replayed` usually means a CI job re-sent an old envelope; `digest_mismatch` means the submitted report differs from what CI signed; `revoked_key` / `unknown_key` mean the trust root is missing. Check `/dashboard/runs/{run_id}` (*Attestation* status and reason) and `/dashboard/benchmark#attestation-keys`. Revoke a compromised key immediately; already-verified outcomes keep their recorded status.

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

### 3. Tool Circuit Breaker Tripped (`v1.5.6`)
- **Symptoms**: Runs requesting a specific tool are escalated to `awaiting_approval` with policy flag `tool_circuit_breaker_open`; tool status shows `open` on `/dashboard/control-plane`.
- **Root Causes**:
  - Consecutive worker failures for the tool reached `TOOL_BREAKER_FAILURE_THRESHOLD` (default `3`), or an operator manually tripped the breaker.
- **Mitigation Steps**:
  1. Verify the health of the downstream tool or service and inspect failure counts on `/dashboard/control-plane` (`GET /v1/control-plane/breakers`).
  2. Click **Reset Breaker** on `/dashboard/control-plane` (`POST /v1/control-plane/tools/{tool_name}/breaker/close`), or reset via Redis CLI:
     ```bash
     redis-cli del "circuit:tool:{tool_name}" "breaker:{tool_name}:failures"
     ```
  3. Re-verify with a test submission via `/dashboard/playground`.

---

### 4. Tenant, User, or Tool Quota Exceeded (HTTP 429) (`v1.5.6`)
- **Symptoms**: Client requests fail with `429 Too Many Requests` (`quota_exceeded` on `tenant_minute`, `tenant_high_risk_hour`, `user_minute`, or `tool_minute`).
- **Root Causes**:
  - Request rate exceeded `TENANT_REQUEST_LIMIT_PER_MINUTE` (60), `TENANT_HIGH_RISK_LIMIT_PER_HOUR` (10), `USER_REQUEST_LIMIT_PER_MINUTE` (30), or `TOOL_CALL_LIMIT_PER_MINUTE` (60).
- **Mitigation Steps**:
  1. Inspect tenant quota events at `/dashboard/governance` and `agent_quota_rejections_total` in `/v1/metrics`.
  2. If legitimate traffic spike, update the tenant's rate limits directly via the **Upsert Tenant & Quotas** form on `/dashboard/governance` (`POST /v1/tenants`).

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

---

### 8. Burn-Rate Exhaustion Warnings & Stuck Token Budget Leases (`v1.5.6`)
- **Symptoms**:
  - Runs include policy flag `finops_burn_rate_warning` and `/dashboard/costs` displays `elevated_burn`, `critical_burn`, or `exhausted` in the **Burn-Rate & Exhaustion Forecast** panel.
  - Concurrent runs are blocked by budget enforcement because queued or awaiting-approval sibling runs hold active **Dynamic Token Broker** leases (`lease_...`).
- **Mitigation Steps**:
  1. **Triage Burn-Rate Warnings**:
     - Open `/dashboard/costs?tenant_id={tenant_id}` or query `GET /v1/cost/tenants/{tenant_id}/forecast` to inspect `hourly_burn_rate`, `projected_period_cost`, and `hours_to_hard_limit`.
     - Enable `degrade_model` on the tenant's soft limit or raise the budget policy cap at `/dashboard/admin/finops/budgets`.
  2. **Release Stuck Token Budget Leases**:
     - Inspect active leases in the **Dynamic Token Broker — Active & Recent Budget Leases** table on `/dashboard/costs` (`GET /v1/cost/tenants/{tenant_id}/leases?active_only=true`).
     - Click **Release Lease** next to any stalled run (or call `POST /v1/cost/leases/{run_id}/release`) to immediately free the reserved budget capacity.

---

### 9. Judge Triage Failures, Token Cap & Review Sampling Skips (`v1.5.10`)
- **Symptoms**:
  - `evaluation_judge_health` warns (`judge calls failed in 24h`, external endpoint blocked, same-family warnings in prod) or fails (`openai_compatible` without base URL / model).
  - Review triage shows `fallback: deterministic:1.0.0` and assessment statuses `timeout`, `error`, `invalid_output`, `blocked_external` or `budget_exceeded`.
  - `evaluation_sampling_health` warns about skipped candidates, a missing `EVALUATION_SAMPLE_SECRET`, or a random slice that selected nothing.
- **Safety note**: none of these affect verdicts. Rule verdicts and review priorities are computed before the judge runs, and every judge failure falls back to the deterministic judge.
- **Mitigation Steps**:
  1. **Judge errors**: check `GET /v1/evaluations/judge/status` (status counts, external flags, token usage). Verify `EVALUATION_JUDGE_BASE_URL` / `EVALUATION_JUDGE_MODEL`, the endpoint health and `EVALUATION_JUDGE_TIMEOUT_SECONDS`. For hosted APIs set `EVALUATION_JUDGE_ALLOW_EXTERNAL=true` only after confirming the data-sharing terms. To stop LLM calls immediately set `EVALUATION_JUDGE_PROVIDER=deterministic` (or `EVALUATION_JUDGE_MODE=off`).
  2. **Token cap reached**: raise `EVALUATION_JUDGE_DAILY_TOKEN_CAP_PER_TENANT` or wait for the UTC day to roll over; judge costs appear in the ledger as `step_id=evaluation_judge`. Add a pricing row for the judge model to clear `missing_pricing_rule` violations.
  3. **Same-family warning**: choose a judge from a different model family than the monitored agents.
  4. **Re-assess a review**: `/dashboard/reviews/{review_id}` → **Re-assess** (or `POST /v1/reviews/{review_id}/judge-assessments`); unchanged evidence returns the existing assessment.
  5. **Sampling skips**: inspect `GET /v1/evaluations/sampling/summary` and `/dashboard/reviews#sampling`. Raise `EVALUATION_SAMPLE_DAILY_CAP_PER_TENANT` / `EVALUATION_SAMPLE_MAX_SHARE` only if reviewers have capacity; keep the random slice small but non-zero. Set `EVALUATION_SAMPLE_SECRET` in production so draws are reproducible.
  6. **Switching to `triage` mode**: run `POST /v1/benchmark/runs` with `judge_provider` on holdout and review-export corpora, confirm `metrics.triage.promotion_criteria_met = true`, then set `EVALUATION_JUDGE_MODE=triage` and restart. Roll back by setting `shadow`.

---

### 10. Local / Cloud Judge Profiles (`v1.5.11`)
- **Local server unreachable from containers** (assessments `error` / `timeout`, probe fails): check `curl http://localhost:<port>/v1/models` on the host, then `docker compose exec -T api python -c "import httpx;print(httpx.get('http://host.docker.internal:<port>/v1/models').status_code)"`. Use `host.docker.internal`, not the `169.254.x.x` address LM Studio may display. Make sure the model is loaded.
- **`invalid_output` / `empty_content` with a reasoning model**: the model spent `max_output_tokens` thinking. Set `reasoning_effort: none` on the profile (LM Studio honours it; prompt tags are ignored) or raise `max_output_tokens`.
- **Pending triage piling up** (`evaluation_judge_health` warns): the background tasks were lost or the model is too slow. Run **Sweep** on `/dashboard/benchmark#pending-triage` (or `POST /v1/evaluations/judge/sweep`), raise `timeout_seconds`, or switch `EVALUATION_JUDGE_PROFILE` back to `deterministic`.
- **External judge returns `blocked_external`**: set `EVALUATION_JUDGE_ALLOW_EXTERNAL=true` and record the tenant's consent in *External judge consent*; check `api_key_configured` on the profiles table.
- **Switching profiles**: edit `config/judge_profiles.yaml` (no rebuild; it is mounted) and set `EVALUATION_JUDGE_PROFILE`, then restart `api` and `worker`. Invalid profiles stop startup with a message naming the field.
