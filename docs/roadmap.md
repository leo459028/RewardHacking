# Roadmap v1.1

本 roadmap 將專案從 prototype 推進到可逐步承擔 guardrail 職責的服務。`v0.3.0` 起不再把 Kafka、Redis、Tool Executor、Approval 視為互不相干的線性 phase，而是先建立一條可展示、可查詢、可審計的產品閉環，再把完整 hardening 與 production governance 放到後續版本。

## 版本節奏

| 版本 | 狀態 | 主軸 | 說明 |
| --- | --- | --- | --- |
| `v0.2.0` | 已完成 | 同步 API + Audit + Dashboard prototype | 建立 request、policy、demo execution、evaluation、Postgres audit trail 與 server-rendered Dashboard。 |
| `v0.3.0` | 已完成 | Guarded Async Execution MVP | 合併 Kafka、Redis、Approval、Executor Interface 的最小閉環。 |
| `v0.4.0` | 已完成 | Execution Hardening | 補 worker retry/DLQ、approval TTL/event、tool execution audit、resource/cost limit 與 sandbox policy。 |
| `v0.5.0` | 已完成 | Evaluator + Observability | 補 task-specific evaluator、deterministic judge signal、worker metrics、SLO 與 Dashboard 趨勢。 |
| `v0.6.0` | 已完成 | Tenant Governance MVP | 補 API key auth、Dashboard Basic Auth、tenant-aware audit、Redis quota 與 Governance Dashboard。 |
| `v0.7.0` | 已完成 | Tenant Governance Center | 補 DB-backed tenant 設定、API key hash 儲存、RBAC-lite、tenant quota override 與 Governance Dashboard 查詢區塊。 |
| `v0.8.0` | 已完成 | Production Operations Gate | 補 Operations Dashboard、operations evidence、release readiness、release gate snapshot 與 `X-Trace-Id` 基線。 |
| `v1.0.0` | 已完成 | Production Ready：A + C-lite | 將 release gate 升級為 production readiness blocker，補 safety regression evidence、敏感 read API auth coverage、HTTP metrics、incident counter 與 red-team corpus regression。 |
| `v1.1.0` | 目前版本 | Classical Chinese Prompt Guard | 偵測古文/文言式繞語包裝的 prompt injection、policy bypass、憑證外洩、破壞資料與惡意程式意圖，並在 API、Dashboard、metrics 與測試中可查。 |
| `v1.2+` | 規劃中 | Production Operations Automation / C-full | 補真實 backup/restore automation、retention purge、secret rotation automation、完整 OpenTelemetry、Kafka lag alert、LLM judge 與更完整評測資料集。 |

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

## Milestone：v0.8.0 / Production Operations Gate

目標：把 v0.7 的可審計治理中心往 production readiness 推進，但先做可查詢、可保存、可驗證的營運閘門，不在本版宣稱已自動完成真實備份還原或完整 OpenTelemetry。

已完成項目：

- 新增 `/dashboard/operations`，可查看 release readiness、operations evidence 與 release gate snapshot。
- 新增 Operations API：`/v1/operations/readiness`、`/v1/operations/evidence`、`/v1/operations/release-gates`。
- 新增 `operations_evidence`、`release_gate_runs`、`release_gate_items` audit tables。
- Release gate checks 涵蓋 audit retention 設定、stale queued runs、worker DLQ、high/critical incidents、expired approvals、tenant API keys、backup/restore evidence、secret rotation evidence 與 production bootstrap key 檢查。
- `X-Trace-Id` 會進 response header；submit flow 會把 trace id 保存到 request metadata 與 Kafka request payload。

本段不包含：

- 自動執行 `pg_dump` / restore drill。
- 自動 secret rotation。
- 自動 retention purge。
- 完整 OpenTelemetry SDK。
- 真實 Kafka broker consumer lag alert。

## Milestone：v1.0.0 / Production Ready：A + C-lite

目標：把 v0.8 的可查詢營運閘門升級為 production readiness baseline。v1.0 不宣稱完成真實 OS sandbox 或外部 LLM judge，而是讓 release gate 在正式環境能根據營運證據、安全 regression 與阻擋條件 fail closed。

已完成項目：

- Operations evidence 新增 `safety_regression`，`/dashboard/operations` 可新增與查詢。
- `/v1/operations/readiness` 新增 `safety_regression_evidence` check；`APP_ENV=prod` 時缺少 fresh safety regression evidence 會 fail。
- Release gate snapshot 會保存 safety regression check item。
- `AUTH_ENABLED=true` 時，`/v1/metrics`、worker DLQ、run subresources、observability summary 與 dashboard summary 都需要 `X-API-Key`。
- Prometheus metrics 新增 HTTP request counter / latency；incident 建立時會遞增 `agent_incidents_total{severity,code}`。
- 新增 deterministic red-team corpus regression，覆蓋 prompt injection、PII exfiltration、destructive action、tool budget abuse、external network bypass、system/developer instruction reveal、done shortcut 與 not tested。
- FastAPI / package version 更新為 `1.0.0`。

本段不包含：

- 外部 LLM-as-judge 或大型標註資料平台；`LLM_JUDGE_ENABLED` 預設仍為 `false`。
- OAuth/SSO 或完整企業 IAM；本版仍使用 DB-managed API key、RBAC-lite 與 Dashboard Basic Auth。
- 真實 OS sandbox、真實 backup/restore automation、secret rotation automation、retention purge、完整 OpenTelemetry 或 Kafka broker lag alert。

## Milestone：v1.1.0 / Classical Chinese Prompt Guard

目標：防止使用者用古文、文言文或混合中文包裝駭客意圖來繞過 prompt sanitizer 與 policy engine，同時避免把正常文言文教學誤擋。

已完成項目：

- 新增 deterministic prompt analysis，輸出 language、normalized prompt、flags 與 signals。
- 正常文言文內容不阻擋；古文攻擊意圖會加入 `classical_*` policy flags。
- 風險評分會把古文繞語攻擊納入 high-risk 訊號。
- `evaluation_summary.prompt_guard` 保存可審計分析結果。
- Dashboard run detail 顯示 Prompt Guard 區塊。
- Prometheus 新增 `agent_prompt_guard_flags_total{flag}`。
- 新增 V1.1 regression tests，覆蓋正常文言文、古文 prompt injection、憑證外洩、破壞意圖、惡意程式意圖、Dashboard 與 metrics。

本段不包含：

- 外部 LLM 翻譯或語義模型。
- 完整古漢語 NLP。
- 輸出端古文安全審查；本版聚焦 request ingress。

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
