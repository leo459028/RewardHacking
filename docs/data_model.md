# Database Schema & Audit Models

> **Data Model Version**: `v1.5.3`
> **ORM**: SQLAlchemy (asyncio)  
> **Migration Baseline**: Alembic revision `202606010004` (head)

This specification details the relational data models and audit schemas maintained in PostgreSQL for the **RewardHacking** platform.

---

## Core Audit Trail Models

### 1. `AgentRun` (`agent_runs`)
Stores the complete state and evaluation dossier for every submitted agent task.

| Column | Type | Nullable | Description |
| --- | --- | --- | --- |
| `id` | Integer | No | Primary Key |
| `run_id` | String(64) | No | Unique external UUID identifier (Unique Index) |
| `tenant_id` | String(64) | No | Owning tenant identifier (Index) |
| `user_id` | String(64) | No | Submitting user ID (Index) |
| `task_type` | String(64) | No | Task classification (e.g. `code_fix`, `infra_deploy`) |
| `prompt` | Text | No | Raw input prompt |
| `goal` | Text | No | Stated objective of the agent task |
| `status` | String(32) | No | Status (`queued`, `running`, `awaiting_approval`, `completed`, `denied`, `failed`) |
| `decision_reason` | Text | Yes | Policy engine justification |
| `risk_score` | Float | No | Computed composite risk score (0.0 - 1.0) |
| `policy_flags` | JSON | No | List of triggered policy flags |
| `request_payload`| JSON | No | Full verbatim submission JSON payload |
| `execution_output`| Text | Yes | Bounded executor output summary |
| `evaluation_summary`| JSON | Yes | Composite payload including `prompt_guard`, evaluator signals |
| `result` | JSON | Yes | Final consolidated result snapshot |
| `requires_human_review` | Boolean | No | Indicates whether human review was mandated |
| `created_at` | Timestamp | No | Ingestion timestamp |
| `updated_at` | Timestamp | No | Last modification timestamp |

---

### 2. `Incident` (`incidents`)
Logs security incidents, policy denials, and unmitigated rule breaches.

| Column | Type | Nullable | Description |
| --- | --- | --- | --- |
| `id` | Integer | No | Primary Key |
| `run_id` | String(64) | No | Foreign link to `agent_runs.run_id` (Index) |
| `severity` | String(16) | No | Severity level (`low`, `medium`, `high`, `critical`) |
| `code` | String(64) | No | Incident classification code |
| `detail` | Text | No | Detailed forensic evidence and rationale |
| `created_at` | Timestamp | No | Incident logging timestamp |

---

### 3. `ExecutionPlan` (`execution_plans`) & `ExecutionPlanStep` (`execution_plan_steps`)
Captures the control plane pre-execution plan and step-level audit trail.

- **`execution_plans`**: Captures `plan_id`, `run_id`, `workflow_id`, `trace_id`, `status`, and `policy_snapshot`.
- **`execution_plan_steps`**: Captures ordered steps, assigned tools, arguments, and execution statuses.

---

### 4. `RunCheckpoint` (`run_checkpoints`) & `ReplayAttempt` (`replay_attempts`)
Enables resumable runs and reproducible dry-run replay analysis.

- **`run_checkpoints`**: Records `checkpoint_id`, `run_id`, `checkpoint_type` (`submit`, `resume`, `step`), and step counters.
- **`replay_attempts`**: Records dry-run reconstruction logs, step results, and replay verification summaries.

---

## Multi-Tenant Governance Models

### 5. `Tenant` (`tenants`) & `TenantApiKey` (`tenant_api_keys`)
Provides multi-tenant governance with hashed credential storage.

- **`tenants`**: Records `tenant_id`, `name`, `status`, `request_limit_per_minute`, `high_risk_limit_per_hour`.
- **`tenant_api_keys`**: Records `key_id`, `tenant_id`, `key_prefix`, `key_hash` (SHA-256), `role` (`viewer`, `operator`, `admin`, `finops_admin`), and `revoked_at`.

---

## Agent FinOps Core Models

### 6. `PricingCatalog` (`pricing_catalog`)
Maintains the centralized, versioned cost matrix for AI models and infrastructure resources.

| Column | Type | Description |
| --- | --- | --- |
| `pricing_id` | String(64) | Unique price identifier |
| `provider` | String(64) | Model provider (e.g., `openai`, `anthropic`, `google`) |
| `model_name` | String(128)| Model name (e.g., `gpt-4o`, `claude-3-5-sonnet`) |
| `resource_type` | String(64) | `token`, `tool_call`, `worker_runtime` |
| `token_type` | String(64) | `input`, `output`, `cached_input`, `reasoning` |
| `rate` | Numeric | Cost per pricing unit |
| `price_unit` | String(64) | Unit (e.g., `per_1m_tokens`, `per_invocation`) |
| `effective_start_date`| Timestamp | Validity start timestamp |
| `is_active` | Boolean | Active status flag |

### 7. `UsageEvent` (`usage_events`) & `CostEvent` (`cost_events`)
The immutable financial ledger recording deterministic resource consumption and calculated expenditures.
- **`usage_events`**: Immutable logs of token counts, tool calls, and execution latencies.
- **`cost_events`**: Computed financial records bound to specific pricing catalog snapshots, guaranteeing historical cost integrity.

### 8. `BudgetPolicy` (`budget_policies`) & `BudgetViolation` (`budget_violations`)
Enforces soft and hard expenditure thresholds across `run`, `tenant`, `workflow`, and `user` scopes and `per_run`, `daily`, and `monthly` periods, with actions (`warn_only`, `require_approval`, `degrade_model`, `block`) and violation lifecycle states (`open`, `acknowledged`, `resolved`, `ignored`).

### 9. `PricingImportBatch` (`pricing_import_batches`), `PricingChangeLog` (`pricing_change_logs`) & `PricingSourceDocument` (`pricing_source_documents`)
Maintains full audit traceability for pricing catalog seed imports, manual rate adjustments, supersessions, and upstream official pricing source documents.
