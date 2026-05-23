# 部署文件

本專案提供 Dockerfile 與 Docker Compose，適合本機開發、demo、CI smoke test。正式環境部署前仍需補上 migration、secrets、observability、worker lifecycle 與安全控制。

## 本機 Docker Compose

1. 建立 `.env`：

```bash
cp .env.example .env
```

2. 啟動全部服務：

```bash
docker compose up -d --build
```

3. 檢查服務：

```bash
docker compose ps
curl http://localhost:8000/v1/healthz
```

4. 查看 logs：

```bash
docker compose logs -f api
```

5. 停止服務：

```bash
docker compose down
```

## 只啟動基礎設施

如果想在 host 上直接跑 API：

```bash
docker compose up -d postgres redis kafka kafka-init
uvicorn app.main:app --reload
```

這種模式下 `.env` 需要使用 `localhost` DSN，詳見 [設定文件](configuration.md)。

## Container 組成

| Service | Image/Build | 說明 |
| --- | --- | --- |
| `postgres` | `postgres:16` | 預設 DB `app`，使用者 `app` |
| `redis` | `redis:7` | state/cache/circuit breaker 預留 |
| `kafka` | `bitnami/kafka:3.7` | KRaft single-node Kafka |
| `kafka-init` | `bitnami/kafka:3.7` | 建立 topics |
| `api` | local Dockerfile | FastAPI app |

## Dockerfile 行為

Dockerfile 使用 `python:3.12-slim`：

- 安裝 `build-essential`、`curl`。
- 安裝 `requirements.txt`。
- 複製 `app`、`tests`、`README.md`。
- 以 `uvicorn app.main:app --host 0.0.0.0 --port 8000` 啟動。

## Production Checklist

正式部署前至少補齊：

- 使用 Alembic migration，不在 runtime 用 `create_all()`。
- 將 Postgres、Redis、Kafka 改成 managed service 或高可用叢集。
- 設定 TLS、auth、network policy。
- 拆分 API 與 worker deployment。
- 加入 readiness/liveness probes。
- 建立 Prometheus scrape config 與 alert rules。
- 建立 log aggregation。
- 設定 backup、restore drill、retention。
- 設定 resource requests/limits 與 autoscaling。
- 使用 secret manager。
- 設定 CI/CD gate：test、lint、migration check、container scan。

## Kubernetes 建議

如果部署到 Kubernetes，建議資源切分：

- `Deployment/api`：FastAPI replicas。
- `Deployment/worker`：Kafka consumer replicas。
- `Service/api`：對內或對外服務入口。
- `ConfigMap`：非敏感設定。
- `Secret`：DSN、密碼、API keys。
- `Job/migration`：部署前 migration。
- `ServiceMonitor`：Prometheus metrics。
- `HorizontalPodAutoscaler`：依 CPU、latency、Kafka lag 擴縮。

## Rollback 建議

- API image rollback：保留最近可用 image tag。
- DB migration rollback：每個 migration 都要有 downgrade 或 forward-fix playbook。
- Policy rule rollback：policy thresholds 與 rules 應可透過 config 或 feature flag 回退。
- Kafka schema rollback：event payload 加欄位時保持 backward compatible。
