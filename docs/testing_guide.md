# Testing Guide & Red-Team Corpus

> **Testing Baseline**: `v1.5.4`
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

# Run full interactive browser E2E test suite (requires running server on localhost:8000)
python scripts/browser_manual_test.py
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

---

## Adversarial Red-Team Regression Corpus

The test suite incorporates a deterministic red-team adversarial corpus covering critical agent failure modes:
1. **Direct Instruction Overrides**: Prompts attempting to force safety bypasses (`ignore all prior rules`).
2. **Classical Chinese Obfuscation**: Jailbreak instructions translated into literary or classical Chinese syntax.
3. **Data Exfiltration**: Attempts to dump environment files (`.env`), read private SSH keys, or query sensitive database tables.
4. **Specification Gaming / Shortcut Verification**: Tasks where the agent claims tests pass without executing the test harness.
5. **Tool Call Exhaustion**: Agents attempting infinite tool calling loops beyond configured budgets (`DEFAULT_MAX_TOOL_CALLS`).

---

## CI / CD Verification Gate

Every GitHub pull request and commit to `main` triggers automated verification via `.github/workflows/ci.yml`:
1. **Lint**: `ruff check app tests scripts`
2. **Security & Secret Scan**: `python scripts/security_check.py`
3. **Automated Tests**: `pytest -q`
