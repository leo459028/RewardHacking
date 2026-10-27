# Configuration & Environment Variables

> **Configuration Version**: `v1.5.3`
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
| `FINOPS_BUDGET_ENFORCEMENT_ENABLED` | `true` | Enables active pre-execution and runtime budget enforcement across runs, tenants, workflows, and users |

---

## Production Security Recommendations

1. **Disable Bootstrap Keys**: Set `AUTH_ENABLED=true` and provision hashed API keys in the PostgreSQL database via `/v1/tenants/{tenant_id}/api-keys`. Remove fallback `API_KEYS` from production environment files.
2. **Dashboard Security**: Set `DASHBOARD_AUTH_ENABLED=true` and supply high-entropy credentials via enterprise secret managers.
3. **Fail-Closed Thresholds**: Ensure `RELEASE_GATE_FAIL_ON_BOOTSTRAP_KEYS_IN_PROD=true` in `APP_ENV=prod`.
