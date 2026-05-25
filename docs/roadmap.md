# Roadmap v0.7

本 roadmap 將專案從 prototype 推進到可逐步承擔 guardrail 職責的服務。`v0.3.0` 起不再把 Kafka、Redis、Tool Executor、Approval 視為互不相干的線性 phase，而是先建立一條可展示、可查詢、可審計的產品閉環，再把完整 hardening 與 production governance 放到後續版本。

## 版本節奏

| 版本 | 狀態 | 主軸 | 說明 |
| --- | --- | --- | --- |
| `v0.2.0` | 已完成 | 同步 API + Audit + Dashboard prototype | 建立 request、policy、demo execution、evaluation、Postgres audit trail 與 server-rendered Dashboard。 |
| `v0.3.0` | 已完成 | Guarded Async Execution MVP | 合併 Kafka、Redis、Approval、Executor Interface 的最小閉環。 |
| `v0.4.0` | 已完成 | Execution Hardening | 補 worker retry/DLQ、approval TTL/event、tool execution audit、resource/cost limit 與 sandbox policy。 |
| `v0.5.0` | 已完成 | Evaluator + Observability | 補 task-specific evaluator、deterministic judge signal、worker metrics、SLO 與 Dashboard 趨勢。 |
| `v0.6.0` | 已完成 | Tenant Governance MVP | 補 API key auth、Dashboard Basic Auth、tenant-aware audit、Redis quota 與 Governance Dashboard。 |
| `v0.7.0` | 目前版本 | Tenant Governance Center | 補 DB-backed tenant 設定、API key hash 儲存、RBAC-lite、tenant quota override 與 Governance Dashboard 查詢區塊。 |
| `v0.8+` | 規劃中 | Production Operations | 補 backup/restore、retention、secret rotation、release gate、OpenTelemetry 與 Kafka lag alert。 |

## Milestone：v0.2.0 / Phase 0-2 + Dashboard

本段是已落地的產品化里程碑，目標是讓專案從 API skeleton 變成可展示、可查詢、可審計的 guardrail prototype。

完成項目：

- 文件基線：補 Dashboard 使用說明，確認 docs 可用 UTF-8 解碼。
- API 穩定化：新增 `/v1/readyz`、run 查詢、incident 查詢、dashboard summary API。
- Error handling：統一 request validation error response。
- Persistence：導入 Alembic，建立 `agent_runs` 與 `incidents` 初始 migration。
- Audit trail：submit 後保存 request、policy、execution output、evaluation summary 與 incident。
- Dashboard UI：提供 `/dashboard`、`/dashboard/runs`、`/dashboard/runs/{run_id}`、`/dashboard/playground`。
- Tests：補 sanitizer、risk scoring、policy、evaluator、API、dashboard HTML tests。

本段不包含：

- Kafka worker / async execution。
- Redis run state / circuit breaker。
- 真實 tool executor / sandbox。
- Human approval workflow。

## Milestone：v0.3.0 / Guarded Async Execution MVP

目標：讓低風險任務能先進入 queue，由 worker 非同步處理；高風險或 destructive 任務進入 approval queue；所有重要狀態仍寫回 Postgres audit trail，並可從 Dashboard 查詢。

已完成項目：

- API submit 後會先建立 run，低風險任務回傳 `queued`，不再同步執行工具。
- `POST /v1/agent/submit` 與 `POST /v1/agent/submit-with-details` 會 publish `agent.requests` event，供 worker 消費。
- Worker 可依 run id 重新讀取 request、重跑 policy、執行 bounded executor、寫回 execution/evaluation/incident。
- Redis state store 接入最小閉環：`run:{run_id}:state`、`idempotency:{event_id}`、`breaker:{tool_name}`。
- Policy 會檢查已開啟的 tool circuit breaker；命中時 fail closed，進入 `awaiting_approval`。
- Executor 從 `DummyToolExecutor` 演進為 bounded executor interface，記錄 tool transcript 與 artifact refs，但仍不是完整 OS sandbox。
- Destructive 或需審核任務會進入 `awaiting_approval`，不執行工具。
- 新增 approval API：`GET /v1/approvals`、`POST /v1/approvals/{run_id}/decision`。
- Dashboard 新增 `/dashboard/approvals`，run detail 顯示 event timeline、tool transcript、approval state。
- Docker Compose 新增 `worker` service，與 API 共用映像檔並消費 Kafka request topic。

完成條件：

- 低風險 request：API 回 `queued`，worker 完成後 run 變 `completed`，Dashboard 可查 timeline。
- 高風險或 destructive request：進入 `denied` 或 `awaiting_approval`，incident 與 approval state 可查。
- breaker 開啟的 tool：policy fail closed，Dashboard 顯示對應 policy flag。
- 重複 worker event：idempotency key 避免重複執行。

目前限制：

- `v0.3.0` 不是完整 production sandbox；bounded executor 只記錄受控 transcript，不呼叫真實外部工具。
- Approval workflow 是最小版，支援 approve once、deny、request changes；完整 reviewer queue、TTL 與稽核報表放到 `v0.4.0`。
- Kafka publish/consume 已接入，但 retry/backoff、DLQ、consumer lag alert 放到 `v0.4.0`。
- Redis 不作為唯一 audit store；重要決策仍以 Postgres 為準。

## Milestone：v0.4.0 / Execution Hardening

目標：把 `v0.3.0` 的最小閉環強化成更可靠的執行層。

已完成項目：

- Worker retry/backoff。
- DLQ audit 查詢，保留 Kafka dead-letter topic 作為後續部署延伸。
- Worker failure reason 與 retry attempt 寫入 audit trail。
- Approval workflow：reviewer、decision reason、TTL、request changes、審核事件查詢。
- Tool transcript 獨立查詢與 artifact refs 查詢，落到 `tool_executions`。
- Tool ACL 依 task type 管理允許工具。
- 加入 timeout、resource limit、cost limit 的 audit policy。
- Sandbox boundary policy：network、filesystem、process、secret 邊界以 fail-closed audit contract 記錄。

完成條件：

- 失敗任務不會資料遺失或無限重試。
- Destructive action 沒有人審不能執行。
- 所有 tool action 可追蹤。

## Milestone：v0.5.0 / Evaluator + Observability

目標：讓評估訊號與營運觀測更接近真實安全需求。

已完成項目：

- 建立 task-specific rubrics。
- 對 code tasks 檢查 diff、test result、lint result。
- 對 data_query 檢查來源與 query lineage。
- 導入 deterministic fake judge signal，保留 LLM-as-judge 插拔點，且不作為唯一 hard deny。
- 建立 labeled evaluation dataset 的 repo fixture 方向，完整資料集管理保留後續擴充。
- 追蹤 evaluator pass/review 作為 precision / recall 報表前置資料。
- Worker metrics：job duration、completion latency、failure count。
- Dashboard 趨勢：incident、approval、worker DLQ、evaluator verdict summary。
- SLO：API latency、worker completion latency、incident response。

完成條件：

- evaluator false positive/false negative 可量化。
- worker 與 incident 可被發現、定位與回顧。

## Milestone：v0.6.0 / Tenant Governance MVP

目標：先補上最小可展示的多租戶治理閉環，讓 request 可以歸屬 tenant、受 quota 限制，並能從 Dashboard 查治理事件。

已完成項目：

- `AgentRequest`、`AgentRun` 與查詢 API 加入 `tenant_id`。
- API key auth：`AUTH_ENABLED=true` 時以 `X-API-Key` 對應 tenant。
- Dashboard Basic Auth：`DASHBOARD_AUTH_ENABLED=true` 時保護 Dashboard。
- Tenant request quota：每分鐘 request 超限回 `429`，並寫入 `governance_events`。
- Tenant high-risk quota：每小時 high-risk 超限時 fail closed 進入 `awaiting_approval`。
- 新增 `/v1/governance/events`、`/v1/governance/summary` 與 `/dashboard/governance`。

後續項目：

- 完整 RBAC 與 tenant 管理 UI。
- 更細的 user quota 與 per-tool quota。
- Data retention policy。
- Backup/restore drill。
- Secret rotation。
- OpenTelemetry tracing。
- Release gate 與 rollback runbook。
- 多租戶 billing/cost governance。

完成條件：

- production 變更有可靠 release gate。
- 長期保存、權限、配額與事故回顧有明確治理方式。

## Milestone：v0.7.0 / Tenant Governance Center

目標：把 v0.6 的 tenant governance MVP 變成可以操作與審計的治理中心，先做 DB-backed tenant/API key 管理與 tenant-level quota，不在本版導入 OAuth/SSO 或 billing ledger。

已完成項目：

- 新增 `tenants` 與 `tenant_api_keys` 資料表。
- API key 只保存 hash 與 prefix，明文 key 只在建立 API key 時回傳一次。
- API key 支援 `viewer`、`operator`、`admin` 三種 RBAC-lite role。
- 新增 tenant 管理 API：`GET/POST /v1/tenants`、`PATCH /v1/tenants/{tenant_id}`。
- 新增 API key 管理 API：`GET/POST /v1/tenants/{tenant_id}/api-keys`、`POST /v1/tenants/{tenant_id}/api-keys/{key_id}/revoke`。
- Tenant request quota 與 high-risk quota 可由 tenant 設定覆蓋全域 `.env` 預設。
- Governance Dashboard 顯示 tenant 設定與 API key 清單。
- tenant/key/quota 變更寫入 `governance_events`。

本段不包含：

- OAuth/SSO。
- user quota、per-tool quota 或 billing/cost ledger。
- retention purge、backup/restore drill、secret rotation 自動化。
- 真實 OS sandbox。

## 優先順序

最高優先：

- 測試與安全 regression corpus。
- Persistence/audit trail。
- Worker retry/DLQ。
- Tool ACL 與 approval gate。

中優先：

- LLM-as-judge evaluator。
- Dashboard 趨勢與 SLO。
- Tenant quota。

較後期：

- 自動 policy tuning。
- anomaly detection。
- 多租戶 billing/cost governance。
