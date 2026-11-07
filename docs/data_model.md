# Database Schema & Audit Models

> **Data Model Version**: `v1.5.7`
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

## Evidence-Based Evaluation & Review Models (`v1.5.8`, Alembic `202611070001`)

### `EvaluationResultRecord` (`evaluation_results`) — new columns
`tenant_id` (default `default`), `evaluator_version`, `evaluation_source` (`executor` | `outcome`), `assurance_level` (`simulated` | `none` | `self_reported` | `ci_attested_unverified`), `findings` (JSON list), `review_priority` (`P0`–`P3` or empty), `outcome_id`. Server defaults keep pre-`v1.5.8` rows valid.

### `RunOutcome` (`run_outcomes`, `outcome_` IDs)
Append-only agent outcome evidence: `run_id`, `tenant_id`, `submission_seq`, `provenance`, `submitted_by`, `submitted_by_key_id`, redacted `evidence` (JSON), `evidence_digest` (SHA-256), `verdict`, `review_priority`, `assurance_level`, `evaluator_version`, `findings`.

### `EvaluationReview` (`evaluation_reviews`, `review_` IDs)
Post-execution review queue (at most one open review per run): `status` (`open` | `needs_info` | `closed`), `priority`, `categories`, `finding_ids`, `findings`, `sample_reason` (`rule_review`; `judge_sample` / `random_sample` reserved for `v1.5.10`), `evaluator_version`, `submitted_by` / `submitted_by_key_id`, `label` (`confirmed_hacking` | `false_positive` | `accepted` | `needs_info`), `label_categories`, `reviewer`, `reviewer_key_id`, `reason`, `opened_at`, `decided_at`.

### `TenantEvaluationPolicy` (`tenant_evaluation_policies`)
`tenant_id` (unique), `protected_paths` (JSON globs), `allow_default_protected_paths`, `updated_by`.

### Run statuses
`AgentRun.status` adds `review_required` (executed, awaiting post-execution review) and `rejected` (review confirmed reward hacking). Both are execution-finished states: token leases settle and execution plans complete.

**Finding IDs** use the `fnd_` prefix and are deterministic (hash of detector, rule, file, line, snippet).

---

## Multi-Tenant Governance Models

### 5. `Tenant` (`tenants`) & `TenantApiKey` (`tenant_api_keys`)
Provides multi-tenant governance with hashed credential storage.

- **`tenants`**: Records `tenant_id`, `name`, `status`, `request_limit_per_minute`, `high_risk_limit_per_hour`.
- **`tenant_api_keys`**: Records `key_id`, `tenant_id`, `key_prefix`, `key_hash` (SHA-256), `role` (`viewer`, `operator`, `admin`, `finops_admin`), and `revoked_at`.

---

## Safe MCP Gateway Storage (`v1.5.7`, zero migrations)

- **MCP Server Registry**: custom servers are stored in `tool_definitions` with the reserved `tool_name = "__mcp_server__:{mcp_server_id}"` (hence the 113-character id limit) and `metadata_payload = {"entity_type": "mcp_server", "server": {...}}`. `list_tool_definitions()` excludes the prefix, so `GET /v1/control-plane/tools` is unchanged; builtin servers live in code (`BUILTIN_MCP_SERVERS`).
- **Tool binding & schemas**: `ToolDefinition.metadata_payload` keys `mcp_server_id`, `input_schema` (with optional per-field `x-scan-profile`), `output_schema`, `sensitive_fields`, `schema_enforcement` (`deny | escalate`), `strict_arguments_required`.
- **Request**: `AgentRun.request_payload.tool_arguments` (redacted) and `metadata.mcp_gateway` (server map, schema status, redacted field paths, flags) — any client-supplied `metadata.mcp_gateway` is overwritten.
- **Plan**: `ExecutionPlan.selected_tools[*]` adds `mcp_server_id`, `mcp_trust_tier`, `mcp_transport`, `schema_validation`, `sanitized_arguments`, `redacted_fields`; `model_route.mcp_route = {primary_mcp_server_id, mcp_servers, tool_mcp_map}`; approval gates `mcp_trust_gate`, `mcp_network_gate`, `mcp_breaker_gate`.
- **Execution**: `ToolExecution.sandbox_policy` adds `mcp_server_id`, `sanitized_arguments`, `redacted_fields`, `schema_validation` (output schema).
- **FinOps**: `UsageEvent.mcp_server_id`, `tool_id`, `tool_name`, `tool_provider`, `tool_risk_level` are now populated on `step_2_tools`; `CostEvent.allocation_tags.mcp_server_id|tool_id` and `metadata_payload.mcp_server_id|mcp_servers|tool_id|tools|tool_mcp_map`.
- **Audit**: `GovernanceEvent` types `mcp_server.upserted`, `mcp_breaker.opened|closed`, `mcp_schema_violation`, `mcp_argument_injection`, `mcp_server_quota_exceeded`; `Incident(code="mcp_argument_injection", severity="high")`.

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
- **`cost_events`**: Computed financial records bound to specific pricing catalog snapshots, guaranteeing historical cost integrity. Supports `cost_type` values (`estimated`, `actual`, `failed_retry`, `adjustment`), multi-currency FX and tax fields (`currency`, `fx_rate`, `fx_rate_source`, `tax_rate`, `tax_inclusive`, `tax_amount`, `base_currency_cost`), cost allocation attribution (`project_id`, `cost_center`, `org_id`, `business_unit`, `environment`, `allocation_tags`), and invoice reconciliation (`invoice_id`, `reconciled`).

### 8. `BudgetPolicy` (`budget_policies`) & `BudgetViolation` (`budget_violations`)
Enforces soft and hard expenditure thresholds across `run`, `tenant`, `workflow`, `user`, `project`, and `model` scopes and `per_run`, `daily`, `weekly`, `monthly`, `quarterly`, and `yearly` periods using settled pre-tax base-currency amounts (or post-tax when `budget_basis="base_currency"`), with actions (`warn`, `require_approval`, `degrade_model`, `dry_run_only`, `fail_closed`), violation types (`soft_limit_exceeded`, `hard_limit_exceeded`, `missing_fx_rate`), and violation lifecycle states (`open`, `acknowledged`, `resolved`, `ignored`).

### 9. `PricingImportBatch` (`pricing_import_batches`), `PricingChangeLog` (`pricing_change_logs`) & `PricingSourceDocument` (`pricing_source_documents`)
Maintains full audit traceability for pricing catalog seed imports, manual rate adjustments, supersessions, and upstream official pricing source documents.
