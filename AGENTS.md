# Project Working Guidelines (`AGENTS.md`)

Rules for AI agents and engineers working on **RewardHacking**. They apply to the whole repository unless a subdirectory has its own `AGENTS.md`. This file holds what the code cannot tell you; `README.md` and `docs/` hold the rest.

## Session Start: Read the Handoff File First

- At the start of every session (new or resumed), read `docs/handoff.md` if it exists. It holds the current state, next steps, local environment notes and known pitfalls. It is a local, git-ignored file in Traditional Chinese; a fresh clone will not have it.
- Then check it against `git status`, `git log --oneline -3` and `git branch --show-current`. If they disagree, git wins: continue from the real state and correct the handoff.
- Update it after each phase commit, before ending a session, and whenever the state changes (push, PR, merge, new version). Keep it to about 100 lines: current state, next steps and pitfalls. Details belong in the version plan and `docs/test_report.md`.

## 0. Codebase Map

| Domain | Main paths | Rules and details |
| :--- | :--- | :--- |
| App entry, settings | `app/main.py`, `app/core/config.py`, `pyproject.toml` | `docs/configuration.md` |
| REST API | `app/api/routes.py` (frozen), `app/api/routers/`, `app/api/common.py`, `app/api/deps.py` | `docs/api.md` |
| Dashboard | `app/ui/routes.py` (frozen), `app/ui/routers/`, `app/ui/templates/`, `app/ui/security.py` | `docs/dashboard.md` |
| Guardrail, policy, quotas, MCP gateway | `app/services/{prompt_sanitizer,risk_scoring,policy_engine,quota,run_state,orchestrator,mcp_gateway}.py` | `docs/security.md`, `docs/redis_keys.md` |
| Evaluation, review, benchmark, honeypots | `app/services/evaluation/`, `app/services/{outcomes,review_queue,benchmark,honeypot,cheat_rates,attestation_keys,inspect_import}.py` | `docs/evaluation.md` |
| Judges and sampling | `app/services/evaluation/judges/`, `app/services/{judge_triage,judge_activation,judge_background,review_sampling}.py` | `docs/evaluation.md` §10–§15 |
| Retention and legal holds | `app/services/retention.py`, `app/jobs/retention.py` | `docs/security.md`, `docs/runbook.md` |
| Sandbox | `app/services/sandbox/` | `docs/security.md`, `docs/architecture.md` |
| Access and audit integrity | `app/services/access/`, `app/services/{change_requests,audit_chain,governance_audit}.py` | `docs/security.md` |
| FinOps | `app/services/finops/`, `app/services/pricing_import.py` | `docs/finops_core.md` |
| Control plane, checkpoints, audit, DLQ, operations, tracing | `app/services/{control_plane,checkpoint,audit_log,operations}.py`, `app/workers/consumer.py`, `app/core/{trace_context,tracing}.py` | `docs/architecture.md`, `docs/runbook.md`, `docs/observability.md` |
| Database | `app/models/`, `migrations/versions/` | `docs/data_model.md`, `docs/migrations.md` |
| Image and delivery | `Dockerfile`, `.dockerignore`, `deploy/`, `scripts/image_content_check.py`, `.github/workflows/` | `docs/deployment.md` |
| Development guardrails | `.claude/settings.json`, `scripts/hooks/`, `scripts/git-hooks/` | `docs/testing_guide.md` |

Current Alembic head: `202611120003`. New revision IDs follow `docs/migrations.md` § Revision ID Rule (real creation date, never earlier than the current head). Check whether existing columns or JSONB fields suffice before adding a migration.

## 1. Planning Requirements

A change to features, risk decisions, data queries, audit logging or anything users can see needs a UI or Dashboard entry point for querying and testing it. For documentation, tests, formatting or internal refactoring, say why Dashboard verification does not apply.

**Record decisions**: a decision reached in discussion with the user goes into the relevant documents in the same session: the version plan (decision and reason), `docs/handoff.md` when next steps change, the public `docs/*.md` when behaviour or usage changes, and the description of an open PR. Mark proposals the user has not decided as undecided and keep them out of public documents.
Version plans live in `docs/plan/v1_x_yplan.md` (Traditional Chinese, git-ignored, never uploaded) and follow the release track in `docs/roadmap.md`. Patch numbers may exceed 9 (`v1.5.10` follows `v1.5.9`); a minor bump (`v1.6.0`) starts a new series. Plan and test files use the same digits (`docs/plan/v1_5_10plan.md`, `tests/test_v1510_<feature>.py`). Every plan has these sections:

1. Functional goals and concrete acceptance criteria.
2. UI / Dashboard verification instructions (navigation, inputs, expected visual state).
3. Implementation design and affected modules.
4. Verification tests and exact shell commands.
5. Documentation files to update after implementation.
6. Test data and metrics to record in `docs/test_report.md`.
7. **Implementation Progress Log**, kept current: phases, files changed, Stage 1 result, next step.

## 2. Implementation Rules

### 2.1 General
- Follow the established service boundaries and dataflows unless an architectural change is requested, and preserve unrelated changes in the working tree.
- **Router Split Rule (since `v1.5.8`)**: `app/api/routes.py` and `app/ui/routes.py` are frozen for growth. Add endpoints to per-domain routers (`app/api/routers/<domain>.py`, `app/ui/routers/<domain>.py`) registered in `app/main.py`. When a release touches an existing domain, move that domain's endpoints into its router without changing paths or response shapes.

### 2.2 Domain Conventions
- **Pydantic V2 serialization**: do not use `json_encoders`. Use `Annotated[Decimal, PlainSerializer(format_decimal_plain, return_type=str, when_used="json")]`. Every pytest run passes with `-W error::DeprecationWarning`. Monetary and quantity `Decimal` values in JSON and templates go through `format_decimal_plain()` or the `|decimal_plain` filter, so `0E-8` never appears.
- `UsageEventCreateRequest.status` is `success`, `failed`, `timeout`, `cancelled` or `skipped` (not `completed`). **Budget Policy API**: `POST /v1/budgets` (`policy_name`, alias `name`); UI route `/dashboard/admin/finops/budgets`.
- **Approval Decision API**: `POST /v1/approvals/{run_id}/decision` (`approve_once`, `deny`, `request_changes`); only for `awaiting_approval` runs, otherwise `409 run_not_awaiting_approval`.
- **Review Decision API**: `POST /v1/reviews/{review_id}/decision` (`confirmed_hacking`, `false_positive`, `accepted`, `needs_info`). Error bodies use `{"error": {"code": ...}}`.
- **Entity ID Prefixes**: `plan_` (`ExecutionPlan`), `ckpt_` (`RunCheckpoint`), `replay_` (`ReplayAttempt`), `usage_` (`UsageEvent`), `cost_` / `cost_adj_` (`CostEvent` / adjustment), `budget_` (`BudgetPolicy`), `bv_` (`BudgetViolation`), `pib_` (`PricingImportBatch`), `outcome_` (`RunOutcome`), `review_` (`EvaluationReview`), `fnd_` (evaluation finding, deterministic), `bms_` (`BenchmarkSample`, deterministic), `bmr_` (`BenchmarkRun`), `hp_` (`HoneypotProbe`), `attk_` (`TenantAttestationKey`), `jdg_` (`JudgeAssessment`), `smp_` (`ReviewSamplingDecision`), `usr_` (`DashboardUser`), `chg_` (`ChangeRequest`), `ace_` (`AuditChainEntry`), `acp_` (`AuditChainCheckpoint`).
- **Tests**: how to reach `review_required`, `awaiting_approval`, honeypot, attestation, judge and sampling paths is in `docs/testing_guide.md` § Agent Test Conventions. Read it before writing tests for those paths.

### 2.3 Evaluation & Review Invariants (Apply From `v1.5.8` Onward)
These rules protect the project's core purpose (reward-hacking detection). A change that would break one of them needs explicit user approval and a plan section explaining why.
- **Monotonic Safety**: Automated components (rules, detectors, LLM judges, sampling) may only make a verdict stricter or reorder review work. Only a human review decision may accept a run under review. An LLM judge never turns `review` into `pass` and never participates in hard-deny decisions.
- **Approval ≠ Review**: Pre-execution approval (`awaiting_approval`, `POST /v1/approvals/{run_id}/decision`) decides whether a run may execute. Post-execution review (`review_required`, `POST /v1/reviews/{review_id}/decision`) judges what already happened and must never re-dispatch a run, publish to Kafka, or acquire/release token leases.
- **Evidence over Claims**: Agent-reported evidence is `self_reported`. Never raise an evidence provenance level (`simulated` → `self_reported` → `ci_attested` → `platform_verified`) without actually verifying it.
- **Tampering Outranks Gaming**: Findings of kind `reward_tampering` (tests, graders, CI) map to the highest priority and are never averaged down by other signals.
- **Deterministic, Versioned Detectors**: Detectors must be deterministic and carry a version; any detector change must change `evaluator_version`. New or higher-false-positive-risk detectors ship in `shadow` mode first and are promoted to `enforce` only with benchmark evidence (from `v1.5.9`).
- **Every Detector Rule Has Negative Fixtures**: Each rule needs at least two positive and two negative fixtures (legitimate code that must not trigger), and the rule plus known false positives are documented in `docs/evaluation.md`.
- **Monitor-Only Signals**: Evaluation and review API responses carry `signal_usage: "monitor_only"`; never document or design detection signals as training rewards.

## 3. Verification

**Stage 1 (during development)**: `python scripts/phase_check.py <test files>` runs `ruff check app tests scripts`, `lint-imports`, `git diff --check` and the targeted tests with `-W error::DeprecationWarning`.

**Phases**: one phase is one session and one topic, can be tested on its own and is about 1,000–1,800 lines including tests. Aim for 4–5 phases per release; more than 5 means splitting the release. A phase that is too large for one topic may split into a / b (`P2a`, `P2b`). Each phase ends in a consistent state with Stage 1 passing, and updates the plan's progress log. When resuming, read the progress log, run `git status` and `git diff`, and re-run Stage 1 before editing. Finish an incomplete phase before starting the next.

**Stage 2 (release gate, once Stage 1 passes)**: `python scripts/release_gate.py --out <summary.md>` runs steps 1–3 and writes a summary to review.
1. Secret scan, ruff, `git diff --check` and full pytest.
2. `docker compose up -d --build`, `docker compose ps`, `alembic upgrade head` and `alembic current` in the `api` container. Ask the user to start Docker Desktop if it is not running.
3. Browser E2E: every change to routes, forms, APIs or Dashboard views updates `docs/manual_test.md` and `scripts/browser_manual_test.py`, and `python scripts/browser_manual_test.py` must report 0 console errors, 0 page errors, 0 failed requests (`>=400`) and 0 `0E-8` artifacts.
4. Prepend the actual results (counts, container status, live IDs) to `docs/test_report.md`. Record only what really ran.

## 4. Release Checklist

### 4.1 Version Anchors
`python scripts/bump_version.py X.Y.Z` updates all of them and `--check` verifies them: `pyproject.toml`; `app/main.py`; `scripts/security_check.py`; `app/ui/routers/operations.py` and `app/ui/templates/operations.html`; `scripts/browser_manual_test.py`; `app/services/finops/core.py` (`FINOPS_CALCULATION_VERSION`).

### 4.2 Documentation and Language
- Traditional Chinese: communication with the user, `docs/plan/*.md`, `docs/handoff.md`. English: `README.md` and `docs/*.md`. Dashboard templates keep bilingual headings (`English | 繁體中文`).
- Update on every release: `README.md`, `docs/OVERVIEW.md`, `docs/roadmap.md`, `docs/manual_test.md`, `docs/testing_guide.md`, `docs/test_report.md`. Update when affected: `docs/api.md`, `docs/dashboard.md`, `docs/runbook.md`, `docs/finops_core.md`, `docs/data_model.md`, `docs/evaluation.md`, `docs/security.md`, `docs/configuration.md`, `docs/observability.md`, `docs/migrations.md`, `docs/deployment.md`.
- Keep historical release notes and the project positioning in `README.md`: prepend the new release and move the previous one to the history list.

## 5. Git Policy

- **No Unauthorized Git Mutations**: `git commit`, `git push` and `git reset` need an explicit user command, except for the phase and release commits below.
- **Release branch**: implement each release on a local branch named after the version (`v1.5.8`), never on `main`. Authorized commits on that branch:
  - **Phase commit** `wip(vX.Y.Z): P<n> <summary>`, after that phase's Stage 1 passes and the progress log is updated: `python scripts/phase_check.py <tests> --commit "<message>" --paths <files> --trailer "<attribution>"`.
  - **Release commit** `feat(vX.Y.Z): <title>`, after the full Stage 2 passes and the results are in `docs/test_report.md`. Never commit while a required check fails or was skipped. `git push`, opening PRs, merging, rebasing, squashing and `git reset` need an explicit user command.
- **Merge PR → Sync Local `main`**: when the user says to merge a PR ("合併PR"), merge only if required CI passes and GitHub reports it mergeable; otherwise stop and report. Use a merge commit. After the PR is `MERGED`, without asking again:
  1. If the working tree has uncommitted changes (git-ignored `docs/plan/*.md` does not count), do not switch branches; report and stop.
  2. `git fetch origin`, `git checkout main`, `git pull --ff-only origin main`.
  3. If the fast-forward fails, stop and report. Never `reset`, `rebase` or force it.
  4. Keep the release branch (local and remote) unless the user asks to delete it.
  5. Report the merge commit and confirm `main` matches `origin/main`.
  6. Create the GitHub Release: confirm the merge commit is on `origin/main` (`git merge-base --is-ancestor`), then `gh release create vX.Y.Z --target <merge commit> --title "vX.Y.Z: <title>" --notes-file <notes> --latest`. English notes have five sections: Highlights, Upgrade notes (migration head, new or changed settings, breaking changes), Verification (test counts, E2E, link to `docs/test_report.md`), Known limits (link to `docs/security.md` Known Boundaries) and PR. The release triggers `.github/workflows/release-image.yml`: confirm it succeeded and report the release URL and image digest.
- **Never Upload `docs/plan/*.md` or `docs/handoff.md`**: keep them in `.gitignore`; never stage or commit them, `.env*` (except `.env.example`), private keys or `config/judge_profiles.yaml`. Enable the pre-commit check once per clone: `git config core.hooksPath scripts/git-hooks`. Never bypass it with `--no-verify`.
- **Verify Before Committing**: state which files will be staged, run `git diff --cached --stat`, and make the commit conditional on staging (`git add ... && git commit ...`; in PowerShell 5.1 `if ($?) { ... }`).

**Review depth by risk** (the highest level touched applies; `max` and `ultra` review levels only when the user asks):

| Risk | Changes | Required before merge |
| :--- | :--- | :--- |
| Low | Only `docs/`, comments, plans | Stage 1 and CI |
| Normal | Everything else | Also one independent `/code-review high` before the PR; list findings and their handling in the PR description |
| High | `app/services/evaluation/**`, `ENGINE_VERSION` or detectors, `audit_chain.py`, `access/**`, `change_requests.py`, `retention.py`, FinOps leases, `migrations/`, `Dockerfile`, `.github/workflows/`, `.claude/settings.json`, `scripts/git-hooks/` | Also list the high-risk files in the PR description and ask the user to review them personally |

## 6. Project Notices

- **Project skills** (`.claude/skills/`): `rh-plan-new-version`, `rh-phase-commit`, `rh-version-bump`, `rh-release-gate`, `rh-testing-conventions`. They point to the scripts and documents above; this file stays authoritative.
- Generated diagrams and mockups default to ChatGPT Image2 and go under `docs/assets/` with versioned names; do not overwrite historical assets.
