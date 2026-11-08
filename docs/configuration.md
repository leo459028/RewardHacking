# Configuration & Environment Variables

> **Configuration Version**: `v1.5.7`
> **Source Model**: `app/core/config.py` (`Settings`) via `pydantic-settings`  
> **Template**: `.env.example`

Application configuration is loaded from environment variables and local `.env` files. In Docker Compose deployments, container network hostnames automatically override host machine localhost defaults.

---

## Environment Variable Reference

| Variable | Default Value | Description |
| --- | --- | --- |
| `APP_NAME` | `reward-hacking-guardrail` | Service name identifier in OpenAPI and log streams |
| `APP_ENV` | `dev` | Target runtime environment (`dev`, `staging`, `prod`) |
| `LOG_LEVEL` | `INFO` | Structlog level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `API_HOST` | `0.0.0.0` | Ingress binding host interface |
| `API_PORT` | `8000` | Ingress HTTP port |
| `POSTGRES_DSN` | `postgresql+asyncpg://app:app@localhost:5432/app` | SQLAlchemy asynchronous PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis URI for run states, circuit breakers, and tenant quotas |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker endpoints for asynchronous event messaging |
| `KAFKA_REQUEST_TOPIC` | `agent.requests` | Topic for queued agent execution requests |
| `KAFKA_DECISION_TOPIC` | `agent.decisions` | Topic for policy evaluation decisions |
| `KAFKA_EXECUTION_TOPIC` | `agent.executions` | Topic for bounded executor results |
| `KAFKA_EVALUATION_TOPIC` | `agent.evaluations` | Topic for evaluator rubrics and verdicts |
| `KAFKA_INCIDENT_TOPIC` | `agent.incidents` | Topic for logged safety incidents |
| `DEFAULT_MAX_TOOL_CALLS` | `8` | Default ceiling on permissible tool calls per agent task |
| `DEFAULT_RISK_THRESHOLD` | `0.65` | Risk score cutoff; scores $\ge 0.65$ trigger approval gates |
| `WORKER_MAX_ATTEMPTS` | `3` | Maximum worker retry attempts before routing to the DLQ |
| `WORKER_RETRY_BACKOFF_SECONDS` | `30` | Exponential retry backoff baseline for worker recovery |
| `APPROVAL_TTL_SECONDS` | `3600` | Expiration countdown (TTL) for pending human reviews |
| `WORKER_COMPLETION_LATENCY_TARGET_SECONDS` | `300` | SLO target for worker execution turnaround |
| `INCIDENT_RESPONSE_TARGET_SECONDS` | `900` | SLO target for incident acknowledgment and resolution |
| `LLM_JUDGE_ENABLED` | `false` | Enables external LLM judge; uses deterministic rubrics if false |
| `AUTH_ENABLED` | `false` | Enables mandatory `X-API-Key` authentication |
| `API_KEYS` | `demo-key:default:admin` | Bootstrap fallback API keys (`key:tenant:role`) |
| `DASHBOARD_AUTH_ENABLED` | `false` | Enables HTTP Basic Authentication for web dashboard |
| `DASHBOARD_USERNAME` | `admin` | Dashboard operator username |
| `DASHBOARD_PASSWORD` | `admin` | Dashboard operator password (MUST be rotated for production) |
| `QUOTA_ENABLED` | `true` | Enables Redis rate limiting and high-risk quotas |
| `TENANT_REQUEST_LIMIT_PER_MINUTE` | `60` | Default sliding-window request quota per tenant |
| `TENANT_HIGH_RISK_LIMIT_PER_HOUR` | `10` | Default hourly ceiling for high-risk requests per tenant |
| `AUDIT_RETENTION_DAYS` | `90` | Operational audit retention lifespan |
| `OPERATIONS_EVIDENCE_MAX_AGE_DAYS` | `30` | Maximum validity age for backup drill evidence |
| `SECRET_ROTATION_MAX_AGE_DAYS` | `90` | Maximum validity age for credential rotation logs |
| `RELEASE_GATE_MAX_QUEUED_AGE_MINUTES` | `15` | Threshold for detecting stale queued tasks during release gate |
| `RELEASE_GATE_FAIL_ON_BOOTSTRAP_KEYS_IN_PROD` | `true` | If true, blocks production release if bootstrap keys exist in `.env` |
| `PRICING_SOURCE_STALE_DAYS` | `30` | Maximum age (in days) since `retrieved_at` before pricing source documents are flagged as stale |
| `FINOPS_BUDGET_ENFORCEMENT_ENABLED` | `true` | Enables active pre-execution and runtime budget enforcement across runs, tenants, workflows, users, projects, and models |
| `FINOPS_BASE_CURRENCY` | `USD` | System-wide base currency (ISO 4217 3-letter uppercase code) used to normalize `CostEvent.base_currency_cost` and budget evaluations |
| `FINOPS_DEFAULT_TAX_RATE` | `0` | Default decimal tax rate (`0` <= rate <= `1`, e.g., `0.05` for 5% VAT) applied when pricing or request metadata does not specify a `tax_rate` |
| `FINOPS_FX_RATES` | `{"USD":"1","CNY":"0.13888889","TWD":"0.03125000","EUR":"1.08000000","JPY":"0.00666667","GBP":"1.27000000"}` | JSON mapping of source currency to base currency multiplier (`1 unit of source currency = fx_rate units of FINOPS_BASE_CURRENCY`) |
| `FINOPS_FX_RATES_AS_OF` | `2026-10-01` | Reference date label recorded in `fx_rate_source` (`env_fx_table:YYYY-MM-DD`) |
| `FINOPS_EXPORT_MAX_ROWS` | `10000` | Maximum rows returned by `GET /v1/cost/tenants/{tenant_id}/export` (`1`–`100000`) |
| `FINOPS_BURN_RATE_WINDOW_HOURS` | `24` | Sliding window in hours (`1`–`720`) used to compute recent tenant/project burn rate |
| `FINOPS_BURN_ALERT_THRESHOLD_HOURS` | `72` | Threshold in hours (`1`–`720`) before projected budget exhaustion triggers `critical_burn` and `finops_burn_rate_warning` |
| `FINOPS_TOKEN_BROKER_ENABLED` | `true` | Enables Dynamic Token Broker pre-execution budget leasing and sibling lease accounting |
| `FINOPS_TOKEN_LEASE_TTL_SECONDS` | `900` | Default expiration TTL in seconds (`60`–`86400`) for active pre-execution token budget leases |
| `USER_REQUEST_LIMIT_PER_MINUTE` | `30` | Default sliding-window request limit per user per minute (`1`–`10000`) |
| `TOOL_CALL_LIMIT_PER_MINUTE` | `60` | Default sliding-window invocation limit per tool per minute (`1`–`10000`) |
| `MCP_SERVER_CALL_LIMIT_PER_MINUTE` | `90` | `v1.5.7` default per-MCP-server call limit per minute when a server has no `quota_per_minute` (`>= 1`) |
| `MCP_SCHEMA_ENFORCEMENT_MODE` | `deny` | Default tool schema violation handling: `deny` (hard deny) or `escalate` (`awaiting_approval`); overridable per tool via `metadata.schema_enforcement` |
| `MCP_INTERNAL_HOST_SUFFIXES` | `.internal.local,.mcp.internal` | Comma-separated hostname suffixes (each must start with `.`) treated as internal network for `sse` / `streamable_http` endpoints; matching uses the parsed hostname, never string prefixes |
| `MCP_TOOL_ARGUMENTS_MAX_DEPTH` | `8` | Maximum nesting depth of a tool argument object (`>= 1`) |
| `MCP_TOOL_ARGUMENTS_MAX_BYTES` | `65536` | Maximum serialized size of a tool argument object in bytes (`>= 1`) |
| `EVALUATION_OUTCOME_MAX_SUBMISSIONS` | `3` | `v1.5.8` maximum outcome evidence submissions per run (`429 outcome_submission_limit_reached`) |
| `EVALUATION_MAX_DIFF_BYTES` | `524288` | Maximum outcome diff size; longer diffs are truncated with an `evidence_quality` finding |
| `EVALUATION_MAX_TEST_REPORT_BYTES` | `1048576` | Maximum JUnit XML size (`413 test_report_too_large`) |
| `EVALUATION_MAX_FINAL_MESSAGE_BYTES` | `32768` | Maximum final message size (truncated with a finding) |
| `EVALUATION_MAX_FILE_CONTENT_BYTES` | `262144` | Maximum `changed_files[].before/after` size; larger content is dropped with a finding |
| `EVALUATION_LINT_SUPPRESSION_THRESHOLD` | `3` | Added lint/type suppressions above this count raise `ES-003` (low; medium above 3x) |
| `EVALUATION_REVIEW_P0_SLA_HOURS` | `4` | P0 reviews open longer than this make `evaluation_review_queue_health` warn |
| `EVALUATION_DETECTOR_MODES` | `{}` | JSON map of detector name to `enforce` / `shadow` / `disabled` (e.g. `{"scope": "shadow"}`); unknown names fail the readiness check. `special_casing` defaults to `shadow` (`v1.5.9`) |
| `EVALUATION_BENCHMARK_MIN_TPR` | `0.90` | `v1.5.9` minimum reward-tampering TPR at `EVALUATION_BENCHMARK_GATE_FPR` for `evaluation_benchmark_gate` (`(0, 1]`) |
| `EVALUATION_BENCHMARK_GATE_FPR` | `0.05` | False-positive rate at which the gate reads TPR (`(0, 1)`) |
| `EVALUATION_BENCHMARK_GATE_ENFORCED` | `false` | When `false` the benchmark gate is advisory (shown, excluded from overall readiness); when `true` a stale benchmark warns and a TPR below the minimum fails in `APP_ENV=prod` |
| `EVALUATION_BENCHMARK_MAX_SAMPLES` | `5000` | Maximum samples per benchmark run (`422 benchmark_too_many_samples`) |
| `EVALUATION_CHEAT_RATE_MIN_SAMPLES` | `20` | Below this many trials a cheat rate is flagged `insufficient_data` |
| `EVALUATION_ATTESTATION_MAX_AGE_HOURS` | `24` | Maximum age of a CI attestation timestamp |
| `EVALUATION_ATTESTATION_MAX_BYTES` | `262144` | Maximum serialized attestation size; larger envelopes are `unsupported_type` |
| `EVALUATION_SIGSTORE_ENABLED` | `false` | Reserved for optional Sigstore / GitHub keyless verification (not implemented through `v1.5.10`; bundles always stay unverified) |
| `EVALUATION_INSPECT_IMPORT_MAX_BYTES` | `5242880` | Maximum Inspect JSON log size (`413 inspect_import_too_large`) |
| `EVALUATION_JUDGE_MODE` | `shadow` | `v1.5.10` judge triage mode: `off` (no assessments), `shadow` (record and show; default order stays `rule`), `triage` (triage order becomes the default). The judge never changes verdicts in any mode |
| `EVALUATION_JUDGE_PROVIDER` | *(empty → `deterministic`)* | `deterministic` or `openai_compatible`. The deprecated `LLM_JUDGE_ENABLED=true` selects `openai_compatible` when this is empty |
| `EVALUATION_JUDGE_BASE_URL` | *(empty)* | OpenAI-compatible base URL (for example `http://localhost:11434/v1`); `/chat/completions` is appended |
| `EVALUATION_JUDGE_MODEL` | *(empty)* | Judge model name; also used for the same-family warning |
| `EVALUATION_JUDGE_API_KEY` | *(empty)* | Bearer token for the judge endpoint. Secret: read from the environment only, never stored, logged or returned |
| `EVALUATION_JUDGE_ALLOW_EXTERNAL` | `false` | Allow judge endpoints outside localhost / `MCP_INTERNAL_HOST_SUFFIXES`; otherwise calls are `blocked_external` and fall back |
| `EVALUATION_JUDGE_TIMEOUT_SECONDS` | `8` | Hard timeout per judge call (`> 0`); timeouts fall back to the deterministic judge |
| `EVALUATION_JUDGE_MAX_INPUT_CHARS` | `24000` | Evidence budget sent to the judge (truncation order: diff, test report, final message, lint) |
| `EVALUATION_JUDGE_MAX_OUTPUT_TOKENS` | `600` | `max_tokens` for judge replies |
| `EVALUATION_JUDGE_DAILY_TOKEN_CAP_PER_TENANT` | `200000` | Judge tokens per tenant per UTC day; above it calls are `budget_exceeded` and fall back (`0` disables LLM calls) |
| `EVALUATION_JUDGE_BENCHMARK_MAX_SAMPLES` | `500` | Upper bound for `max_judge_samples` on LLM-judged benchmark runs |
| `EVALUATION_SAMPLING_ENABLED` | `true` | `v1.5.10` quota-capped sampling of rule-passed outcomes |
| `EVALUATION_SAMPLE_MIN_SCORE` | `0.5` | Threshold slice: `max(shadow-inclusive suspicion_score, risk_score)` at or above this value (`[0, 1]`) |
| `EVALUATION_SAMPLE_DAILY_CAP_PER_TENANT` | `20` | Threshold-slice samples per tenant per UTC day |
| `EVALUATION_SAMPLE_MAX_SHARE` | `0.05` | Threshold-slice samples may not exceed `max(1, floor(share × candidates))` per day (`[0, 1]`) |
| `EVALUATION_SAMPLE_RANDOM_RATE` | `0.02` | Uniform random audit rate (`[0, 1]`) |
| `EVALUATION_SAMPLE_RANDOM_DAILY_CAP_PER_TENANT` | `5` | Random-audit samples per tenant per UTC day |
| `EVALUATION_SAMPLE_SECRET` | *(empty)* | HMAC secret for random draws. Set it in production: without it a per-process secret is used and draws are not reproducible across restarts |
| `EVALUATION_JUDGE_PROFILE` | `default` | `v1.5.11` live-triage judge profile name; unknown names fall back to `deterministic` and fail `evaluation_judge_health` |
| `EVALUATION_JUDGE_PROFILES_FILE` | `config/judge_profiles.yaml` | YAML profile file (git-ignored; copy `config/judge_profiles.example.yaml`). Missing file = no file profiles. Mounted read-only into the containers |
| `EVALUATION_JUDGE_PROFILES` | `{}` | JSON profiles (`{"name": {...}}` or a list); override file profiles by name |
| `EVALUATION_JUDGE_INTERNAL_HOSTS` | `host.docker.internal` | Comma-separated host names a judge endpoint may use without being external (exact match) |
| `EVALUATION_JUDGE_BACKGROUND_CONCURRENCY` | `1` | Background judge tasks per event loop (keep 1 for a single local model) |
| `EVALUATION_JUDGE_PENDING_SWEEP_SECONDS` | `600` | Reviews pending longer than this are re-scheduled by the sweep and warn in `evaluation_judge_health` |
| `TOOL_BREAKER_FAILURE_THRESHOLD` | `3` | Consecutive worker execution failures (`1`–`100`) before a tool's circuit breaker automatically trips to `open` |
| `TOOL_BREAKER_TTL_SECONDS` | `300` | Default TTL in seconds (`30`–`86400`) for an open tool circuit breaker in Redis |

---

## Multi-Currency & Base Currency Configuration Notes (`v1.5.5`)

- **Default Base Currency (`USD`)**: By default, `FINOPS_BASE_CURRENCY=USD` so all 149 official `USD` pricing seed rows convert 1:1 while foreign-currency pricing rows (such as `TWD` or `CNY`) convert into `USD` using `FINOPS_FX_RATES`.
- **Fresh Deployment with `TWD` Base Currency**: For a **brand-new deployment** (empty database) that wishes to settle all ledgers and budgets in New Taiwan Dollars (`TWD`), set `FINOPS_BASE_CURRENCY=TWD` and invert the multipliers in `FINOPS_FX_RATES` so `1 unit of source currency = X TWD`:
  ```env
  FINOPS_BASE_CURRENCY=TWD
  FINOPS_FX_RATES={"TWD":"1","USD":"32.00000000","CNY":"4.44444444","EUR":"34.56000000","JPY":"0.21333333","GBP":"40.64000000"}
  ```
- **Do Not Mutate Base Currency on Historical Databases**: Because historical `CostEvent.base_currency_cost` records are immutable, changing `FINOPS_BASE_CURRENCY` on a database that already contains historical cost events without a data migration is unsupported.

---

## Production Security Recommendations

1. **Disable Bootstrap Keys**: Set `AUTH_ENABLED=true` and provision hashed API keys in the PostgreSQL database via `/v1/tenants/{tenant_id}/api-keys`. Remove fallback `API_KEYS` from production environment files.
2. **Dashboard Security**: Set `DASHBOARD_AUTH_ENABLED=true` and supply high-entropy credentials via enterprise secret managers.
3. **Fail-Closed Thresholds**: Ensure `RELEASE_GATE_FAIL_ON_BOOTSTRAP_KEYS_IN_PROD=true` in `APP_ENV=prod`.

## Judge Profiles (`v1.5.11`)

Copy `config/judge_profiles.example.yaml` to `config/judge_profiles.yaml` and set `EVALUATION_JUDGE_PROFILE`. Fields and semantics: [`docs/evaluation.md`](evaluation.md) §13.

- **LM Studio (local)**: load the model, start the server (Developer tab) and use the port shown under *API Usage*. From the containers use `http://host.docker.internal:<port>/v1` (do not use the `169.254.x.x` address LM Studio may show; link-local addresses are treated as external). For reasoning models (for example Qwen3.5) give reasoning room with `max_output_tokens` ≈ 2000 and a longer `timeout_seconds` (better judgement, ~2.3x slower); set `reasoning_effort: none` only for a fast, weaker profile (`local-small-fast` in the example). Use `execution: background` for slow CPU inference and create zero-cost pricing with **Zero-cost pricing** on `/dashboard/benchmark#judge-profiles`.
- **Ollama / vLLM (larger local models)**: same pattern with their OpenAI-compatible base URL (Ollama: `http://host.docker.internal:11434/v1`).
- **Gemini (cloud)**: base URL `https://generativelanguage.googleapis.com/v1beta/openai`, `api_key_env: GEMINI_API_KEY`, `external: true`, `pricing_provider: google`; set `EVALUATION_JUDGE_ALLOW_EXTERNAL=true` and record tenant consent before evidence is sent.
