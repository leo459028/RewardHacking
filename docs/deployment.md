# 部署文件 v1.5

本專案提供 Dockerfile 與 Docker Compose，適合本機開發、demo、CI smoke test。`v1.5.1` 已包含 API、worker、tenant-aware audit、DB-backed API key、RBAC-lite、Dashboard Basic Auth、Redis tenant quota、Governance Dashboard、Control Plane execution plan、Trace / Replay Readiness、Checkpoint / Resume / Replay MVP、Agent FinOps Core / Pricing Import Admin、Production Readiness Gate、safety regression evidence、敏感 read API auth coverage、HTTP metrics、incident counter 與 Classical Chinese Prompt Guard。正式環境部署前仍需補上完整 OAuth/SSO、secret 管理、完整 OpenTelemetry、真實 sandbox、真實 MCP Gateway、完整 LangGraph runtime、完整 worker lifecycle 與安全控制。

目前部署版本：`v1.5.1`

V1.5.1 在部署面不新增外部服務；FinOps Core 使用既有 FastAPI、Postgres、Kafka worker queue、Dashboard 與 metrics pipeline。部署時需套用 Alembic head `202606010004`，除了 V1.5 的成本帳本資料表，也會擴充 FinOps 欄位並建立 `pricing_import_batches`、`pricing_change_logs` 與 `pricing_source_documents`。

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

Compose 會在 `api` 與 `worker` service 內覆蓋 Postgres、Redis、Kafka 的 container network hostnames；因此 `.env.example` 可以保留 host 端 `localhost` 設定，方便直接執行本機 migration 或 API。`AUTH_ENABLED`、`API_KEYS`、`DASHBOARD_AUTH_ENABLED`、`QUOTA_ENABLED`、tenant quota 與 operations gate 設定也由同一份環境設定管理。v0.7 的 tenant / DB-managed API key、v1.0 的 operations evidence / release gate / safety regression evidence、v1.1 的 Prompt Guard summary，以及 v1.2 的 Control Plane execution plan 都會保存在 Postgres。

3. 檢查服務：

```bash
docker compose ps
curl http://localhost:8000/v1/healthz
curl http://localhost:8000/v1/readyz
docker compose logs worker
```

4. 開啟 Dashboard：

```text
http://localhost:8000/dashboard
```

5. 查看 logs：

```bash
docker compose logs -f api
docker compose logs -f worker
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
| `redis` | `redis:7` | run state、idempotency、circuit breaker、tenant quota |
| `kafka` | `bitnamilegacy/kafka:3.7.0` | event bus，本機 demo 沿用 Bitnami KRaft 設定 |
| `kafka-init` | `bitnamilegacy/kafka:3.7.0` | 建立 Kafka topics |
| `api` | local Dockerfile | FastAPI app、JSON API、Dashboard |
| `worker` | local Dockerfile | Kafka worker，等待 API service 啟動後消費 `agent.requests` 並寫回 audit trail |

## Dockerfile 行為

Dockerfile 使用 `python:3.12-slim`：

- 安裝 `build-essential`、`curl`。
- 安裝 `requirements.txt`。
- 複製 `app`、`migrations`、`tests`、`README.md`。
- 啟動時先執行 `alembic upgrade head`。
- 再以 `uvicorn app.main:app --host 0.0.0.0 --port 8000` 啟動。

## Docker Compose 常見陷阱

- `.env` 必須存在；本機 demo 建議從 `.env.example` 複製後再啟動 Compose。
- `.env.example` 預設使用 host 端 `localhost`；Docker container 內連線值由 `docker-compose.yml` 的 service environment 覆蓋，不要把這兩種 hostname 混成同一份設定。
- Kafka 目前使用 `bitnamilegacy/kafka:3.7.0`，不要改回不存在或未驗證的 `bitnami/kafka:3.7` tag。
- `kafka-init` 使用 `/opt/bitnami/kafka/bin/kafka-topics.sh` 建立 topics，不要假設 `kafka-topics.sh` 一定在 PATH。
- Migration 應維持單一 owner；目前由 API 啟動前執行 `alembic upgrade head`，worker 不應同時跑 migration，避免 Alembic version table 競態。

## Production Checklist

正式部署前至少補齊：

- 使用 managed Postgres，並設定 backup/restore drill。
- 將 Redis/Kafka 改成 managed service 或高可用叢集，並納入 readiness。
- 設定 TLS、完整 OAuth/SSO 或反向代理 auth、network policy。
- Dashboard Basic Auth 與 RBAC-lite 可作為 MVP 防護；正式環境建議接入 SSO/RBAC 與更細的權限控管。
- 在正式環境拆分 API 與 worker deployment，分開設定 replica、資源限制與 autoscaling。
- 建立 Prometheus scrape config 與 alert rules。
- 建立 log aggregation。
- 設定 resource requests/limits 與 autoscaling。
- 使用 secret manager。
- 設定 CI/CD gate：test、lint、migration check、container scan。
- 部署前從 `/dashboard/operations` 或 `/v1/operations/release-gates` 建立 release gate snapshot，並保存 backup/restore、secret rotation 與 safety regression evidence；`APP_ENV=prod` 時缺少 safety regression evidence 會 fail closed。

## Kubernetes 建議

如果部署到 Kubernetes，建議資源切分：

- `Deployment/api`：FastAPI replicas。
- `Job/migration`：部署前 migration。
- `Service/api`：對內或對外服務入口。
- `ConfigMap`：非敏感設定。
- `Secret`：DSN、密碼、API keys。
- `ServiceMonitor`：Prometheus metrics。
- `Deployment/worker`：在 migration 完成後執行 `python -m app.workers.consumer`，消費 Kafka request topic。

## Rollback 建議

- API image rollback：保留最近可用 image tag。
- DB migration rollback：每個 migration 都要有 downgrade 或 forward-fix playbook。
- Policy rule rollback：policy thresholds 與 rules 應可透過 config 或 feature flag 回退。
- Kafka schema rollback：event payload 變更要保持 backward compatible。
## V1.5.1 Deployment 補充

V1.5.1 需要套用 Alembic head `202606010004_expand_v151_finops_core`，並安裝 `PyYAML>=6.0.2` 以支援官方價格 seed YAML 匯入。部署後可用 `/dashboard/admin/finops/pricing/import` 執行 dry-run，再 apply `app/seeds/model_pricing_seed.yaml`。
