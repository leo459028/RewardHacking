# REST API Specification

> **API Version**: `v1.5.7`
> **Base Path**: `/v1`  
> **Interactive Documentation**: `http://localhost:8000/docs` (Swagger UI) or `http://localhost:8000/redoc` (ReDoc)

All REST API endpoints are exposed under the `/v1` route prefix. The service returns and accepts JSON payloads formatted in UTF-8.

---

## Authentication & Headers

- **`X-API-Key`**: API key header when `AUTH_ENABLED=true`. Maps the request to an authorized tenant and RBAC role (`viewer`, `operator`, `admin`, `finops_admin`).
- **`X-Trace-Id`**: Distributed trace identifier. If provided in the request header, it is bound to the execution plan and audit events; otherwise, the server generates a unique trace ID and returns it in the response header.

---

## Endpoint Catalog

### System & Health

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `GET` | `/v1/healthz` | Process liveness health check | Public |
| `GET` | `/v1/readyz` | PostgreSQL connection readiness check | Public |
| `GET` | `/v1/metrics` | Prometheus metrics scrape endpoint | Public / Authenticated |

### Agent Request Submission

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `POST` | `/v1/agent/submit` | Ingests agent task; returns compact decision summary | `operator`, `admin` |
| `POST` | `/v1/agent/submit-with-details` | Ingests agent task; returns full policy, risk, plan, and evaluation details | `operator`, `admin` |

#### Submit Request Payload Example
```json
{
  "tenant_id": "default",
  "user_id": "user-101",
  "task_type": "code_fix",
  "prompt": "Fix payment retry logic without modifying core business rules.",
  "goal": "Generate a verified patch",
  "allowed_tools": ["read_repo", "write_patch", "run_tests"],
  "constraints": {
    "no_external_network": true,
    "requires_human_approval": false,
    "max_tool_calls": 8
  }
}
```

---

### Runs & Execution Inspection

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `GET` | `/v1/runs` | Queries historical runs (supports `tenant_id`, `status`, `limit`) | `viewer`, `operator`, `admin` |
| `GET` | `/v1/runs/{run_id}` | Retrieves details for a specific run | `viewer`, `operator`, `admin` |
| `GET` | `/v1/runs/{run_id}/execution-plan` | Retrieves the pre-execution plan and tool registry snapshots | `viewer`, `operator`, `admin` |
| `GET` | `/v1/runs/{run_id}/trace` | Queries execution timeline, step audits, and trace metadata | `viewer`, `operator`, `admin` |
| `GET` | `/v1/runs/{run_id}/replay-bundle` | Exports a self-contained, reproducible replay bundle for offline audit | `viewer`, `operator`, `admin` |
| `GET` | `/v1/runs/{run_id}/checkpoints` | Lists submit/resume checkpoints associated with the run | `viewer`, `operator`, `admin` |
| `POST` | `/v1/runs/{run_id}/resume` | Creates a resume checkpoint and re-publishes the run to Kafka queue | `operator`, `admin` |
| `POST` | `/v1/runs/{run_id}/replay` | Triggers a dry-run replay attempt without external side-effects | `operator`, `admin` |

---

### Human Approval Queue

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `GET` | `/v1/approvals` | Queries runs currently in `awaiting_approval` status | `viewer`, `operator`, `admin` |
| `POST` | `/v1/approvals/{run_id}/decision` | Submits human review decision (`approve_once`, `deny`, `request_changes`) | `operator`, `admin` |
| `POST` | `/v1/approvals/expire-stale` | Sweeps pending approvals past TTL (`expires_at <= now`) to `expired` / `denied` | `operator`, `admin` |

#### Approval Decision Payload Example
```json
{
  "decision": "approve_once",
  "reviewer": "alice@company.com",
  "reason": "Verified that database migration script has passed manual staging review."
}
```

---

### Control Plane & Registry

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `GET` | `/v1/control-plane/workflows` | Lists available workflow definitions | `viewer`, `operator`, `admin` |
| `POST` | `/v1/control-plane/workflows` | Creates or updates workflow definition | `admin` |
| `GET` | `/v1/control-plane/tools` | Lists registered tools, parameters, and risk classifications | `viewer`, `operator`, `admin` |
| `POST` | `/v1/control-plane/tools` | Registers or modifies a tool definition | `admin` |
| `GET` | `/v1/control-plane/breakers` | Lists tool circuit breaker states (`open`/`closed`) and recent failure counts | `viewer`, `operator`, `admin` |
| `POST` | `/v1/control-plane/tools/{tool_name}/breaker/open` | Manually trips a tool circuit breaker to `open` | `operator`, `admin` |
| `POST` | `/v1/control-plane/tools/{tool_name}/breaker/close` | Resets a tool circuit breaker to `closed` and clears failure counters | `operator`, `admin` |

---

### Agent FinOps Core & Cost Ledger

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `GET` | `/v1/pricing` | Lists active model pricing catalog entries | `viewer`, `operator`, `admin` |
| `POST` | `/v1/pricing` | Creates pricing catalog item | `admin`, `finops_admin` |
| `PATCH` | `/v1/pricing/{pricing_id}` | Updates existing pricing catalog entry | `admin`, `finops_admin` |
| `DELETE` | `/v1/pricing/{pricing_id}` | Soft-deletes / supersedes pricing entry | `admin`, `finops_admin` |
| `POST` | `/v1/usage-events` | Ingests a usage event and generates immutable cost event | `operator`, `admin` |
| `GET` | `/v1/usage-events/runs/{run_id}` | Retrieves usage event ledger for a run | `viewer`, `operator`, `admin` |
| `GET` | `/v1/cost/runs/{run_id}` | Queries estimated, actual, settled, FX, tax, adjustment, and token lease summary for a run | `viewer`, `operator`, `admin` |
| `GET` | `/v1/cost/tenants/{tenant_id}` | Queries tenant settled cost totals, tax/billed/reconciled totals, burn-rate forecast, active token leases, cost center & currency breakdowns, top models, and savings (supports `project_id`, `cost_center`) | `viewer`, `operator`, `admin` |
| `GET` | `/v1/cost/tenants/{tenant_id}/forecast` | Computes hourly/daily burn rate, projected period-end cost, and time-to-exhaustion warnings (`window_hours`, `project_id`) | `viewer`, `operator`, `admin`, `finops_admin` |
| `GET` | `/v1/cost/tenants/{tenant_id}/leases` | Lists Dynamic Token Broker leases for a tenant (`active_only=true|false`) | `viewer`, `operator`, `admin`, `finops_admin` |
| `POST` | `/v1/cost/leases/{run_id}/release` | Manually releases an active token budget lease for a run (`status="released"`) | `operator`, `admin`, `finops_admin` |
| `GET` | `/v1/cost/tenants/{tenant_id}/export` | Exports tenant cost ledger as JSON or RFC 4180 CSV (`format=json|csv`, `settled_only`, `project_id`, `cost_center`, `reconciled`) | `viewer`, `operator`, `admin`, `finops_admin` |
| `POST` | `/v1/cost/adjustments` | Creates a manual reconciliation adjustment (`cost_type="adjustment"`, `cost_adj_...`) with optional `invoice_id` and `mark_run_reconciled` | `admin`, `finops_admin` |
| `GET` | `/v1/budgets` | Lists budget policies and limit thresholds (`scope_type`, `scope_id`, `active_only`) | `viewer`, `operator`, `admin` |
| `POST` | `/v1/budgets` | Configures new budget policy or supersedes existing policy (`supersede_existing`) | `admin`, `finops_admin` |
| `GET` | `/v1/budgets/{budget_id}` | Retrieves details for a specific budget policy | `viewer`, `operator`, `admin` |
| `POST` | `/v1/budgets/{budget_id}/disable` | Soft-disables an active budget policy | `admin`, `finops_admin` |
| `GET` | `/v1/budget-violations` | Queries budget overrun violations (`tenant_id`, `run_id`, `status`, `severity`) | `viewer`, `operator`, `admin` |
| `GET` | `/v1/budget-violations/{violation_id}` | Retrieves details for a specific budget violation | `viewer`, `operator`, `admin` |
| `POST` | `/v1/budget-violations/{violation_id}/acknowledge` | Transitions a budget violation to `acknowledged` | `reviewer`, `admin`, `finops_admin` |
| `POST` | `/v1/budget-violations/{violation_id}/resolve` | Transitions a budget violation to `resolved` or `ignored` | `reviewer`, `admin`, `finops_admin` |

---

### Tenant Governance & Administration

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `GET` | `/v1/tenants` | Lists configured tenants and quota overrides | `admin` |
| `POST` | `/v1/tenants` | Provisions a new tenant | `admin` |
| `GET` | `/v1/tenants/{tenant_id}/api-keys`| Lists hashed API keys for a tenant | `admin` |
| `POST` | `/v1/tenants/{tenant_id}/api-keys`| Generates new API key (plaintext returned once) | `admin` |
| `GET` | `/v1/governance/events` | Queries tenant policy and quota event log | `viewer`, `operator`, `admin` |

---

### Operations Gate & Incident Management

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `GET` | `/v1/operations/readiness` | Evaluates release readiness criteria | `viewer`, `operator`, `admin` |
| `POST` | `/v1/operations/evidence` | Submits operational evidence (backup drill, secret rotation, safety regression) | `admin` |
| `POST` | `/v1/operations/release-gates` | Creates an auditable release gate snapshot | `admin` |
| `GET` | `/v1/incidents` | Lists safety incidents and policy denials | `viewer`, `operator`, `admin` |
| `GET` | `/v1/worker/dlq` | Lists worker attempts routed to dead letter queue | `viewer`, `operator`, `admin` |
| `POST` | `/v1/worker/dlq/{run_id}/requeue` | Requeues a DLQ run (`status="queued"`) and republishes `agent.requested` | `operator`, `admin` |
| `POST` | `/v1/worker/dlq/{run_id}/resolve` | Marks DLQ worker attempts as `dlq_resolved` to clear readiness gate | `operator`, `admin` |

### Safe MCP Gateway (`v1.5.7`)

| Method | Path | Description | Access Role |
| --- | --- | --- | --- |
| `GET` | `/v1/control-plane/mcp-servers` | Lists builtin and custom MCP servers with trust tier, transport, external-network flag, quota and `breaker_open` | `viewer`, `operator`, `admin` |
| `POST` | `/v1/control-plane/mcp-servers` | Upserts an MCP server (`mcp_server_id` `^[a-zA-Z0-9._-]{2,113}$`, `transport`, `endpoint`, `trust_tier`, `is_external_network` auto-detected from the parsed hostname when omitted, `status`, `requires_approval`, `risk_level`, `quota_per_minute`, `allowed_tenants`); records `GovernanceEvent(mcp_server.upserted)` | `admin` |
| `POST` | `/v1/control-plane/mcp-servers/{mcp_server_id}/breaker/open` | Opens the MCP server breaker (`circuit:mcp:{id}`); runs using its tools escalate to `awaiting_approval` | `admin` |
| `POST` | `/v1/control-plane/mcp-servers/{mcp_server_id}/breaker/close` | Closes the MCP server breaker | `admin` |
| `POST` | `/v1/control-plane/tools/{tool_name}/validate-arguments` | Validates `{"arguments": {...}}` against the tool `input_schema` and scan profiles; returns `valid`, `errors`, `injection_flags`, `sanitized_arguments`, `redacted_fields` | `viewer`, `operator`, `admin` |

**`AgentRequest.tool_arguments`** (optional, `dict[tool_name, dict]`, also read from `metadata.tool_arguments`): validated and redacted before persistence. Arguments for tools not in `allowed_tools`, nesting deeper than `MCP_TOOL_ARGUMENTS_MAX_DEPTH`, or payloads larger than `MCP_TOOL_ARGUMENTS_MAX_BYTES` raise `mcp_tool_schema_violation:{tool}`.

**MCP policy flags**: hard-deny (`status="denied"`, no cost/lease) — `mcp_server_blocked:{id}`, `mcp_tenant_not_allowed:{id}`, `mcp_argument_injection_detected:{tool}`, `mcp_tool_schema_violation:{tool}` (when `schema_enforcement="deny"`); escalate (`awaiting_approval`) — `mcp_untrusted_server:{id}`, `mcp_external_network_violation:{id}`, `mcp_server_circuit_breaker_open:{id}`, `mcp_tool_schema_violation:{tool}` (when `schema_enforcement="escalate"`).

**Cost endpoints**: `GET /v1/cost/tenants/{tenant_id}` and `/export` accept `mcp_server_id`; responses add `mcp_server_breakdown` (and `top_mcp_servers` on the summary); `GET /v1/cost/runs/{run_id}` adds `mcp_server_breakdown`; `CostEventRecord` adds `mcp_server_id` and `tool_id`. `POST /v1/budgets` accepts `scope="mcp_server"`.

---

## Common Error Codes

| Status Code | Error Code | Description |
| --- | --- | --- |
| `400` | `invalid_input` | Malformed parameters or unsupported enum value |
| `401` | `unauthorized` | Missing or invalid API key |
| `403` | `forbidden` | Role lacks necessary permissions for operation |
| `404` | `not_found` | Resource does not exist |
| `422` | `validation_error` | Payload failed Pydantic schema validation |
| `429` | `quota_exceeded` | Tenant exceeded request limit or high-risk rate limit |
| `429` | `mcp_server_quota_exceeded` | Per-MCP-server call quota exceeded (`quota_per_minute` or `MCP_SERVER_CALL_LIMIT_PER_MINUTE`) |
| `422` | `validation_error` (reserved prefix) | `tool_name` starting with `__mcp_server__:` or `mcp_server_id` longer than 113 characters |
| `500` | `internal_error` | Internal server or unhandled exception |
