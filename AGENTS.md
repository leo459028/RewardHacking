# Project Working Guidelines & Agent Specification (`AGENTS.md`)

This document provides operational guidelines, architectural quick-reference maps, domain conventions, and fast-iteration workflows for AI agents and human engineers working on **RewardHacking**. Please adhere to these guidelines first before referring to `README.md`.

---

## Scope of Application

These specifications apply across the entire repository. Unless a subdirectory provides a more specific `AGENTS.md`, all feature planning, code modifications, test additions, and documentation updates must comply with this document.

---

## 0. Codebase Quick Map & Fast-Iteration Reference

To minimize exploratory file reads and maximize iteration speed and model quota efficiency, use this map to locate core modules immediately:

| Layer / Domain | Primary Files | Key Responsibilities & Notes |
| :--- | :--- | :--- |
| **App Entry & Config** | `pyproject.toml`<br>`app/main.py`<br>`app/core/config.py` | Package version, FastAPI initialization & middleware (`X-Trace-Id`, Prometheus HTTP metrics), and environment settings (`get_settings()`). |
| **REST API Layer** | `app/api/routes.py`<br>`app/api/deps.py` | All `/v1/*` JSON endpoints (submit, runs, trace, replay-bundle, checkpoints, approvals, worker DLQ, governance, control-plane, FinOps `/v1/pricing`, `/v1/budgets`, `/v1/budget-violations`, `/v1/cost/*`, `/v1/admin/finops/*`, operations). |
| **Dashboard UI Layer** | `app/ui/routes.py`<br>`app/ui/templates/*.html`<br>`app/ui/static/styles.css` | Server-rendered Jinja2 operational portal (`/dashboard/*`, 14 primary views). Uses custom Jinja2 filters `json_pretty` and `decimal_plain`. |
| **Guardrail & Policy** | `app/services/prompt_sanitizer.py`<br>`app/services/classical_chinese_guard.py`<br>`app/services/risk_scoring.py`<br>`app/services/policy_engine.py`<br>`app/services/orchestrator.py` | Pre-execution prompt sanitization, Classical Chinese adversarial detection, composite risk scoring (`0.0`–`1.0`), deterministic `allow`/`escalate`/`deny` policy decisions, and bounded tool simulation. |
| **Control Plane & Step Audit** | `app/services/control_plane.py`<br>`app/models/control_plane.py`<br>`app/schemas/control_plane.py` | `ControlPlanePlanner.plan()`, workflow/tool registries, `ExecutionPlan` & `ExecutionPlanStep` persistence, and lifecycle synchronization (`sync_execution_plan_on_approval`, `sync_execution_plan_on_worker_completion`). |
| **Checkpoints & Replay** | `app/services/checkpoint.py`<br>`app/models/checkpoint.py`<br>`app/schemas/checkpoint.py` | `RunCheckpoint` (`ckpt_...`) and `ReplayAttempt` (`replay_...`) persistence, checkpoint resume re-dispatch, and dry-run replay. |
| **Agent FinOps Engine** | `app/services/finops.py`<br>`app/services/pricing_import.py`<br>`app/models/finops.py`<br>`app/schemas/finops.py`<br>`app/seeds/model_pricing_seed.yaml` | 149-entry official model pricing catalog, context-tier resolution, `record_submit_usage()`, `record_actual_usage()`, multi-scope (`run`/`tenant`/`workflow`/`user`/`project`/`model`) & multi-period budget enforcement (`fail_closed`, `require_approval`, `degrade_model`, `dry_run_only`, `warn`), `failed_retry` cost accounting, and violation triage. |
| **Audit, DLQ & Operations** | `app/services/audit_log.py`<br>`app/services/operations.py`<br>`app/workers/consumer.py` | `AgentRun`, `ApprovalEvent` (including `expire_stale_approvals`), `WorkerAttempt` (including `requeue_dlq_run`, `resolve_dlq_run`), `GovernanceEvent`, `Incident`, `process_run_by_id()`, and release readiness gate checks. |
| **Database & Migrations** | `migrations/versions/`<br>`app/models/` | Current Alembic Head: `202606010004` (`20260601_0004_v151_finops_core_expansion.py`). Check if existing columns/JSONB fields suffice before creating new migrations. |

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

Do not write vague conceptual sketches; ensure the plan provides actionable step-by-step instructions that subsequent engineers can execute directly.

---

## 2. Implementation Requirements, Domain Conventions & Quota Efficiency

### 2.1 General Implementation Rules
- **Maintain Architectural Continuity**: Follow established service boundaries and dataflows unless an architectural change is explicitly requested.
- **Read Before Modifying**: Inspect target line ranges before editing, and **batch multiple `read_file` or `edit_file` tool calls in parallel within a single turn** to save model quota and wall-clock time.
- **Preserve Unrelated Changes**: Never revert or discard existing user modifications in the working tree.

### 2.2 Domain Conventions & Anti-Footguns (Prevent Avoidable Retries)
- **Zero-Warning Pydantic V2 Serialization**: Never use deprecated `json_encoders` in `ConfigDict`. Always use `Annotated[Decimal, PlainSerializer(format_decimal_plain, return_type=str, when_used="json")]`. All pytest runs must pass with `-W error::DeprecationWarning`.
- **Plain Decimal Formatting (`0` `0E-8` Artifacts)**: All monetary/quantity `Decimal` values rendered in JSON responses or Jinja2 templates must use `format_decimal_plain()` or the `|decimal_plain` template filter so scientific notation (`0E-8`) never appears in the UI or API.
- **Key Enum & Route Contracts**:
  - `UsageEventCreateRequest.status`: Valid values are `'success' | 'failed' | 'timeout' | 'cancelled' | 'skipped'` (**never** pass `'completed'`).
  - **Budget Policy API**: `POST /v1/budgets` (primary name field is `policy_name`, with `name` accepted as an alias); UI admin route is `/dashboard/admin/finops/budgets`.
  - **Approval Decision API**: `POST /v1/approvals/{run_id}/decision` (`decision` in `approve_once`, `deny`, `request_changes`).
  - **Triggering `awaiting_approval` in Tests**: In `PolicyEngine`, `constraints.requires_human_approval=True` indicates approval is already satisfied for non-destructive tasks unless a tool in `allowed_tools` has `requires_approval=True` (e.g., `deploy_service`) or `allow_destructive_actions=True` is set.
  - **Entity ID Prefixes**: `plan_` (`ExecutionPlan`), `ckpt_` (`RunCheckpoint`), `replay_` (`ReplayAttempt`), `usage_` (`UsageEvent`), `cost_` (`CostEvent`), `budget_` (`BudgetPolicy`), `bv_` (`BudgetViolation`), `pib_` (`PricingImportBatch`).

---

## 3. Two-Stage Fast Verification & Testing Requirements

To keep iteration fast while maintaining 100% release quality, follow a **Two-Stage Verification Workflow**:

### Stage 1: Fast Inner Loop During Development (`< 10s`)
While implementing and debugging new features, run only the targeted test file(s) and linter:
```bash
ruff check app tests scripts
pytest tests/test_v15x_<new_feature>.py -q -W error::DeprecationWarning
```

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

### 4.1 Code Version Anchors (Update All 5 When Bumping Release Version `vX.Y.Z`)
1. `pyproject.toml`: `version = "X.Y.Z"`
2. `app/main.py`: `FastAPI(..., version="X.Y.Z")`
3. `scripts/security_check.py`: Banner print string `(vX.Y.Z)`
4. `app/ui/routes.py` & `app/ui/templates/operations.html`: Default `release_version` (`"vX.Y.Z"`)
5. `scripts/browser_manual_test.py`: Operations release version assertion (`assert release_ver == "vX.Y.Z"`)

### 4.2 Documentation Synchronization & Language Policy
- **Language Policy**:
  - **User Communication & Local Version Plans (`docs/plan/*.md`)**: Use **Traditional Chinese (繁體中文)**. (`docs/plan/*.md` is git-ignored and kept local only; do not push to GitHub.)
  - **Public Repository Documentation (`README.md`, `docs/*.md` reference guides)**: Keep in **English** (aligned since `v1.5.2` for international open-source and OpenAI Codex for OSS standards). Dashboard HTML templates retain bilingual headings (`English | 繁體中文`).
- **Core Files to Synchronize on Release**:
  - Always update: `README.md`, `docs/OVERVIEW.md`, `docs/roadmap.md`, `docs/manual_test.md`, `docs/testing_guide.md`, `docs/test_report.md`.
  - Update when affected: `docs/api.md`, `docs/dashboard.md`, `docs/runbook.md`, `docs/v1.5.1_finops_core.md`, `docs/data_model.md`.
- **Preserve Historical Context**: Do not delete historical release notes or alter core project positioning in `README.md` unless explicitly instructed. Prepend new release sections and move the previous release to the historical list.

---

## 5. Git Operations Policy

- **No Unauthorized Git Mutations**: Do not execute `git commit`, `git push`, or `git reset` unless explicitly commanded by the user.
- **Never Upload `docs/plan/*.md`**: Ensure `docs/plan/` and `docs/plan/*.md` remain in `.gitignore` and are never staged (`git add`) or committed to GitHub.
- **Pre-Commit Disclosure**: State clearly which files will be staged and verify that unrelated changes are not commingled.
- **Protect Working Tree**: If the working tree contains pre-existing user changes, preserve them untouched.

---

## 6. Special Project Notices

- **Primary Entrance**: The root `README.md` serves as the primary gateway for users and external contributors.
- **Image Generation**: If generating diagrams, infographics, or mockups, default to ChatGPT Image2, save assets with versioned identifiers under `docs/assets/`, and avoid overwriting historical assets.
- **Transparent Communication**: Clearly explain what was accomplished, what was tested, and which limitations or known boundaries remain.
