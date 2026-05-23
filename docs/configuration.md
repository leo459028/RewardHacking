# 設定文件

設定由 `app/core/config.py` 的 `Settings` 管理，使用 `pydantic-settings` 從 `.env` 讀取。範例檔案在 `.env.example`。

## 環境變數

| 變數 | 預設值 | 說明 |
| --- | --- | --- |
| `APP_NAME` | `reward-hacking-guardrail` | FastAPI app 名稱 |
| `APP_ENV` | `dev` | 執行環境，例如 `dev`、`staging`、`prod` |
| `LOG_LEVEL` | `INFO` | logging level |
| `API_HOST` | `0.0.0.0` | API listen host |
| `API_PORT` | `8000` | API listen port |
| `POSTGRES_DSN` | `postgresql+asyncpg://app:app@localhost:5432/app` | SQLAlchemy async Postgres DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka bootstrap servers |
| `KAFKA_REQUEST_TOPIC` | `agent.requests` | request topic |
| `KAFKA_DECISION_TOPIC` | `agent.decisions` | policy decision topic |
| `KAFKA_EXECUTION_TOPIC` | `agent.executions` | execution result topic |
| `KAFKA_EVALUATION_TOPIC` | `agent.evaluations` | evaluation result topic |
| `KAFKA_INCIDENT_TOPIC` | `agent.incidents` | incident topic |
| `DEFAULT_MAX_TOOL_CALLS` | `8` | 預設 tool call 預算 |
| `DEFAULT_RISK_THRESHOLD` | `0.65` | policy engine 風險門檻 |

## Docker Compose 設定

`.env.example` 使用 container network 內的 hostnames：

```env
POSTGRES_DSN=postgresql+asyncpg://app:app@postgres:5432/app
REDIS_URL=redis://redis:6379/0
KAFKA_BOOTSTRAP_SERVERS=kafka:9092
```

如果在本機直接執行 `uvicorn app.main:app --reload`，通常要改成：

```env
POSTGRES_DSN=postgresql+asyncpg://app:app@localhost:5432/app
REDIS_URL=redis://localhost:6379/0
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
```

注意：目前 `docker-compose.yml` 的 Kafka advertised listener 是 `PLAINTEXT://kafka:9092`，適合 container 之間互連。若要從 host 直接連 Kafka，需要額外調整 advertised listeners。

## Policy Threshold

`DEFAULT_RISK_THRESHOLD=0.65` 會傳入 `PolicyEngine(risk_threshold=...)`。

目前決策邏輯：

- `risk_score < threshold` 且沒有 flags：`allow`
- 有任何 flags：`escalate`
- `risk_score >= threshold` 且 `< 0.85`：`escalate`
- `risk_score >= 0.85`：`deny`

調低 threshold 會增加人工審核量；調高 threshold 會降低 false positive，但可能放過更多高風險任務。

## Secret 管理

目前範例為本機開發設定，不應直接用於正式環境。正式環境建議：

- 不把 `.env` commit 到版本控制。
- 使用 secret manager 或部署平台的 encrypted environment variables。
- Postgres、Redis、Kafka 密碼使用強密碼並定期輪替。
- 將不同環境的 DSN 與 topic namespace 分開。

## 設定驗證建議

正式化時建議新增 startup validation：

- 確認 `DEFAULT_RISK_THRESHOLD` 在 `0.0` 到 `1.0`。
- 確認 topic names 非空。
- 對 Postgres、Redis、Kafka 做 readiness check。
- 在 `APP_ENV=prod` 時禁止使用預設密碼。
