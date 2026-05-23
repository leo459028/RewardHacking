# Dashboard 使用說明 v0.2

本專案提供 FastAPI 內建的 server-rendered dashboard，不需要額外啟動 React/Vite 前端。啟動 API 後可直接開啟：

目前 Dashboard 版本：`v0.2.0`

```text
http://localhost:8000/dashboard
```

## 頁面

| 路徑 | 用途 |
| --- | --- |
| `/dashboard` | 總覽 runs、status 分布、平均 risk score、近期 incidents |
| `/dashboard/runs` | 查詢 run list，支援 status 與 task type 篩選 |
| `/dashboard/runs/{run_id}` | 查看單一 run 的 request、policy、execution、evaluation、incident |
| `/dashboard/playground` | 用表單送出測試 AgentRequest，立即產生 run 與 incident |

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
4. 再送出一筆高風險 request，例如包含 `Ignore previous instructions`、允許 external network、PII 或 destructive actions。
5. 回到 `/dashboard` 查看統計是否更新。
6. 從 `/dashboard/runs` 點進 run detail，確認 audit trail。

## 資料來源

Dashboard 不使用假資料；所有資料來自 Postgres audit tables：

- `agent_runs`
- `incidents`

`POST /v1/agent/submit` 與 `POST /v1/agent/submit-with-details` 都會寫入同一份 audit trail，因此 API 與 UI 看到的是同一批資料。

## 注意事項

- 目前 dashboard 沒有登入權限控管，不應直接暴露在公開網路。
- 目前 execution 仍是 `DummyToolExecutor`，不是正式 tool sandbox。
- Kafka worker、Redis state、human approval workflow 尚未納入本段。
