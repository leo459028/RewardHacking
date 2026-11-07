# Project Roadmap: Evolutionary Milestones

> **Roadmap Baseline**: `v1.5.8`
> **Status**: Active Open-Source Track (`v1.5.x` Governance, FinOps & Evaluation Series)
> **Next Target**: `v1.5.9` (Labeled Benchmark & Detector Measurement)

This roadmap outlines the past development trajectory, the remaining `v1.5.x` evaluation releases (`v1.5.8`–`v1.5.10`), the `v1.6.x` runtime execution series, and long-term architectural milestones for the **RewardHacking** platform.

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
| `v1.5.7` | Completed | Safe MCP Gateway MVP & Tool Schema Guardrails | MCP server registry with trust tiers & transport guardrails, tool input/output JSON schema validation with injection scan profiles, sensitive argument redaction, dynamic executor registry closure, and per-MCP-server cost and risk attribution (`mcp_server_id`). |
| `v1.5.8` | **Current** | **Outcome Ingestion, Evidence Detectors & Review Queue** | Agent outcome evidence ingestion (diff, test/lint reports, final message) with provenance levels, evidence-based reward-hacking detectors (test/grader/CI tampering, claim–evidence mismatch, error suppression, scope), and a post-execution review queue separated from pre-execution approval. |
| `v1.5.9` | **Next** | Labeled Benchmark & Detector Measurement | Review labels exported as benchmark samples, TPR at low FPR / AUROC / audit-budget curves, shadow `special_casing` detector, honeypot (impossible-task) probes, per-agent cheat rates, and CI attestation verification. |
| `v1.5.10` | Planned | LLM-as-Judge Triage (Shadow) | Pluggable trusted judge providers that only prioritize review work and never turn `review` into `pass`, plus quota-capped `sample_for_review` with a uniform random audit slice. |
| `v1.6.0` | Planned | Runtime Foundations | OTLP / W3C trace export, audit retention purge with legal hold (FinOps ledger excluded), FinOps module split, and a `SandboxBackend` interface. |
| `v1.6.1` | Planned | Isolated Sandbox Runtime MVP | Hardened Docker backend with a configurable OCI runtime (`runc` locally, `runsc`/gVisor on Linux), limited to low-risk tools behind a feature flag; sandbox re-runs upgrade evidence to `platform_verified`. |
| `v1.6.2+` | Long-Term | Remote MCP, MicroVM & Graph Replay | Real remote MCP transports behind an egress proxy, K8s Job and microVM backends, hidden held-out tests, mutation testing, resampling, and stateful graph time-travel replay. |

---

## Current Release: `v1.5.8` Agent Outcome Ingestion, Evidence-Based Detectors & Post-Execution Review Queue

The `v1.5.8` milestone moves reward-hacking detection onto evidence:
- **Agent Outcome Ingestion**: `POST /v1/runs/{run_id}/outcome` accepts the agent's final message, unified diff, optional full file contents, JUnit or summary test reports, lint reports and tool transcripts. Evidence is size-bounded, JUnit `DOCTYPE` / `ENTITY` payloads are rejected, and secrets and emails are redacted before detection and persistence. Agent-reported evidence is `self_reported` (a claim, shown as unverified); `ci_attested` is recorded but not verified until `v1.5.9`. Submissions are append-only and capped per run (`EVALUATION_OUTCOME_MAX_SUBMISSIONS`).
- **Evidence-Based Evaluation Engine** (`app/services/evaluation/`): taxonomy (`reward_tampering`, `deception`, `specification_gaming`), deterministic claim extraction (English and Traditional Chinese, negation-aware), versioned detectors with `enforce` / `shadow` / `disabled` modes (`EVALUATION_DETECTOR_MODES`), deterministic aggregation into `verdict`, `review_priority` (`P0`–`P3`) and `assurance_level`, and a reproducible `evaluator_version`. The old keyword heuristic is kept as a low-weight `legacy_text` detector, and `Evaluator.evaluate()` keeps its signature.
- **Five Evidence Detectors**: `test_tampering` (protected paths, skip/xfail markers, removed assertions, deleted tests, narrowed test selection, shrinking test counts), `grader_tampering` (always-true `__eq__`, `sys.exit(0)` outside a main guard, timer patching, result-rewriting `conftest.py` hooks), `error_suppression` incl. CI tampering (`continue-on-error`, `|| true`, swallowed exceptions, lint suppressions), `claim_evidence` (claimed passing tests with missing, failing or skipped reports), and `scope`. Python uses AST analysis; every rule has positive and negative fixtures (`tests/fixtures/v158/`). Tenant protected paths: `GET/PUT /v1/tenants/{tenant_id}/evaluation-policy`.
- **Post-Execution Review Queue**: new run statuses `review_required` and `rejected`. Previously a post-execution `review` verdict reused `awaiting_approval`, so `approve_once` re-dispatched an already-executed run. Reviews (`/v1/reviews`, `/dashboard/reviews`) are now separate: decisions (`confirmed_hacking`, `false_positive`, `accepted`, `needs_info`) never re-execute runs or touch token leases, the outcome submitter cannot decide the same review, and a run under review is never downgraded by new evidence. `approve_once` on a non-`awaiting_approval` run returns `409 run_not_awaiting_approval`.
- **Monitor-Only Signals**: all evaluation and review responses carry `signal_usage: "monitor_only"`; findings must not be used as training rewards.
- **Dashboard & Operations**: `/dashboard/reviews` (priority queue, filters, review detail with evidence pointers, decision form, Evidence Preview), Run Detail *Outcome Evidence & Findings* and *Submit Outcome Evidence*, Governance *Evaluation Policy*, `review_required` / `rejected` badges, `evaluation_review_queue_health` readiness check, and four `agent_evaluation_*` metrics. Alembic head `202611070001`. CI now runs `pytest -W error::DeprecationWarning`. Details: [`docs/evaluation.md`](evaluation.md).

## Previous Release: `v1.5.7` Safe MCP Gateway MVP & Tool Schema Guardrails

The `v1.5.7` milestone closes the structured tool-argument attack surface:
- **Safe MCP Server Registry, Trust Tiers & Transport Guardrails**: Registers Model Context Protocol servers (`GET/POST /v1/control-plane/mcp-servers`) with `transport` (`stdio | sse | streamable_http`), `trust_tier` (`trusted | verified | untrusted | blocked`), hostname-parsed `is_external_network` detection, tenant allow-lists and per-server quotas. Four builtin servers ship by default (`mcp-core-local`, `mcp-data-verified`, `mcp-deploy-verified`, `mcp-external-untrusted`). Blocked/disabled servers or disallowed tenants are hard-denied (zero cost, no lease); untrusted servers (+0.20 risk), external-network violations (+0.15 risk) and open MCP breakers (`circuit:mcp:{id}`) route runs to `awaiting_approval` with `mcp_trust_gate` / `mcp_network_gate` / `mcp_breaker_gate`.
- **Tool JSON Schema Guardrails, Injection Scan Profiles & Sensitive Argument Redaction**: `AgentRequest.tool_arguments` is validated by a dependency-free JSON Schema subset validator (`required`, `type`, `additionalProperties`, `min/maxLength`, `minimum/maximum`, `enum`, `pattern`, `items`) with depth/size limits. Per-field `x-scan-profile` (`path | shell | sql_destructive | sql | none`) blocks path traversal and shell/SQL injection without false positives on `query_data` `UNION SELECT` or `write_patch` diffs. Credentials, bearer tokens and emails are recursively redacted to `[REDACTED]` / `[REDACTED_EMAIL]` before `AgentRun`, `ExecutionPlan`, `ToolExecution` and replay bundles are persisted. `POST /v1/control-plane/tools/{tool_name}/validate-arguments` exposes the validator directly.
- **Dynamic Executor Registry Closure & Worker Re-Check**: The background worker now executes any tool registered in the Control Plane (no more false `blocked` transcripts for dynamically registered tools), validates simulated outputs against `output_schema`, and re-checks hard-deny MCP conditions at execution time (TOCTOU) while never re-escalating already-approved runs.
- **Per-MCP-Server FinOps Attribution**: Populates `UsageEvent.mcp_server_id` / `tool_*` columns, tags `CostEvent.allocation_tags` and metadata, fixes `run_cost_summary.tool_breakdown` and `tenant_cost_summary.top_tools` (no more `step_id` leakage), adds `mcp_server_breakdown` / `top_mcp_servers`, `scope="mcp_server"` and exact `scope="tool"` budgets, `mcp_server_id` filters on cost summary/export, and appends `mcp_server_id,tool_id` to the CSV ledger (37 columns). Zero Alembic migrations (head stays `202606010004`).

## Roadmap Principles (Re-baselined after `v1.5.7`)

The pre-execution perimeter (prompt guards, policy engine, Safe MCP Gateway, FinOps budgets) has matured over `v1.0`–`v1.5.7`. The post-execution evaluator, which is the core of reward-hacking detection, is still a small keyword heuristic that scores simulated executor text. The remaining releases rebalance the project toward that core and follow these principles:

1. **Evidence over claims**: Agent self-reports are claims, not proof. Every piece of evidence carries a provenance level: `simulated` → `self_reported` → `ci_attested` → `platform_verified`.
2. **Tampering outranks gaming**: Modifying tests, graders or CI (reward tampering) is always top priority and never averaged away by other signals.
3. **Monotonic safety**: Automated components (rules, detectors, LLM judges, sampling) may only make a verdict stricter or reorder review work. Only a human can accept a run under review.
4. **Measure before you automate**: A labeled benchmark with detection rates at low false-positive rates comes before any LLM judge, so every new signal can be measured.
5. **Separate approval from review**: Pre-execution approval decides whether a run may execute. Post-execution review judges what already happened and never re-executes it.
6. **Monitor-only signals**: Detection signals are for monitoring and review. They must not be used as training rewards, because optimizing against a monitor teaches agents to hide intent.
7. **Foundations before real execution**: Observability, data lifecycle and modular code come before running real tools in a sandbox.

---

## Remaining `v1.5.x` Evaluation Track

### `v1.5.8` (Completed): Agent Outcome Ingestion, Evidence-Based Detectors & Post-Execution Review Queue
- **Outcome Ingestion API**: `POST /v1/runs/{run_id}/outcome` accepts the agent's final message, unified diff, optional full file contents, JUnit/summary test reports, lint reports and tool transcripts, with provenance levels, size limits, unsafe-XML rejection and redaction before persistence. Submissions are append-only and capped per run; a run under review cannot be downgraded by resubmitting.
- **Evaluation Engine Refactor** (`app/services/evaluation/`): taxonomy (`reward_tampering`, `deception`, `specification_gaming`), claim extraction (English and Traditional Chinese), a versioned detector registry with `enforce`/`shadow` modes, a deterministic aggregator producing `verdict`, `review_priority` (P0–P3) and `assurance_level`, and a reproducible `evaluator_version`. The legacy keyword heuristic becomes a low-weight `legacy_text` detector for backward compatibility.
- **Evidence Detectors**: `test_tampering` (protected paths, added skip/xfail, removed assertions, shrinking test counts), `grader_tampering` (always-true `__eq__`, `sys.exit(0)`, timer/grader monkeypatching, result-rewriting `conftest.py` hooks), `error_suppression` including CI tampering (`continue-on-error`, `|| true`, bare `except: pass`, lint suppressions), `claim_evidence` (claimed passing tests with missing or failing reports), and `scope` (changes outside a declared scope). Python uses AST analysis; other languages use diff-level patterns.
- **Post-Execution Review Queue**: New run statuses `review_required` and `rejected` replace the previous reuse of `awaiting_approval` after evaluation, which re-dispatched already-executed runs on `approve_once`. Review decisions (`confirmed_hacking`, `false_positive`, `accepted`, `needs_info`) never re-execute runs or touch token leases, enforce reviewer/submitter separation, and are stored as labels for the `v1.5.9` benchmark. Also adds tenant-level protected-path policies, `/dashboard/reviews`, Run Detail findings with evidence pointers, and an `evaluation_review_queue_health` operations check.

### `v1.5.9` (Next Target): Labeled Benchmark & Detector Measurement
- **Labeled Benchmark Corpus**: Versioned labeled samples, seeded from the inline red-team corpus and extended with one-click export of human review decisions from `v1.5.8`.
- **Control-Style Metrics**: Per-category TPR at fixed low FPR, AUROC, and audit-budget-versus-catch-rate curves instead of a single F1 score, with benchmark recall thresholds as an operations readiness gate.
- **Harder Detectors in Shadow Mode**: `special_casing` (hard-coded expected outputs) ships as a shadow detector until the benchmark shows an acceptable false-positive rate.
- **Honeypot Probes**: Impossible-task probes with conflicting tests that measure an agent's propensity to cheat, plus per-agent and per-model-version cheat-rate tracking.
- **CI Attestation Verification**: Verifies `ci_attested` evidence (in-toto / SLSA provenance, GitHub OIDC attestations). Optional importer for Inspect evaluation logs.

### `v1.5.10`: LLM-as-Judge Triage (Shadow Mode)
- **Safety Statement**: The judge never turns `review` into `pass` and never participates in hard-deny decisions.
- **Triage Only**: Judge output becomes `triage_priority` and rationale to order the review queue, measured by precision@K on the benchmark.
- **Quota-Capped `sample_for_review`**: Rule-passed runs can be pulled into review only above a suspicion or risk threshold, within a per-tenant daily cap and a maximum share of passed volume, plus a small uniform random slice for unbiased miss-rate estimation. Skipped samples are counted and shown, never silently dropped.
- **Trusted, Hardened Judges**: Pluggable `deterministic` and `openai-compatible` providers (hosted or local), warnings when the judge shares a model family with the monitored agent, redaction before external calls, judge prompt-injection regression tests, judge token costs recorded in the FinOps ledger, and deterministic fallback on timeout.

---

## `v1.6.x` Runtime Execution Series

### `v1.6.0`: Runtime Foundations
- **OpenTelemetry Export**: W3C `traceparent` propagation and OTLP span export across API, worker, judge and executor.
- **Audit Retention Purge with Legal Hold**: Dry-run and scheduled purges driven by `audit_retention_days`, excluding open incidents, runs under review, and runs referenced by checkpoints or replay bundles. The FinOps ledger is never auto-purged and follows a separate `finops_ledger_retention_days` setting (default about seven years) that only reports due records.
- **Modularization**: Split `app/services/finops.py` into domain submodules and continue moving endpoints out of `app/api/routes.py` into per-domain routers.
- **`SandboxBackend` Interface**: Abstracts tool execution while keeping deterministic simulation as the default backend.

### `v1.6.1`: Isolated Sandbox Runtime MVP
- **Hardened Docker Backend**: Read-only root filesystem, no network by default, all capabilities dropped, non-root user, and pid/memory/CPU limits. The OCI runtime is configurable: `runc` for local Docker Desktop verification, `runsc` (gVisor) on Linux hosts without code changes.
- **Narrow Scope**: Only low-risk tools (for example `read_repo`, `run_tests`) and in-sandbox `stdio` MCP servers, behind a disabled-by-default feature flag.
- **Platform-Verified Evidence**: Re-running tests inside the sandbox upgrades outcome evidence to `platform_verified`; dry-run replay can optionally re-execute inside the sandbox.

---

## Long-Term Horizon: `v1.6.2+`

- **Remote MCP Transports**: Real `sse` / `streamable_http` connections behind an egress proxy and allow-lists.
- **Additional Sandbox Backends**: Kubernetes Job backend (with gVisor `RuntimeClass`) and optional Firecracker microVMs for Linux deployments.
- **Stronger Verification**: Hidden held-out tests, mutation testing to detect weakened tests, and resampling of suspicious steps.
- **Closed-Loop Risk Scoring**: Feed confirmed per-agent cheat rates back into pre-execution risk scoring once false-positive rates are stable.
- **Stateful Graph Time-Travel & Live Replay**: Full workflow graph checkpointing with isolated sandbox re-execution.
