# 部署文件 v0.2

本專案提供 Dockerfile 與 Docker Compose，適合本機開發、demo、CI smoke test。正式環境部署前仍需補上 auth、secrets、observability、worker lifecycle 與安全控制。

目前部署版本：`v0.2.0`

## 本機 Docker Compose

1. 建立 `.env`：

```bash
cp .env.example .env
```

2. 啟動全部服務：

```bash
docker compose up -d --build
```

Dockerfile 會在 API 啟動前執行：

```bash
alembic upgrade head
```

3. 檢查服務：

```bash
docker compose ps
curl http://localhost:8000/v1/healthz
curl http://localhost:8000/v1/readyz
```

4. 開啟 Dashboard：

```text
http://localhost:8000/dashboard
```

5. 查看 logs：

```bash
docker compose logs -f api
```

6. 停止服務：

```bash
docker compose down
```

## 只啟動基礎設施

如果想在 host 上直接跑 API：

```bash
docker compose up -d postgres redis kafka kafka-init
alembic upgrade head
uvicorn app.main:app --reload
```

這種模式下 `.env` 需要使用 `localhost` DSN，詳見 [設定文件](configuration.md)。

## Container 組成

| Service | Image/Build | 說明 |
| --- | --- | --- |
| `postgres` | `postgres:16` | 預設 DB `app`，使用者 `app` |
| `redis` | `redis:7` | Phase 4 state/cache/circuit breaker 預留 |
| `kafka` | `bitnami/kafka:3.7` | Phase 3 event bus 預留 |
| `kafka-init` | `bitnami/kafka:3.7` | 建立 Kafka topics |
| `api` | local Dockerfile | FastAPI app、JSON API、Dashboard |

## Dockerfile 行為

Dockerfile 使用 `python:3.12-slim`：

- 安裝 `build-essential`、`curl`。
- 安裝 `requirements.txt`。
- 複製 `app`、`migrations`、`tests`、`README.md`。
- 啟動時先執行 `alembic upgrade head`。
- 再以 `uvicorn app.main:app --host 0.0.0.0 --port 8000` 啟動。

## Production Checklist

正式部署前至少補齊：

- 使用 managed Postgres，並設定 backup/restore drill。
- 將 Redis/Kafka 改成 managed service 或高可用叢集，並等 Phase 3/4 接入後納入 readiness。
- 設定 TLS、auth、network policy。
- Dashboard 加入登入與權限控管。
- 拆分 API 與 worker deployment。
- 建立 Prometheus scrape config 與 alert rules。
- 建立 log aggregation。
- 設定 resource requests/limits 與 autoscaling。
- 使用 secret manager。
- 設定 CI/CD gate：test、lint、migration check、container scan。

## Kubernetes 建議

如果部署到 Kubernetes，建議資源切分：

- `Deployment/api`：FastAPI replicas。
- `Job/migration`：部署前 migration。
- `Service/api`：對內或對外服務入口。
- `ConfigMap`：非敏感設定。
- `Secret`：DSN、密碼、API keys。
- `ServiceMonitor`：Prometheus metrics。
- `Deployment/worker`：Phase 3 導入 Kafka worker 後再新增。

## Rollback 建議

- API image rollback：保留最近可用 image tag。
- DB migration rollback：每個 migration 都要有 downgrade 或 forward-fix playbook。
- Policy rule rollback：policy thresholds 與 rules 應可透過 config 或 feature flag 回退。
- Kafka schema rollback：Phase 3 導入 event payload 後保持 backward compatible。
