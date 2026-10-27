# REST API Specification

> **API Version**: `v1.5.3`
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
| `GET` | `/v1/cost/runs/{run_id}` | Queries estimated vs. actual cost summary for a run | `viewer`, `operator`, `admin` |
| `GET` | `/v1/cost/tenants/{tenant_id}` | Queries tenant cost totals, top models, workflow costs, and cache/batch savings | `viewer`, `operator`, `admin` |
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
| `500` | `internal_error` | Internal server or unhandled exception |
