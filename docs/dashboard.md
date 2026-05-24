# Dashboard 使用說明 v0.5

本專案提供 FastAPI 內建的 server-rendered dashboard，不需要額外啟動 React/Vite 前端。啟動 API 後可直接開啟：

目前 Dashboard 版本：`v0.5.0`

```text
http://localhost:8000/dashboard
```

## 頁面

| 路徑 | 用途 |
| --- | --- |
| `/dashboard` | 總覽 runs、status 分布、平均 risk score、近期 incidents |
| `/dashboard/runs` | 查詢 run list，支援 status 與 task type 篩選，包含 queued/running/awaiting_approval/failed |
| `/dashboard/runs/{run_id}` | 查看單一 run 的 request、policy、execution、evaluation、event timeline、tool transcript、approval state、incident |
| `/dashboard/approvals` | 查詢 awaiting approval runs，並支援 approve once、deny、request changes |
| `/dashboard/worker` | 查詢 worker attempts、retry failure reason 與 DLQ |
| `/dashboard/observability` | 查詢 worker、incident、approval、evaluator 與 SLO summary |
| `/dashboard/playground` | 用表單送出測試 AgentRequest，立即產生 queued、awaiting approval 或 denied run |

## 使用流程

1. 套用 migration：

```bash
alembic upgrade head
```

2. 啟動 API：

```bash
uvicorn app.main:app --reload
```

或使用 Docker Compose：

```bash
docker compose up -d --build
```

3. 開啟 `/dashboard/playground`，送出低風險 request。
4. 回到 `/dashboard/runs`，確認 run 先出現為 `queued`；Docker Compose worker 消費後會更新為 `completed`。
5. 從 `/dashboard/runs/{run_id}` 確認 event timeline、tool transcript、worker attempts、tool executions 與 evaluation summary。
6. 再送出一筆高風險或 destructive request，例如包含 `Ignore previous instructions`、允許 external network、PII 或 destructive actions。
7. 到 `/dashboard/approvals` 查看 awaiting approval run，測試 approve once、deny 或 request changes。
8. 回到 `/dashboard` 查看 queued/running/awaiting approval/failed 統計是否更新。
9. 到 `/dashboard/worker` 確認 retry/DLQ 狀態。
10. 到 `/dashboard/observability` 確認 incident、approval、worker、evaluator 與 SLO summary。

## 資料來源

Dashboard 不使用假資料；所有資料來自 Postgres audit tables：

- `agent_runs`
- `incidents`

`POST /v1/agent/submit`、`POST /v1/agent/submit-with-details`、worker update 與 approval decision 都會寫入同一份 audit trail，因此 API 與 UI 看到的是同一批資料。

## 注意事項

- 目前 dashboard 沒有登入權限控管，不應直接暴露在公開網路。
- 目前 execution 是 `BoundedToolExecutor`，會記錄 tool transcript 與 sandbox boundary policy，但不是正式 OS sandbox。
- Kafka worker、Redis state、retry/DLQ、approval TTL/event 與 evaluator result 已納入 `v0.5.0`；auth、多 reviewer queue、consumer lag alert 與 OpenTelemetry 仍在後續版本。
