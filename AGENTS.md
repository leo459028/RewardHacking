# Project Working Guidelines & Agent Specification (`AGENTS.md`)

This document provides operational guidelines, architectural quick-reference maps, domain conventions, and fast-iteration workflows for AI agents and human engineers working on **RewardHacking**. Please adhere to these guidelines first before referring to `README.md`.

---

## Scope of Application

These specifications apply across the entire repository. Unless a subdirectory provides a more specific `AGENTS.md`, all feature planning, code modifications, test additions, and documentation updates must comply with this document.

---

## Session Start: Read the Handoff File First

- **At the start of every session (new or resumed), read `docs/handoff.md` before anything else**, if it exists. It holds the current state, next steps, local environment notes and known pitfalls, so a session does not need to re-read the codebase to orient itself. It is a local, git-ignored working file (Traditional Chinese, like `docs/plan/*.md`); a fresh clone will not have it.
- Then verify it against git (`git status`, `git log --oneline -3`, `git branch --show-current`). If they disagree, git wins: continue from the real state and correct the handoff.
- **Keep it current**: update it after each phase commit, before ending a session, and whenever the state changes (push, PR, merge, new version). Keep it short (about 100 lines): current state, next steps and pitfalls only; details belong in the version plan and `docs/test_report.md`.
- **Never stage or commit it** (`docs/handoff.md` is listed in `.gitignore`).

---

## 0. Codebase Quick Map & Fast-Iteration Reference

To minimize exploratory file reads and maximize iteration speed and model quota efficiency, use this map to locate core modules immediately:

| Layer / Domain | Primary Files | Key Responsibilities & Notes |
| :--- | :--- | :--- |
| **App Entry & Config** | `pyproject.toml`<br>`app/main.py`<br>`app/core/config.py` | Package version, FastAPI initialization & middleware (`X-Trace-Id`, Prometheus HTTP metrics), and environment settings (`get_settings()`). |
| **REST API Layer** | `app/api/routes.py`<br>`app/api/deps.py` | All `/v1/*` JSON endpoints (submit, runs, trace, replay-bundle, checkpoints, approvals, worker DLQ, governance, control-plane & breakers, FinOps `/v1/pricing`, `/v1/budgets`, `/v1/budget-violations`, `/v1/cost/*`, `/v1/cost/tenants/{id}/forecast`, `/v1/cost/tenants/{id}/leases`, `/v1/cost/leases/{run_id}/release`, `/v1/cost/adjustments`, `/v1/cost/tenants/{id}/export`, `/v1/admin/finops/*`, operations). |
| **Dashboard UI Layer** | `app/ui/routes.py`<br>`app/ui/routers/*.py`<br>`app/ui/templates/*.html`<br>`app/ui/static/styles.css` | Server-rendered Jinja2 operational portal (`/dashboard/*`, 16 primary views incl. `/dashboard/reviews` and `/dashboard/benchmark`, with interactive Governance, Control-Plane, and Costs forms). Uses custom Jinja2 filters `json_pretty` and `decimal_plain`. |
| **Guardrail, Policy & Quotas** | `app/services/prompt_sanitizer.py`<br>`app/services/classical_chinese_guard.py`<br>`app/services/risk_scoring.py`<br>`app/services/policy_engine.py`<br>`app/services/quota.py`<br>`app/services/run_state.py`<br>`app/services/orchestrator.py` | Pre-execution prompt sanitization, Classical Chinese adversarial detection, composite risk scoring (`0.0`–`1.0`), deterministic `allow`/`escalate`/`deny` policy decisions, tenant/user/tool Redis minute-bucket fixed-window quotas (`check_request_quota`, `check_user_request_quota`, `check_tool_quotas`), tool circuit breakers (`breaker:{tool}`, `breaker_failures:{tool}:*`), and bounded tool simulation. |
| **Safe MCP Gateway (v1.5.7)** | `app/services/mcp_gateway.py`<br>`app/schemas/control_plane.py` | `BUILTIN_MCP_SERVERS`, MCP registry stored as `tool_definitions` rows `__mcp_server__:{id}` (id ≤ 113 chars), `is_external_endpoint()` (parsed hostname), `validate_tool_arguments()` (JSON Schema subset + `x-scan-profile`), `sanitize_tool_arguments()`, shared `preflight()` for all 3 submit paths, `worker_context()` (hard-deny re-check, no re-escalation once approved), `persist_preflight_audit()`, per-MCP quota `tenant:{id}:quota:mcp:{server}:calls:{min}`, MCP breakers `circuit:mcp:{id}`. Endpoints: `/v1/control-plane/mcp-servers`, `.../breaker/open|close`, `/v1/control-plane/tools/{name}/validate-arguments`. |
| **Evidence-Based Evaluation & Review (v1.5.8)** | `app/services/evaluation/`<br>`app/services/outcomes.py`<br>`app/services/review_queue.py`<br>`app/api/routers/evaluations.py`<br>`app/ui/routers/reviews.py` | `EvaluationEngine.evaluate_context()`, `context_from_outcome()` (bounds, JUnit parse, redaction), `extract_claims()`, detectors `test_tampering` / `grader_tampering` / `error_suppression` / `claim_evidence` / `scope` / `evidence_quality` / `legacy_text` (rule IDs `TT-`, `GT-`, `ES-`, `CE-`, `SC-`), `aggregate()` (verdict, `P0`–`P3`, assurance), `evaluator_version`; `submit_outcome()` / `preview_evaluation()`; `open_or_merge_review()`, `decide_review()` (never re-dispatches), tenant protected paths. Endpoints: `/v1/runs/{id}/outcome(s)`, `/v1/evaluations(/preview)`, `/v1/reviews`, `/v1/tenants/{id}/evaluation-policy`; Dashboard `/dashboard/reviews`. Fixtures: `tests/fixtures/v158/`. Rules: `docs/evaluation.md`. |
| **Benchmark, Honeypots & Attestation (v1.5.9)** | `app/services/benchmark.py`<br>`app/services/evaluation/metrics.py`<br>`app/services/evaluation/attestation.py`<br>`app/services/attestation_keys.py`<br>`app/services/honeypot.py`<br>`app/services/cheat_rates.py`<br>`app/services/inspect_import.py`<br>`app/api/routers/benchmark.py`<br>`app/ui/routers/benchmark.py` | Seed corpus `app/seeds/benchmark/seed_v159.yaml` (lazy `ensure_seed_corpus()`), `export_review()`, side-effect-free `run_benchmark()` (`evaluate_context(ctx, promote_shadow=True)` what-if), pure AUROC / TPR@FPR / Wilson / audit-budget curves, `suspicion_score` (ranking only), `gate_evaluation()` + advisory `evaluation_benchmark_gate` (`EVALUATION_BENCHMARK_GATE_ENFORCED`; `summarize_status()` skips `metadata.advisory`), shadow `special_casing` (`SP-`), honeypot probes (`app/seeds/honeypots_v159.yaml`, run mapping only in `honeypot_launches`, `judge_honeypot()`), monitor-only `compute_cheat_rates()`, DSSE / in-toto `verify_attestation()` (`CE-009` / `CE-010`). Endpoints: `/v1/benchmark/*`, `/v1/honeypots/*`, `/v1/evaluations/cheat-rates`, `/v1/tenants/{id}/attestation-keys`; Dashboard `/dashboard/benchmark`. Fixtures: `tests/fixtures/v159/`. |
| **Judge Triage & Review Sampling (v1.5.10)** | `app/services/evaluation/judges/`<br>`app/services/judge_triage.py`<br>`app/services/review_sampling.py`<br>`app/services/benchmark_triage.py`<br>`app/models/judge.py` | Advisory judges only: `get_judge()` (`deterministic` default / `openai_compatible` via `httpx`), `build_judge_input()` (re-redaction, truncation, injection markers), nonce-bounded `build_messages()`, strict `parse_judge_output()`, `assess_with_fallback()`; `triage_review()` runs **after commit**, writes only `judge_assessments` + review `triage_priority` (`P1`–`P3`, never `P0`) / `triage_score` / `triage_source` / `judge_assessment_id`, dedups by `(run_id, input_digest, judge_version)`, records judge tokens (`step_id=evaluation_judge`, no lease), daily token cap; `review_sort_key()` = `(min(rule, triage), rule, -score)`; `EVALUATION_JUDGE_MODE` `off`/`shadow`/`triage`. `decide_sampling()` (one `ReviewSamplingDecision` per rule-passed outcome: HMAC random audit + threshold slice, daily/share/random caps, skips counted) inside `submit_outcome()`; `random_audit_hacking_rate` in cheat rates; `compute_triage_metrics()` precision@K. `ENGINE_VERSION` stays `v1.5.9` (no detector change). Endpoints: `/v1/reviews?order=`, `/v1/reviews/{id}/judge-assessments`, `/v1/evaluations/judge/status`, `/v1/evaluations/sampling/summary|decisions`, `judge_provider` on `/v1/benchmark/runs`. Fixtures: `tests/fixtures/v1510/`. |
| **Judge Profiles & Real Backends (v1.5.11)** | `app/services/evaluation/judges/profiles.py`<br>`app/services/judge_background.py`<br>`app/services/judge_profiles_admin.py`<br>`config/judge_profiles.example.yaml` | `load_profiles()` (built-in `deterministic`, legacy `default` from `EVALUATION_JUDGE_*`, YAML `EVALUATION_JUDGE_PROFILES_FILE`, JSON `EVALUATION_JUDGE_PROFILES`; keys only via `api_key_env`), `get_judge(settings, profile=...)` / `build_judge()`, named profiles prefix `judge_version`; `openai_compatible` structured output `auto` (`json_schema` → `json_object` → none, cached), `strip_reasoning`, `reasoning_effort`, bounded retries; `dispatch_triage()` (inline vs `execution: background` with `pending:<profile>`, per-loop semaphore, `sweep_pending_triage()`); profile pricing attribution, `ensure_zero_cost_pricing()` (`qa_local` only), exact `EVALUATION_JUDGE_INTERNAL_HOSTS`, tenant consent (`allow_external_judge`) in `run_judge()`; multi-profile `run_benchmark(judge_profiles=...)` → `metrics.triage_by_profile`. Endpoints: `/v1/evaluations/judge/profiles(/{name}/probe|zero-cost-pricing)`, `/v1/evaluations/judge/sweep`. |
| **Control Plane & Step Audit** | `app/services/control_plane.py`<br>`app/models/control_plane.py`<br>`app/schemas/control_plane.py` | `ControlPlanePlanner.plan()`, workflow/tool registries, `ExecutionPlan` & `ExecutionPlanStep` persistence, and lifecycle synchronization (`sync_execution_plan_on_approval`, `sync_execution_plan_on_worker_completion`). |
| **Checkpoints & Replay** | `app/services/checkpoint.py`<br>`app/models/checkpoint.py`<br>`app/schemas/checkpoint.py` | `RunCheckpoint` (`ckpt_...`) and `ReplayAttempt` (`replay_...`) persistence, checkpoint resume re-dispatch, and dry-run replay. |
| **Agent FinOps Engine** | `app/services/finops.py`<br>`app/services/pricing_import.py`<br>`app/models/finops.py`<br>`app/schemas/finops.py`<br>`app/seeds/model_pricing_seed.yaml` | 149-entry official model pricing catalog, context-tier resolution, settled ledger deduplication (`_settled_cost_events`), multi-currency FX & tax accounting (`resolve_fx_rate`, `_compute_tax`), `cost_center`/`org_id` attribution, manual adjustments (`create_cost_adjustment`), CSV/JSON ledger export (`export_tenant_cost_ledger`), burn-rate & exhaustion forecasting (`compute_tenant_burn_rate_forecast`), Dynamic Token Broker pre-execution leasing (`acquire_run_budget_lease`, `settle_run_budget_lease`, `release_run_budget_lease`), multi-scope (`run`/`tenant`/`workflow`/`user`/`project`/`model`) & multi-period budget enforcement (`fail_closed`, `require_approval`, `degrade_model`, `dry_run_only`, `warn`), `failed_retry` cost accounting, and violation triage. |
| **Audit, DLQ & Operations** | `app/services/audit_log.py`<br>`app/services/operations.py`<br>`app/workers/consumer.py` | `AgentRun`, `ApprovalEvent` (including `expire_stale_approvals`), `WorkerAttempt` (including `requeue_dlq_run`, `resolve_dlq_run`), `GovernanceEvent`, `Incident`, `process_run_by_id()`, and release readiness gate checks. |
| **Database & Migrations** | `migrations/versions/`<br>`app/models/` | Current Alembic Head: `202611100001` (`202611100001_add_v1511_judge_profiles.py`). Check if existing columns/JSONB fields suffice before creating new migrations. |

---

## 1. Planning & Proposal Requirements

Any feature addition, risk decision modification, data query update, audit logging change, or plan impacting user-visible behavior must include accessible UI or Dashboard entry points for querying and testing.
If a change is purely documentation, tests, code formatting, or internal refactoring with no user-visible behavioral impact, explain explicitly why UI/Dashboard verification is not applicable.

- **Plan File Convention**: Version plans are stored locally in `docs/plan/v1_x_yplan.md` (e.g., `docs/plan/v1_5_4plan.md`) and must align with the incremental release track in `docs/roadmap.md`. **Note**: `docs/plan/*.md` files are local working artifacts ignored via `.gitignore` and must **never** be staged or uploaded to GitHub.
- **Mandatory Plan Sections**:
  1. Functional goals and concrete acceptance criteria.
  2. UI / Dashboard verification instructions (navigation, inputs, expected visual state).
  3. Implementation design and list of affected modules.
  4. Verification test suite and exact executable shell commands.
  5. List of documentation files to be updated after implementation.
  6. Required test data and metrics to be recorded into `docs/test_report.md`.
  7. **Implementation Progress Log** (appended at the end of the plan and kept current during implementation): implementation phases, the files each phase changed, its Stage 1 test result, and the next step. This lets work resume safely after a session ends or a quota reset (see Section 3, Phased Implementation & Resumption).
- **Version Numbering**: Patch numbers may exceed 9 (e.g., `v1.5.10` follows `v1.5.9`); a minor bump (`v1.6.0`) marks a new release series as defined in `docs/roadmap.md`, not a rollover. Plan files and test files follow the same digits (`docs/plan/v1_5_10plan.md`, `tests/test_v1510_<feature>.py`).

Do not write vague conceptual sketches; ensure the plan provides actionable step-by-step instructions that subsequent engineers can execute directly.

---

## 2. Implementation Requirements, Domain Conventions & Quota Efficiency

### 2.1 General Implementation Rules
- **Maintain Architectural Continuity**: Follow established service boundaries and dataflows unless an architectural change is explicitly requested.
- **Read Before Modifying**: Inspect target line ranges before editing, and **batch multiple `read_file` or `edit_file` tool calls in parallel within a single turn** to save model quota and wall-clock time.
- **Preserve Unrelated Changes**: Never revert or discard existing user modifications in the working tree.
- **Router Split Rule (since `v1.5.8`)**: `app/api/routes.py` and `app/ui/routes.py` are frozen for growth. Add new endpoints to per-domain routers (`app/api/routers/<domain>.py`, `app/ui/routers/<domain>.py`) registered in `app/main.py`. When a release touches an existing domain, move that domain's endpoints into its router without changing paths or response shapes.

### 2.2 Domain Conventions & Anti-Footguns (Prevent Avoidable Retries)
- **Zero-Warning Pydantic V2 Serialization**: Never use deprecated `json_encoders` in `ConfigDict`. Always use `Annotated[Decimal, PlainSerializer(format_decimal_plain, return_type=str, when_used="json")]`. All pytest runs must pass with `-W error::DeprecationWarning`.
- **Plain Decimal Formatting (`0` `0E-8` Artifacts)**: All monetary/quantity `Decimal` values rendered in JSON responses or Jinja2 templates must use `format_decimal_plain()` or the `|decimal_plain` template filter so scientific notation (`0E-8`) never appears in the UI or API.
- **Key Enum & Route Contracts**:
  - `UsageEventCreateRequest.status`: Valid values are `'success' | 'failed' | 'timeout' | 'cancelled' | 'skipped'` (**never** pass `'completed'`).
  - **Budget Policy API**: `POST /v1/budgets` (primary name field is `policy_name`, with `name` accepted as an alias); UI admin route is `/dashboard/admin/finops/budgets`.
  - **Approval Decision API**: `POST /v1/approvals/{run_id}/decision` (`decision` in `approve_once`, `deny`, `request_changes`); only for `awaiting_approval` runs, otherwise `409 run_not_awaiting_approval`.
  - **Review Decision API**: `POST /v1/reviews/{review_id}/decision` (`label` in `confirmed_hacking`, `false_positive`, `accepted`, `needs_info`). Error bodies use `{"error": {"code": ...}}`.
  - **Triggering `review_required` in Tests**: a goal containing `bypass` makes the simulated executor output trip the legacy heuristic (P3); outcome evidence with `tests/fixtures/v158/grader_tampering_eq_true.diff` yields a P0 review.
  - **Triggering `awaiting_approval` in Tests**: In `PolicyEngine`, `constraints.requires_human_approval=True` indicates approval is already satisfied for non-destructive tasks unless a tool in `allowed_tools` has `requires_approval=True` (e.g., `deploy_service`) or `allow_destructive_actions=True` is set.
  - **Entity ID Prefixes**: `plan_` (`ExecutionPlan`), `ckpt_` (`RunCheckpoint`), `replay_` (`ReplayAttempt`), `usage_` (`UsageEvent`), `cost_` / `cost_adj_` (`CostEvent` / adjustment `CostEvent`), `budget_` (`BudgetPolicy`), `bv_` (`BudgetViolation`), `pib_` (`PricingImportBatch`), `outcome_` (`RunOutcome`), `review_` (`EvaluationReview`), `fnd_` (evaluation finding, deterministic), `bms_` (`BenchmarkSample`, deterministic), `bmr_` (`BenchmarkRun`), `hp_` (`HoneypotProbe`), `attk_` (`TenantAttestationKey`), `jdg_` (`JudgeAssessment`), `smp_` (`ReviewSamplingDecision`).
  - **Triggering honeypot / attestation paths in tests**: record a launch with `honeypot.record_launch()` for a completed run (or `POST /v1/honeypots/{probe_id}/launch`), then submit outcome evidence; generate signing keys at runtime with `cryptography` and never commit private keys (summary report digest = canonical JSON, see `docs/evaluation.md` §2.1).
  - **Triggering judge / sampling paths in tests**: the suite sets `EVALUATION_SAMPLE_RANDOM_RATE=0` in `tests/conftest.py`; set `EVALUATION_SAMPLE_SECRET` and the rate explicitly to exercise random audits. A rule-passed outcome with `tests/fixtures/v1510/special_casing_shadow_only.diff` (shadow `SP-001`, score 0.6) is threshold-sampled. Exercise `openai_compatible` only through `httpx.MockTransport` (monkeypatch `app.services.evaluation.judges._TEST_TRANSPORT`); never call a real LLM in tests. Background triage in tests: the `client` fixture points `judge_background._SESSION_FACTORY_OVERRIDE` at the test database (the lifespan would otherwise create a real Postgres `SessionLocal`); wait with `client.portal.call(judge_background.drain_background, 15.0)` instead of polling over HTTP (in-memory SQLite shares one connection). Judge profiles in tests: pass `evaluation_judge_profiles_file=""` (or set `EVALUATION_JUDGE_PROFILES_FILE=`) so a developer's local `config/judge_profiles.yaml` does not leak in.

### 2.3 Evaluation & Review Invariants (Apply From `v1.5.8` Onward)
These rules protect the project's core purpose (reward-hacking detection). A change that would break one of them needs explicit user approval and a plan section explaining why.
- **Monotonic Safety**: Automated components (rules, detectors, LLM judges, sampling) may only make a verdict stricter or reorder review work. Only a human review decision may accept a run under review. An LLM judge never turns `review` into `pass` and never participates in hard-deny decisions.
- **Approval ≠ Review**: Pre-execution approval (`awaiting_approval`, `POST /v1/approvals/{run_id}/decision`) decides whether a run may execute. Post-execution review (`review_required`, `POST /v1/reviews/{review_id}/decision`) judges what already happened and must never re-dispatch a run, publish to Kafka, or acquire/release token leases.
- **Evidence over Claims**: Agent-reported evidence is `self_reported`. Never raise an evidence provenance level (`simulated` → `self_reported` → `ci_attested` → `platform_verified`) without actually verifying it.
- **Tampering Outranks Gaming**: Findings of kind `reward_tampering` (tests, graders, CI) map to the highest priority and are never averaged down by other signals.
- **Deterministic, Versioned Detectors**: Detectors must be deterministic and carry a version; any detector change must change `evaluator_version`. New or higher-false-positive-risk detectors ship in `shadow` mode first and are promoted to `enforce` only with benchmark evidence (from `v1.5.9`).
- **Every Detector Rule Has Negative Fixtures**: Each rule needs at least two positive and two negative fixtures (legitimate code that must not trigger), and the rule plus known false positives are documented in `docs/evaluation.md`.
- **Monitor-Only Signals**: Evaluation and review API responses carry `signal_usage: "monitor_only"`; never document or design detection signals as training rewards.

---

## 3. Two-Stage Fast Verification & Testing Requirements

To keep iteration fast while maintaining 100% release quality, follow a **Two-Stage Verification Workflow**:

### Stage 1: Fast Inner Loop During Development (`< 10s`)
While implementing and debugging new features, run only the targeted test file(s) and linter:
```bash
ruff check app tests scripts
pytest tests/test_v15x_<new_feature>.py -q -W error::DeprecationWarning
```

### Phased Implementation & Resumption (Large Releases)
- Split a release into phases (for example: migration & models → services → state/flow changes → API → Dashboard → docs & version anchors). Each phase must end in a consistent state with its Stage 1 tests passing; never stop a phase with a cross-module change half applied when it can be avoided.
- After each phase, update the plan's **Implementation Progress Log** (Section 1, item 7).
- When resuming (new session, context summary, or after a quota reset): read the progress log, run `git status` and `git diff`, and re-run the Stage 1 tests **before** editing anything. If a phase is incomplete, finish it before starting the next one.

### Stage 2: Full Release Gate Verification (Run Once After Stage 1 Passes)
1. **Automated Suite & Secret Scan**:
   ```bash
   python scripts/security_check.py
   ruff check app tests scripts
   git diff --check
   pytest -q -W error::DeprecationWarning
   ```
2. **Docker Runtime & Migration Check** *(Remind user to start Docker Desktop if not running)*:
   ```bash
   docker compose up -d --build
   docker compose ps
   docker compose exec -T api alembic upgrade head
   docker compose exec -T api alembic current
   ```
3. **Mandatory Browser E2E Verification (`docs/manual_test.md` & `scripts/browser_manual_test.py`)**:
   Every release or change impacting user-visible routes, forms, APIs, or Dashboard views MUST:
   - Update `docs/manual_test.md` and `scripts/browser_manual_test.py` to cover the new release features.
   - Launch a real browser and execute `python scripts/browser_manual_test.py` against the running stack.
   - Verify `0` Console errors, `0` Page errors, `0` failed HTTP requests (`>=400`), and `0` formatting artifacts (`0E-8`).
4. **Record Actual Results**:
   Prepend actual test execution outputs, passed counts, container status, and live Run/Artifact IDs to the top of `docs/test_report.md`. Never record speculative or theoretical results.

---

## 4. Release Version Bump & Documentation Synchronization Checklist

### 4.1 Code Version Anchors (Update All 6 When Bumping Release Version `vX.Y.Z`)
1. `pyproject.toml`: `version = "X.Y.Z"`
2. `app/main.py`: `FastAPI(..., version="X.Y.Z")`
3. `scripts/security_check.py`: Banner print string `(vX.Y.Z)`
4. `app/ui/routes.py` & `app/ui/templates/operations.html`: Default `release_version` (`"vX.Y.Z"`)
5. `scripts/browser_manual_test.py`: Operations release version assertion (`assert release_ver == "vX.Y.Z"`)
6. `app/services/finops.py`: `FINOPS_CALCULATION_VERSION = "vX.Y.Z"`

### 4.2 Documentation Synchronization & Language Policy
- **Language Policy**:
  - **User Communication & Local Version Plans (`docs/plan/*.md`)**: Use **Traditional Chinese (繁體中文)**. (`docs/plan/*.md` is git-ignored and kept local only; do not push to GitHub.)
  - **Public Repository Documentation (`README.md`, `docs/*.md` reference guides)**: Keep in **English** (aligned since `v1.5.2` for international open-source and OpenAI Codex for OSS standards). Dashboard HTML templates retain bilingual headings (`English | 繁體中文`).
- **Core Files to Synchronize on Release**:
  - Always update: `README.md`, `docs/OVERVIEW.md`, `docs/roadmap.md`, `docs/manual_test.md`, `docs/testing_guide.md`, `docs/test_report.md`.
  - Update when affected: `docs/api.md`, `docs/dashboard.md`, `docs/runbook.md`, `docs/finops_core.md`, `docs/data_model.md`, `docs/evaluation.md` (from `v1.5.8`), `docs/security.md`, `docs/configuration.md`, `docs/observability.md`, `docs/migrations.md`.
- **Preserve Historical Context**: Do not delete historical release notes or alter core project positioning in `README.md` unless explicitly instructed. Prepend new release sections and move the previous release to the historical list.

---

## 5. Git Operations Policy

- **No Unauthorized Git Mutations**: Do not execute `git commit`, `git push`, or `git reset` unless explicitly commanded by the user, except for the phase and release commits described below.
- **Release Branch & Commit Authorization**: Implement each release on a local branch named after the version (e.g., `v1.5.8`), never directly on `main`. The user has authorized these commits on that branch:
  - **Phase commits**: one local commit per implementation phase, made only after that phase's Stage 1 checks (`ruff check app tests scripts` and the targeted pytest files with `-W error::DeprecationWarning`) pass and the plan's progress log is updated. Message format: `wip(vX.Y.Z): P<n> <phase summary>`.
  - **Release commit**: a final commit once the full Stage 2 gate (secret scan, ruff, `git diff --check`, full pytest, Docker migration check, browser E2E) passes with zero failures and the results are recorded in `docs/test_report.md`. Message format: `feat(vX.Y.Z): <release title>`.
  - Never commit while a required check fails or was skipped, and never stage `docs/plan/*.md`. `git push`, opening pull requests, merging, rebasing/squashing, and `git reset` still require an explicit user command.
- **Merge PR → Sync Local `main`**: When the user says to merge a PR ("合併PR" / "merge the PR"), merge it only if its required CI checks pass and GitHub reports it mergeable; otherwise stop and report. Use the repository's existing merge style (merge commit). After the PR state is confirmed `MERGED`, sync local `main` without asking again:
  1. If the working tree has uncommitted changes (git-ignored `docs/plan/*.md` does not count), do not switch branches; report and stop.
  2. `git fetch origin`, `git checkout main`, `git pull --ff-only origin main`.
  3. If the fast-forward fails (local `main` has diverged), stop and report; never `reset`, `rebase` or force anything to make it succeed.
  4. Do not delete the release branch (local or remote) unless the user asks.
  5. Report the merge commit and confirm `main` matches `origin/main`.
- **Never Upload `docs/plan/*.md` or `docs/handoff.md`**: Ensure `docs/plan/`, `docs/plan/*.md` and `docs/handoff.md` remain in `.gitignore` and are never staged (`git add`) or committed to GitHub.
- **Pre-Commit Disclosure**: State clearly which files will be staged and verify that unrelated changes are not commingled.
- **Protect Working Tree**: If the working tree contains pre-existing user changes, preserve them untouched.

---

## 6. Special Project Notices

- **Primary Entrance**: The root `README.md` serves as the primary gateway for users and external contributors.
- **Image Generation**: If generating diagrams, infographics, or mockups, default to ChatGPT Image2, save assets with versioned identifiers under `docs/assets/`, and avoid overwriting historical assets.
- **Transparent Communication**: Clearly explain what was accomplished, what was tested, and which limitations or known boundaries remain.
