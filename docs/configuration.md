# 設定文件

設定由 `app/core/config.py` 的 `Settings` 管理，使用 `pydantic-settings` 從 `.env` 讀取。範例檔案在 `.env.example`。

`.env.example` 預設給「主機端開發」使用，也就是從 PowerShell 直接跑 `alembic`、`pytest` 或 `uvicorn` 時連到 `localhost`。Docker Compose 會在 `docker-compose.yml` 的 `api` 與 `worker` service 內明確覆蓋成 container network hostnames，避免主機端與容器端共用同一組 hostname 造成 migration 失敗。

## 環境變數

| 變數 | 預設值 | 說明 |
| --- | --- | --- |
| `APP_NAME` | `reward-hacking-guardrail` | FastAPI app 名稱 |
| `APP_ENV` | `dev` | 執行環境 |
| `LOG_LEVEL` | `INFO` | logging level |
| `API_HOST` | `0.0.0.0` | API listen host |
| `API_PORT` | `8000` | API listen port |
| `POSTGRES_DSN` | `postgresql+asyncpg://app:app@localhost:5432/app` | SQLAlchemy async Postgres DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL，用於 run state、idempotency、circuit breaker |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka bootstrap servers，用於 API publish 與 worker consume |
| `KAFKA_REQUEST_TOPIC` | `agent.requests` | request topic |
| `KAFKA_DECISION_TOPIC` | `agent.decisions` | policy decision topic |
| `KAFKA_EXECUTION_TOPIC` | `agent.executions` | execution result topic |
| `KAFKA_EVALUATION_TOPIC` | `agent.evaluations` | evaluation result topic |
| `KAFKA_INCIDENT_TOPIC` | `agent.incidents` | incident topic |
| `DEFAULT_MAX_TOOL_CALLS` | `8` | 預設 tool call 預算 |
| `DEFAULT_RISK_THRESHOLD` | `0.65` | policy engine 風險門檻 |
| `WORKER_MAX_ATTEMPTS` | `3` | worker 處理同一 run 的最大嘗試次數 |
| `WORKER_RETRY_BACKOFF_SECONDS` | `30` | worker transient failure 下一次 retry 的預設等待秒數 |
| `APPROVAL_TTL_SECONDS` | `3600` | approval pending 的預設有效秒數 |
| `WORKER_COMPLETION_LATENCY_TARGET_SECONDS` | `300` | observability summary 的 worker 完成時間目標 |
| `INCIDENT_RESPONSE_TARGET_SECONDS` | `900` | observability summary 的 incident response 目標 |
| `LLM_JUDGE_ENABLED` | `false` | 是否啟用外部 LLM judge；目前預設 deterministic fake judge signal |
| `AUTH_ENABLED` | `false` | 是否啟用 API key auth |
| `API_KEYS` | `demo-key:default` | API key 與 tenant 對應，格式為 `key:tenant_id`，多組以逗號分隔 |
| `DASHBOARD_AUTH_ENABLED` | `false` | 是否啟用 Dashboard Basic Auth |
| `DASHBOARD_USERNAME` | `admin` | Dashboard Basic Auth 使用者 |
| `DASHBOARD_PASSWORD` | `admin` | Dashboard Basic Auth 密碼，本機範例值不可用於正式環境 |
| `QUOTA_ENABLED` | `true` | 是否啟用 tenant quota 檢查 |
| `TENANT_REQUEST_LIMIT_PER_MINUTE` | `60` | 每個 tenant 每分鐘 request 上限 |
| `TENANT_HIGH_RISK_LIMIT_PER_HOUR` | `10` | 每個 tenant 每小時 high-risk request 上限 |

## Docker Compose 設定

`.env.example` 使用 host 端可連線的 hostnames：

```env
POSTGRES_DSN=postgresql+asyncpg://app:app@localhost:5432/app
REDIS_URL=redis://localhost:6379/0
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
```

Docker Compose 內的 `api` 與 `worker` 會覆蓋為 container network：

```env
POSTGRES_DSN=postgresql+asyncpg://app:app@postgres:5432/app
REDIS_URL=redis://redis:6379/0
KAFKA_BOOTSTRAP_SERVERS=kafka:9092
```

注意：目前 `docker-compose.yml` 的 Kafka advertised listener 是 `PLAINTEXT://kafka:9092`，適合 container 之間互連。若要從 host 直接連 Kafka，需要額外調整 advertised listeners。

## Readiness

`GET /v1/readyz` 目前檢查 Postgres connection，因為 Postgres 是 submit、audit trail 與 Dashboard 的必要依賴。

Redis 與 Kafka 已在 `v0.3.0` request path 中使用，但 `/v1/readyz` 目前仍只檢查 Postgres。後續版本會補 worker/Kafka/Redis readiness 與更清楚的 degraded mode。

## Policy Threshold

`DEFAULT_RISK_THRESHOLD=0.65` 會傳入 `PolicyEngine(risk_threshold=...)`。

目前決策邏輯：

- `risk_score < threshold` 且沒有 flags：`allow`
- 有任何 flags：`escalate`
- `risk_score >= threshold` 且 `< 0.85`：`escalate`
- `risk_score >= 0.85`：`deny`

API response status 會使用：

- `allow` 且 evaluator pass：`completed`
- `allow` 但 evaluator review：`queued`
- policy `escalate`：`escalated`
- policy `deny`：`denied`

## Auth / Dashboard Auth

`AUTH_ENABLED=false` 時，API 保持本機 demo 友善模式。`AUTH_ENABLED=true` 時，submit、runs、incidents、approvals、evaluations 與 governance API 需要 `X-API-Key`；health、ready、metrics、部分 read-only subresource 與 summary endpoint 仍維持 demo 可讀，正式環境應在反向代理或後續版本補完整 auth coverage。`API_KEYS` 會把 key 對應到 tenant，例如：

```env
AUTH_ENABLED=true
API_KEYS=demo-key:default,tenant-a-key:tenant-a
```

Dashboard 可用 `DASHBOARD_AUTH_ENABLED=true` 開啟 Basic Auth。這是 v0.6 MVP，用來避免 demo Dashboard 直接裸露；正式環境仍建議接入反向代理、SSO 或更完整的權限控管。

## Tenant Quota

`QUOTA_ENABLED=true` 時，submit API 會透過 Redis 計數：

- request quota 超過 `TENANT_REQUEST_LIMIT_PER_MINUTE` 時，API 回 `429 tenant_request_quota_exceeded`，並寫入 `governance_events`。
- high-risk quota 超過 `TENANT_HIGH_RISK_LIMIT_PER_HOUR` 時，run 會 fail closed 進入 `awaiting_approval`，並加上 `tenant_high_risk_quota_exceeded` policy flag。

## Secret 管理

`.env.example` 只適合本機開發，不應直接用於正式環境。正式環境建議：

- 不把 `.env` commit 到版本控制。
- 使用 secret manager 或部署平台的 encrypted environment variables。
- Postgres、Redis、Kafka 密碼使用強密碼並定期輪替。
- 將不同環境的 DSN 與 topic namespace 分開。

## 設定驗證建議

正式化時建議新增 startup validation：

- 確認 `DEFAULT_RISK_THRESHOLD` 在 `0.0` 到 `1.0`。
- 確認 topic names 非空。
- 在 `APP_ENV=prod` 時禁止使用預設密碼。
