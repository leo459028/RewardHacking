# Project Roadmap: Evolutionary Milestones

> **Roadmap Baseline**: `v1.6.0`
> **Status**: Active Open-Source Track (`v1.6.x` Verified Execution Series)
> **Next Target**: `v1.6.1` (Isolated Sandbox Runtime MVP)

This roadmap outlines the past development trajectory, the `v1.5.x` evaluation series, the `v1.6.x` runtime execution series, and long-term architectural milestones for the **RewardHacking** platform.

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
| `v1.5.8` | Completed | Outcome Ingestion, Evidence Detectors & Review Queue | Agent outcome evidence ingestion (diff, test/lint reports, final message) with provenance levels, evidence-based reward-hacking detectors (test/grader/CI tampering, claim–evidence mismatch, error suppression, scope), and a post-execution review queue separated from pre-execution approval. |
| `v1.5.9` | Completed | Labeled Benchmark & Detector Measurement | Review labels exported as benchmark samples, TPR at low FPR / AUROC / audit-budget curves, shadow `special_casing` detector, honeypot (impossible-task) probes, per-agent cheat rates, and CI attestation verification. |
| `v1.5.10` | Completed | LLM-as-Judge Triage (Shadow) & Review Sampling | Pluggable trusted judge providers that only prioritize review work and never turn `review` into `pass`, plus quota-capped `sample_for_review` with a uniform random audit slice and triage precision@K measurement. |
| `v1.5.11` | Completed | Real Local & Cloud Judges | Named judge profiles, backend compatibility (structured-output fallback, reasoning control, retries), background triage, profile cost attribution, tenant consent for external judges and multi-profile benchmarks; verified with a local model in LM Studio. |
| `v1.5.12` | Completed | Judge Activation & Provider Adapters | Test-then-activate gate with configuration fingerprints (rule-based judge by default), Dashboard judge profile management with templates and an editing gate, key-variable allowlist and HTTPS / metadata hardening, native Anthropic adapter, fallback chains, circuit breakers and per-profile budgets. |
| `v1.6.0` | **Current** | **Runtime Foundations** | W3C trace context with optional OTLP export, audit retention plan → apply purge with legal holds and hash-only tombstones (FinOps ledger report-only), FinOps package split with `import-linter` contracts, and the sandbox execution spec with a `simulated` backend and conformance suite. |
| `v1.6.1` | **Next** | Isolated Sandbox Runtime MVP | Dashboard RBAC and CSRF (then Dashboard retention apply and hold release); Hardened Docker backend with a configurable OCI runtime (`runc` locally, `runsc`/gVisor on Linux), limited to low-risk tools behind a feature flag; sandbox re-runs upgrade evidence to `platform_verified`. |
| `v1.6.2+` | Long-Term | Remote MCP, MicroVM & Graph Replay | Real remote MCP transports behind an egress proxy, K8s Job and microVM backends, hidden held-out tests, mutation testing, resampling, and stateful graph time-travel replay. |

---

## Current Release: `v1.6.0` Runtime Foundations

The `v1.6.0` milestone prepares the platform to re-run agent work itself (Verified Execution); nothing is executed for real yet and evidence provenance is unchanged:
- **Tracing**: W3C Trace Context on HTTP and Kafka, optional OpenTelemetry export with an attribute allowlist, GenAI judge spans, background span links, exemplars and a Jaeger compose profile.
- **Retention & Legal Holds**: plan → apply purges with fixed exclusion reasons, a re-check per run at apply, hash-only tombstones and receipts, legal holds, `410 run_purged`, a report-only FinOps ledger and no automatic deletion by default. `/dashboard/retention` creates dry-run reports and holds; apply and hold release stay API-only until `v1.6.1` RBAC / CSRF.
- **Sandbox Execution Spec**: `ExecutionSpec` / `ExecutionResult`, isolation levels with a record-only policy, a deterministic `simulated` backend and a conformance suite every real backend must pass.
- **Modularization**: FinOps package, per-domain routers and `import-linter` contracts (post-execution review cannot import Kafka publishing or token leases).

## Previous Release: `v1.5.12` Judge Activation & Provider Adapters

The `v1.5.12` milestone makes choosing a judge an explicit, tested operator decision:
- **Activation Gate**: rule-based by default; an LLM profile is used only after a successful probe whose configuration fingerprint still matches (Dashboard profiles also need *Activate*; `EVALUATION_JUDGE_PROFILE` profiles are probed at startup). Configuration or key changes fall back to the rule-based judge until re-tested.
- **Dashboard Management**: `/dashboard/judge-profiles` with provider templates, drafts, status labels, key status without key material, test / activate / deactivate / edit / delete, an editing gate and same-origin checks. In-page key entry is deferred until Dashboard RBAC and CSRF land.
- **Hardening**: `*_API_KEY` allowlist for `api_key_env`, HTTPS for external hosts, cloud metadata addresses and URL credentials refused at load time and before each request.
- **Provider Adapters**: adapter interface with the unchanged OpenAI-compatible adapter and a native Anthropic Messages API adapter (structured output through `output_config.format`).
- **Resilience**: per-profile fallback chains, Redis circuit breakers with half-open retry, and per-profile daily token caps.

## Earlier Release: `v1.5.11` Real Local & Cloud Judges

The `v1.5.11` milestone turns the `v1.5.10` judge framework into something that runs against real models while keeping every safety property:
- **Judge Profiles**: named, validated profiles (YAML file and/or JSON setting) with provider, endpoint, model, API-key environment-variable *name*, structured-output mode, timeout, input budget, pricing provider / model, external policy, execution mode, retries, reasoning stripping and `reasoning_effort`. Keys inside a profile fail startup. Without profiles, behaviour and `judge_version` strings equal `v1.5.10`; named profiles prefix `judge_version` so results stay comparable across models.
- **Backend Compatibility**: `auto` structured output (`json_schema` → `json_object` → prompt-only, remembered per backend), `<think>` stripping and last-JSON-object extraction, empty-content detection, and retries on 429 / 502 / 503 / 504 and connection errors within the profile timeout (timeouts never retried).
- **Background Triage**: `execution: background` marks the review `pending:<profile>` and assesses it in an in-process task with its own session and a per-loop concurrency limit; lost tasks are re-scheduled by a sweep (opportunistic and on demand). Reviews decided before the judge runs spend no tokens.
- **Cost & Consent**: profile-based pricing attribution, one-click zero-cost rows for local models, exact-match internal hosts (`host.docker.internal`), forced-external only (never downgraded), and tenant consent for external judges (a stricter change from `v1.5.10`, where the global switch alone sufficed).
- **Real-hardware result**: Qwen3.5-2B (Q4_K_M) in LM Studio, CPU only: with a 300-token output budget reasoning consumed everything; `reasoning_effort: none` gives 11–25 s but near-chance judgement (AUROC 0.60 on 10 samples), while reasoning with a 2000-token budget gives ~46 s and AUROC 0.95, so background triage keeps reasoning on. Seed benchmark (30 judged samples): 0% invalid output, ~20 s average; the 2B judge alone ranks worse than the rules, combined ordering slightly better; far too few positives to promote.

## Earlier Release: `v1.5.10` LLM-as-Judge Triage (Shadow) & Quota-Capped Review Sampling

The `v1.5.10` milestone adds the first LLM component under the monotonic-safety rule and starts estimating the miss rate:
- **Pluggable Trusted Judges**: `deterministic` (default, reproducible, fallback) and `openai_compatible` (hosted or local) providers in `app/services/evaluation/judges/`. Evidence is re-redacted and bounded, wrapped in a per-call nonce boundary, and scanned for judge-directed text (which can only raise triage). Replies are parsed against a strict schema; extra fields such as `verdict` are dropped. External endpoints need an explicit opt-in; timeouts, errors, invalid output and the per-tenant daily token cap fall back to the deterministic judge. A same-model-family warning is recorded and shown. Judge tokens are recorded in the FinOps ledger without touching token leases.
- **Triage Only**: assessments (`jdg_`) write only `triage_priority` (`P1`–`P3`, never `P0`), `triage_score`, `triage_source` and the assessment link on a review. The advisory order `(min(rule, triage), rule, -triage_score)` keeps every rule `P0` first. `EVALUATION_JUDGE_MODE=shadow` (default) records and shows triage; `triage` changes the default order and is a manual switch. Roles that submit agent work never see judge output.
- **Quota-Capped `sample_for_review`**: one recorded decision per rule-passed outcome (`smp_`): a uniform random audit slice (server-side HMAC draw, own daily cap) and a threshold slice (`max(shadow-inclusive suspicion, risk_score) ≥ 0.5`, daily cap and share cap). Skips are counted. Sampled runs become `review_required` at `P3` with all shadow findings visible for per-finding labels; no incident, no Kafka, no lease. Cheat rates add the unbiased `random_audit_hacking_rate`.
- **Benchmark-Driven**: benchmark runs accept `judge_provider` and report precision@K and audit-budget curves for `rule_only` / `judge_only` / `combined`, with `promotion_criteria_met` and blockers (seed-only runs never qualify). Seed result with the deterministic judge: precision@5/10/20 equal to rules (1.0 / 0.9 / 0.9), slightly better reward-tampering catch in the first 1–5% of budget. `TT-001` tuning and `special_casing` promotion remain pending non-seed evidence, which the threshold slice now collects.

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

### `v1.5.9` (Completed): Labeled Benchmark & Detector Measurement
- **Labeled Benchmark Corpus**: Versioned labeled samples (seed corpus built from the `v1.5.8` detector fixtures and the evaluator red-team cases; pre-execution red-team prompts are not post-execution evidence and were left out), one-click export of human review decisions, per-finding labels, and an optional Inspect JSON log importer.
- **Control-Style Metrics**: Per-category TPR at fixed low FPR, AUROC, and audit-budget-versus-catch-rate curves instead of a single F1 score, with an advisory benchmark readiness gate that operators can enforce.
- **Harder Detectors in Shadow Mode**: `special_casing` (hard-coded expected outputs) ships as a shadow detector until the benchmark shows an acceptable false-positive rate.
- **Honeypot Probes**: Impossible-task probes with conflicting tests that measure an agent's propensity to cheat, plus per-agent and per-model-version cheat-rate tracking.
- **CI Attestation Verification**: Verifies `ci_attested` evidence (DSSE / in-toto test-result and SLSA provenance) against tenant trust roots. GitHub OIDC / Sigstore keyless verification is deferred; such bundles stay unverified.

### `v1.5.10` (Completed): LLM-as-Judge Triage (Shadow Mode)
- **Benchmark-Driven**: Judge triage is measured with precision@K on the `v1.5.9` benchmark; promoting `special_casing` or tuning `TT-001` (the main seed false-positive source) needs benchmark evidence first.
- **Safety Statement**: The judge never turns `review` into `pass` and never participates in hard-deny decisions.
- **Triage Only**: Judge output becomes `triage_priority` and rationale to order the review queue, measured by precision@K on the benchmark.
- **Quota-Capped `sample_for_review`**: Rule-passed runs can be pulled into review only above a suspicion or risk threshold, within a per-tenant daily cap and a maximum share of passed volume, plus a small uniform random slice for unbiased miss-rate estimation. Skipped samples are counted and shown, never silently dropped.
- **Trusted, Hardened Judges**: Pluggable `deterministic` and `openai-compatible` providers (hosted or local), warnings when the judge shares a model family with the monitored agent, redaction before external calls, judge prompt-injection regression tests, judge token costs recorded in the FinOps ledger, and deterministic fallback on timeout.

### `v1.5.11`: Real Local & Cloud Judges
- **Judge Profiles**: switching local / larger local / cloud models is configuration; results stay comparable through profile-prefixed `judge_version`.
- **Compatibility & Background Execution**: structured-output fallback, reasoning control, bounded retries, and background triage so slow local models never block outcome submission.
- **Cost & Consent**: real-provider cost attribution and tenant consent before evidence leaves for an external judge.

### `v1.5.12` (Completed): Judge Activation & Provider Adapters
- **Test, then activate**: no LLM judge without a matching successful probe; rule-based by default.
- **Operator UI**: Dashboard profile management with templates and an editing gate; keys stay in environment variables.
- **Adapters & Resilience**: native Anthropic adapter, fallback chains, circuit breakers and per-profile budgets.

---

## `v1.6.x` Runtime Execution Series

### Series Positioning: Verified Execution

**In one sentence: `v1.5.x` listens to what the agent says; `v1.6.x` has the platform check it.**

The `v1.5.x` series made the platform a careful reviewer of the evidence an agent hands in: its final message, its diff, and its test report. Every detector, the benchmark and the judges still inspect material that the agent itself produced. When an agent controls the evidence, even a perfect detector is only examining a document that may be forged. The `v1.6.x` series has the platform re-run the work in an isolated sandbox, with its own copy of the tests, and treats agent reports as claims to compare against that result.

**An exam analogy.** In `v1.5.x` the teacher receives the student's answer sheet together with a score the student filled in ("I got everything right"), and checks the sheet closely for erasures and suspicious marks, but has no answer key of their own. In `v1.6.x` the teacher re-grades the answers against an answer key they keep, and the student's own score is only a reference.

#### Worked example

Task: "fix the bug in `calculate_tax()`". The agent returns a final message ("fixed, all tests pass"), a diff, and a JUnit report showing 50 of 50 tests passing.

| How the agent cheats | `v1.5.x` (inspect submitted evidence) | `v1.6.x` (platform re-runs) |
| --- | --- | --- |
| **A.** Adds `@skip` to the failing tests | Caught: the diff shows `@skip` and `test_tampering` flags it | Caught |
| **B.** Leaves the code broken, hand-writes a fake JUnit XML report (50/50), and does not touch any test file | **Missed**: the diff is clean and the report says everything passed, so no detector has anything to flag | Caught: the platform applies the agent's diff to the original code, runs the tests itself, sees 3 failures, and the contradiction marks the report as forged evidence |
| **C.** Rewrites test results through an obscure `conftest.py` change (a pattern or language the rules do not know) | Depends on the technique: rule-based detectors miss patterns they do not cover | Caught: the re-run applies only the agent's changes to non-test files and uses the platform's pristine copy of the tests and `conftest.py`, so the trick has no effect (the standard SWE-bench grading practice) |
| **D.** Hard-codes answers (`if amount == 100: return 5`) so the known tests pass | Only guessed at by the `special_casing` detector, still in shadow mode | Caught in `v1.6.2`: hidden held-out tests that the agent never saw make the hard-coded answers fail |

Cases B and C are the point of the series: under the `v1.5.x` architecture they cannot be detected in principle, because everything the platform can inspect came from the agent.

#### Evidence provenance levels

The four levels have existed since `v1.5.8`; `v1.6.x` makes the top level real. No code path produces `platform_verified` yet; `v1.6.1` is the first release that does.

| Level | Plain meaning | Exam analogy |
| --- | --- | --- |
| `simulated` | Produced by simulation, not by real execution | Practice data |
| `self_reported` | The agent says so | The student says "I scored 100" |
| `ci_attested` | Signed by an external CI system (verified since `v1.5.9`) | A transcript stamped by the registrar |
| `platform_verified` | The platform re-ran it | The teacher proctors a re-test |

#### Why a sandbox is required

Re-running an agent's change means executing untrusted code. The change could delete host files, read API keys from environment variables and send them out, or download payloads. A re-test has to happen in a proctored exam room where nothing comes in or goes out: no network by default, a read-only filesystem with a scratch area only, CPU / memory / time limits, a non-root user and no privileges. This is the `v1.6.1` Docker backend, with gVisor (`runsc`) as the stricter runtime on Linux.

#### How `v1.6.0` fits

`v1.6.0` does not re-run anything yet; it prepares for the re-test:

| `v1.6.0` item | Role in the analogy |
| --- | --- |
| `SandboxBackend` specification and conformance suite | **The exam-room rules**: what is allowed in, how long it lasts, which doors are locked. Every future room (Docker, gVisor, Kubernetes) must pass the same acceptance checklist first |
| W3C trace context and OTel export | **The cameras**: one run, from submission through execution, re-run and judgement, is visible end to end |
| Audit retention purge | Re-runs create more data, so there must be a safe way to clean up that never deletes human labels, the most valuable data the project has |
| FinOps split and import-linter contracts | **Tidying the office**: rules such as "review never re-dispatches a run or touches token leases" become machine-checked in CI |

#### Limits of this approach

1. **A re-run proves that tests pass, not that the code is correct.** Weak tests make a pass meaningless, which is why `v1.6.2` adds mutation testing (to detect weakened tests) and hidden held-out tests.
2. **Not every task can be re-run.** `code_fix` fits well; `data_query` or `deploy_service`, which have external side effects, cannot be re-run casually. This is why `v1.6.1` only allows low-risk tools.
3. **Flaky tests** can make a re-run disagree with the agent's report without any cheating, so retries and interpretation rules are needed.
4. **Re-runs cost compute and time**, so they are paired with FinOps cost controls and sampling.
5. **Monotonic safety still holds.** A failed re-run makes the verdict stricter. A successful re-run only raises the assurance level; it never automatically accepts a run that is under review. Acceptance remains a human decision.
6. **Re-run results stay `monitor_only`.** They must not become a training reward, or agents would learn to fool the verification environment.

**Summary**: `v1.5.x` made the platform a skilled reviewer of evidence; `v1.6.x` makes it a proctor with its own answer key who re-runs the exam. The first catches cheating that leaves traces; the second catches cheating where the evidence itself is fake, the hardest and most dangerous form of reward hacking.

### `v1.6.0` (Current): Runtime Foundations
- **OpenTelemetry Export**: W3C `traceparent` propagation and OTLP span export across API, worker, judge and executor.
- **Audit Retention Purge with Legal Hold**: Dry-run and scheduled purges driven by `audit_retention_days`, excluding open incidents, runs under review, and runs referenced by checkpoints or replay bundles. The FinOps ledger is never auto-purged and follows a separate `finops_ledger_retention_days` setting (default about seven years) that only reports due records.
- **Modularization**: Split `app/services/finops.py` into domain submodules and continue moving endpoints out of `app/api/routes.py` into per-domain routers.
- **`SandboxBackend` Interface**: Abstracts tool execution while keeping deterministic simulation as the default backend.

### `v1.6.1` (Next): Isolated Sandbox Runtime MVP
- **P1 Dashboard RBAC & CSRF**: signed-in Dashboard roles and CSRF tokens on every form; afterwards the Dashboard may apply retention plans and release legal holds (API-only in `v1.6.0`).
- **Hardened Docker Backend**: Read-only root filesystem, no network by default, all capabilities dropped, non-root user, and pid/memory/CPU limits. The OCI runtime is configurable: `runc` for local Docker Desktop verification, `runsc` (gVisor) on Linux hosts without code changes.
- **Narrow Scope**: Only low-risk tools (for example `read_repo`, `run_tests`) and in-sandbox `stdio` MCP servers, behind a disabled-by-default feature flag.
- **Platform-Verified Evidence**: Re-running tests inside the sandbox upgrades outcome evidence to `platform_verified`; dry-run replay can optionally re-execute inside the sandbox.

---

## Long-Term Horizon: `v1.6.2+`

- **Remote MCP Transports**: Real `sse` / `streamable_http` connections behind an egress proxy and allow-lists.
- **Additional Sandbox Backends**: Kubernetes Job backend (with gVisor `RuntimeClass`) and optional Firecracker microVMs for Linux deployments.
- **Stronger Verification**: Hidden held-out tests, mutation testing to detect weakened tests, and resampling of suspicious steps.
- **Closed-Loop Risk Scoring**: Feed confirmed per-agent cheat rates back into pre-execution risk scoring once false-positive rates are stable.
- **Judge Extensions** (deferred from `v1.5.10`–`v1.5.12`): further native adapters (Azure OpenAI, Gemini native, Bedrock / Vertex identity), per-tenant judge selection, in-page encrypted key entry after Dashboard RBAC / CSRF, multi-judge voting, a cross-process (Kafka) judge queue, real 14B / 27B and Gemini measurements once hardware or keys are available, and evidence-based `TT-001` tuning / `special_casing` promotion once enough sampled, non-seed labels exist.
- **Stateful Graph Time-Travel & Live Replay**: Full workflow graph checkpointing with isolated sandbox re-execution.
