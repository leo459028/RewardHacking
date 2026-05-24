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
