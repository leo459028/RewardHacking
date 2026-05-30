# Dashboard 使用說明 v1.0

本專案提供 FastAPI 內建的 server-rendered Dashboard，不需要額外啟動 React/Vite 前端。啟動 API 後可直接開啟：

```text
http://localhost:8000/dashboard
```

目前 Dashboard 版本：`v1.0.0`

## 雙語標題規則

Dashboard 的頁面大標題以英文為主，並在標題下方提供繁體中文標註，方便同時對照 API、資料表與操作語意。表格欄位、狀態值與技術 identifier 仍保留英文或原始值，例如 `queued`、`completed`、`awaiting_approval`、`run_id`、`task_type`、`DLQ` 與 `SLO`。

`/docs` 是 FastAPI 自動產生的 OpenAPI UI，不屬於本 Dashboard 雙語標題調整範圍。

## 頁面入口

| 路由 | 英文標題 / 中文標註 | 用途 |
| --- | --- | --- |
| `/dashboard` | `Guardrail Overview` / 防護總覽 | 總覽 runs、status 分布、平均 risk score、policy flags 與近期 incidents。 |
| `/dashboard/runs` | `Runs` / 執行紀錄 | 查詢 run list，支援 status、task type 與 tenant 篩選，包含 `queued`、`running`、`awaiting_approval`、`failed` 等狀態。 |
| `/dashboard/runs/{run_id}` | `Run {id}` / 執行詳情 | 查看單一 run 的 request、policy、execution、evaluation、event timeline、tool transcript、worker attempts、tool executions、approval state 與 incidents。 |
| `/dashboard/approvals` | `Approval Queue` / 審核佇列 | 查詢 awaiting approval runs，並支援 `approve_once`、`deny`、`request_changes`。 |
| `/dashboard/worker` | `Worker Operations` / Worker 作業狀態 | 查詢 worker attempts、retry failure reason 與 DLQ entries。 |
| `/dashboard/observability` | `Observability Trends` / 可觀測性趨勢 | 查詢 worker、incident、approval、evaluator 與 SLO summary。 |
| `/dashboard/governance` | `Governance` / 治理中心 | 查詢 tenant runs、average risk、quota limited count、tenant 設定、API key 清單與 governance events。 |
| `/dashboard/operations` | `Operations` / 營運中心 | 查詢 release readiness、營運證據與 release gate snapshot，並可新增 backup/restore、secret rotation、safety regression evidence 或建立 gate。 |
| `/dashboard/playground` | `Playground` / 測試沙盒 | 用表單送出測試 AgentRequest，立即產生 `queued`、`awaiting_approval` 或 `denied` run。 |

Dashboard 的互動表單目前對應這些 POST route：

| Method | Path | 用途 |
| --- | --- | --- |
| `POST` | `/dashboard/playground` | 送出測試用 `AgentRequest`。 |
| `POST` | `/dashboard/approvals/{run_id}/decision` | 從 Approval Queue 送出 `approve_once`、`deny` 或 `request_changes`。 |
| `POST` | `/dashboard/operations/evidence` | 從 Operations Dashboard 新增 `backup_restore_drill`、`secret_rotation`、`safety_regression` 或 `retention_review` evidence。 |
| `POST` | `/dashboard/operations/release-gates` | 從 Operations Dashboard 建立 release gate snapshot。 |

## 使用流程

1. 執行 migration：

```bash
alembic upgrade head
```

如果使用 Docker Compose stack，可以在 API container 內執行 migration：

```bash
docker compose exec -T api alembic upgrade head
```

2. 啟動 API：

```bash
uvicorn app.main:app --reload
```

或啟動 Docker Compose：

```bash
docker compose up -d --build
```

3. 開啟 `/dashboard/playground`，送出低風險 request。
4. 回到 `/dashboard/runs`，確認 run 先出現為 `queued`；Docker Compose worker 消費後會更新為 `completed`。
5. 從 `/dashboard/runs/{run_id}` 確認 event timeline、tool transcript、worker attempts、tool executions 與 evaluation summary。
6. 送出需要人工審核的 destructive request，例如開啟 destructive actions 或 PII access。
7. 到 `/dashboard/approvals` 查看 awaiting approval run，測試 `approve_once`、`deny` 或 `request_changes`。
8. 回到 `/dashboard` 查看 queued、running、awaiting approval、failed 統計是否更新。
9. 到 `/dashboard/worker` 確認 retry 與 DLQ 狀態。
10. 到 `/dashboard/observability` 確認 incident、approval、worker、evaluator 與 SLO summary。
11. 到 `/dashboard/governance` 確認 tenant `default` 或指定 tenant 的 quota、API key 清單與治理事件。
12. 到 `/dashboard/operations` 新增 backup/restore、secret rotation 或 safety regression evidence，建立 `v1.0.0` release gate snapshot，確認每個檢查項目的 `pass`、`warn` 或 `fail`。

## 資料來源

Dashboard 不使用假資料；所有資料來自 Postgres audit tables：

- `agent_runs`
- `incidents`
- `worker_attempts`
- `tool_executions`
- `evaluation_results`
- `governance_events`
- `tenants`
- `tenant_api_keys`
- `operations_evidence`
- `release_gate_runs`
- `release_gate_items`

`POST /v1/agent/submit`、`POST /v1/agent/submit-with-details`、worker update、approval decision 與 quota governance event 都會寫入同一份 audit trail，因此 API 與 UI 看到的是同一批資料。

## 目前限制

- Dashboard Basic Auth 可用 `DASHBOARD_AUTH_ENABLED=true` 啟用；預設關閉以維持本機 demo 便利性。
- 目前 execution 使用 `BoundedToolExecutor`，會記錄 tool transcript 與 sandbox boundary policy，但不是完整 OS sandbox。
- Kafka worker、Redis state、retry/DLQ、approval TTL/event、evaluator result 與 tenant governance 已納入；v1.0 的 Operations Dashboard 會把 safety regression evidence 納入 production readiness gate。OAuth/SSO、多 reviewer queue、consumer lag alert、完整 OpenTelemetry、真實 backup/restore 自動化與 secret rotation 自動化仍在後續版本。
