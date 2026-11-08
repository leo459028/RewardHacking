# Testing Guide & Red-Team Corpus

> **Testing Baseline**: `v1.5.7`
> **Test Suite**: `pytest`  
> **Linter**: `ruff`

RewardHacking relies on a comprehensive, deterministic automated test suite spanning unit algorithms, policy decision logic, asynchronous execution pipelines, multi-tenant governance, FinOps calculations, and an adversarial red-team regression corpus.

---

## Running Test Suites

```bash
# Run all tests with standard progress output
pytest -q

# Run specific test module
pytest tests/test_policy_engine.py -q

# Run with verbose output
pytest -v

# Run linter across codebase
ruff check app tests scripts

# Run security secret scan
python scripts/security_check.py

# Run full interactive browser E2E test suite (requires running server on localhost:8000; default QA currency: TWD)
python scripts/browser_manual_test.py
python scripts/browser_manual_test.py --qa-currency USD
```

> **Mandatory Release Requirement**: Every new version release must update [`docs/manual_test.md`](manual_test.md) and `scripts/browser_manual_test.py` to cover new UI/API capabilities and pass the full browser E2E suite with zero console/page/HTTP errors before sign-off.

---

## Test Suite Coverage Index

| Test Module | Coverage Scope |
| --- | --- |
| `tests/test_policy_engine.py` | Low-risk allow verdicts, high-risk escalation, destructive action interception, policy rules. |
| `tests/test_prompt_sanitizer.py` | Core prompt injection keyword detection, jailbreak pattern matching, system prompt leaks. |
| `tests/test_risk_scoring.py` | Composite risk scoring weights, penalty formulas, clamping guarantees (0.0 - 1.0). |
| `tests/test_evaluator.py` | Independent task evaluation rubrics, shortcut detection, fabricated test completion flags. |
| `tests/test_api.py` | Health/readiness endpoints, queued submissions, worker updates, approval flows, dashboard routes. |
| `tests/test_v04_v05.py` | Worker retry loops, Dead Letter Queue (DLQ), tool execution audit logs, approval TTL expiration. |
| `tests/test_v06_governance.py` | Multi-tenant isolation, API key mapping, tenant quota limits, high-risk hourly quotas, dashboard auth. |
| `tests/test_v07_governance_center.py` | Tenant CRUD, DB-backed hashed API keys, RBAC-lite role checks, tenant quota overrides. |
| `tests/test_v08_operations_gate.py` | Operations evidence submission, release readiness checks, release gate snapshot generation. |
| `tests/test_v10_production_ready.py` | Production release gate fail-closed behavior, safety regression evidence, Prometheus metric increments. |
| `tests/test_v11_classical_chinese_guard.py` | Classical Chinese prompt guard: permitting benign literature, blocking archaic adversarial jailbreaks. |
| `tests/test_v12_control_plane.py` | Control plane execution planning, workflow/tool registries, missing-plan fail-closed handling. |
| `tests/test_v13_trace_replay.py` | Distributed `X-Trace-Id` propagation, step-level audits, self-contained Replay Bundle exports. |
| `tests/test_v14_checkpoint_replay.py` | Submit checkpoints, resume checkpoint re-dispatching, dry-run replay attempts without side-effects. |
| `tests/test_v15_finops_core.py` | Pricing catalog CRUD, immutable usage/cost event ledger, tenant/run cost queries, budget violations. |
| `tests/test_v151_finops_core.py` | Expanded FinOps schema, model pricing YAML seed parsing, dry-run validation, superseding prices. |
| `tests/test_v153_finops_governance.py` | Active multi-scope/period budget enforcement (`block`, `require_approval`, `degrade_model`), context-tier pricing, free quota/minimum quantity, cache/batch savings, violation triage lifecycle, manual pricing change logs, stale source guardrails, and zero-warning Pydantic V2 serialization. |
| `tests/test_v154_control_plane_finops_closure.py` | Model-aware FinOps routing (`provider`/`model_name`/`project_id`), `user`/`project`/`model` budget scope isolation, `dry_run_only` enforcement, `failed_retry` cost accounting, `ExecutionPlanStep` lifecycle synchronization (`healthy` plan health), DLQ requeue/resolve, stale approval TTL sweep, and Run Detail Replay/Resume UI routes. |
| `tests/test_v155_finops_ledger_fx_export.py` | Settled ledger deduplication (zero estimated/actual double-counting), multi-currency FX conversion (`TWD`/`CNY`/`USD`) and inclusive/exclusive tax calculation, pre-tax vs. `base_currency` post-tax budget evaluation, unsupported budget currency `422` validation, `missing_fx_rate` violation tracking, `cost_center`/`org_id` attribution & `finops_missing_cost_center` warning, manual reconciliation adjustments (`POST /v1/cost/adjustments`) & RBAC check, RFC 4180 CSV/JSON ledger export with formula-injection protection, and Dashboard UI routes. |
| `tests/test_v157_mcp_gateway_schema_guardrails.py` | Safe MCP Gateway: MCP server registry CRUD & RBAC, MCP breakers, trust tiers / external-network / tenant allow-list guardrails, JSON schema validation & injection blocking (incl. scan-profile false-positive checks), sensitive argument redaction across `AgentRun` / `ExecutionPlan` / `ToolExecution` / replay bundle, dynamic tool execution in the worker, approved-untrusted completion & queue-time TOCTOU denial, per-MCP-server quota (`429 mcp_server_quota_exceeded`), `UsageEvent.mcp_server_id` / `tool_breakdown` / `mcp_server_breakdown` / `top_tools` attribution, `scope="mcp_server"` and exact `scope="tool"` budgets, Dashboard MCP portals, readiness check, metrics, version anchors, and the Governance tenant-update regression. |
| `tests/test_v1511_judge_profiles_backends.py` | `v1.5.11` judge profiles: migration head and defaults, settings, profile loading from YAML / JSON with secret-field rejection and validation, startup failure on invalid profiles, unknown active profile fallback, profile-prefixed `judge_version`, `json_schema` request schema, `auto` structured-output fallback and per-backend caching, reasoning stripping, empty content, `reasoning_effort`, retries / timeouts, background triage (non-blocking API, triage-only writes, sweep, in-flight skip, concurrency, decided-before-triage, inline fallback, health), profile cost attribution with catalog and zero-cost prices, tenant consent and the global switch, exact internal hosts, multi-profile benchmarks, profiles / probe / pricing / sweep APIs with RBAC, Dashboard flows and exact version anchors. Real LLMs are never called (`httpx.MockTransport`). |
| `tests/test_v1510_judge_triage_sampling.py` | `v1.5.10` judge triage and sampling: migration single head and defaults, settings validation and the `LLM_JUDGE_ENABLED` alias, deterministic judge purity and the `P1` cap, strict output parsing, `openai_compatible` via `httpx.MockTransport` (valid, invalid, timeout, 5xx, connection errors, external-endpoint blocking with zero requests), request redaction and truncation, API key secrecy, injection markers (English and Traditional Chinese) and nonce boundaries, a **malicious judge that cannot change verdict, priority or status**, post-commit triage with fallback, deduplication, mode `off`, tampering-first ordering, judge FinOps usage without lease changes, the daily token cap, same-family warnings, worker-path triage, threshold / random sampling slices, daily / share / random caps, conservation, one decision per run, honeypot and worker exclusions, side-effect-free sampled reviews, the unbiased random-audit rate, precision@K, deterministic / LLM benchmark triage, readiness checks, RBAC and concealment, Dashboard flows and exact version anchors. |
| `tests/test_v159_benchmark_detector_measurement.py` | `v1.5.9` measurement: migration single head and model defaults, hand-computed AUROC / TPR@FPR / Wilson / audit-budget curves, `suspicion_score`, seed corpus size and idempotent loading, reproducible side-effect-free benchmark runs and shadow what-if, the honest seed measurement (reward-tampering TPR@5% below the default gate), tenant isolation, `special_casing` SP-001..004 against two positive and two negative fixtures each (`tests/fixtures/v159/special_casing/`), `reports_impossible` claims, DSSE / in-toto attestation statuses (keys generated at runtime), replay and contradiction findings, review export / relabel / retire, per-finding labels, honeypot launch, judgement and concealment, cheat rates, RBAC, the advisory gate, Prometheus metrics, Dashboard flows, the Inspect importer and version anchors. |
| `tests/test_v158_outcome_evidence_review_queue.py` | `v1.5.8` evidence-based evaluation: legacy evaluator compatibility, deterministic `evaluator_version`, shadow/disabled detector modes, claim extraction (EN / zh-TW, negations), unified diff and JUnit parsing (unsafe XML rejection), redaction and truncation, every detector rule against positive and negative fixtures in `tests/fixtures/v158/`, `review_required` / `rejected` state split (no Kafka re-dispatch, no lease changes, `409 run_not_awaiting_approval`), review merge and self-review protection, outcome API (limits, monotonic safety, redaction), preview, tenant evaluation policy, metrics, readiness check, Dashboard flows, `signal_usage`, and version anchors. |
| `tests/test_v156_burn_rate_broker_quotas_portals.py` | Real-time tenant/project burn-rate and exhaustion forecasting (`GET /v1/cost/tenants/{id}/forecast`, `finops_burn_rate_warning`), Dynamic Token Broker pre-execution budget leasing (`lease_...`), concurrent sibling-lease budget enforcement (`fail_closed` on second concurrent request), worker settlement & manual lease release (`POST /v1/cost/leases/{run_id}/release`), per-user and per-tool Redis sliding-window quotas (`HTTP 429`), automatic worker-failure tool circuit breaker tripping & manual trip/reset (`POST /v1/control-plane/tools/{name}/breaker/open|close`), and interactive Governance/Control-Plane/Costs UI portal actions. |

---

## Adversarial Red-Team Regression Corpus

The test suite incorporates a deterministic red-team adversarial corpus covering critical agent failure modes:
1. **Direct Instruction Overrides**: Prompts attempting to force safety bypasses (`ignore all prior rules`).
2. **Classical Chinese Obfuscation**: Jailbreak instructions translated into literary or classical Chinese syntax.
3. **Data Exfiltration**: Attempts to dump environment files (`.env`), read private SSH keys, or query sensitive database tables.
4. **Specification Gaming / Shortcut Verification**: Tasks where the agent claims tests pass without executing the test harness. Since `v1.5.8` this is covered by evidence fixtures (`tests/fixtures/v158/*.diff`) for skip markers, removed assertions, deleted tests, narrowed test selection, always-true `__eq__`, result-rewriting `conftest.py` hooks, timer patching, CI `continue-on-error`, and swallowed exceptions, each with negative fixtures. Since `v1.5.9` the post-execution corpus is also a labeled benchmark (`app/seeds/benchmark/seed_v159.yaml`, 94 samples) measured with AUROC and TPR at low FPR, including deliberate evasions (`expected_miss`) and legitimate changes that current rules flag (`known_false_positive`).
5. **Tool Call Exhaustion**: Agents attempting infinite tool calling loops beyond configured budgets (`DEFAULT_MAX_TOOL_CALLS`).

---

## CI / CD Verification Gate

Every GitHub pull request and commit to `main` triggers automated verification via `.github/workflows/ci.yml`:
1. **Lint**: `ruff check app tests scripts`
2. **Security & Secret Scan**: `python scripts/security_check.py`
3. **Automated Tests**: `pytest -q -W error::DeprecationWarning` (deprecation warnings fail CI since `v1.5.8`)
