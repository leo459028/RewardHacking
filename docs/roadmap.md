# Roadmap v0.2

本 roadmap 將專案從目前 prototype 推進到可在 production 中承擔 guardrail 職責的服務。優先順序以安全性、可觀測性、資料可靠性與可展示性為主。

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

## Phase 0：文件與基線整理

目標：讓團隊能理解現況並安全接手。

- 完成繁中專案文件。
- 標示已實作與未實作能力。
- 修正文件中的 endpoint path，例如 `/v1/healthz`。
- 補 quick start、runbook、architecture、roadmap。
- 補 Dashboard 使用說明。

完成條件：

- 新成員可依文件啟動服務、送出測試 request，並開啟 dashboard。
- 文件不把 placeholder 描述成 production-ready。

## Phase 1：測試與 API 穩定化

目標：讓目前同步 API flow 可靠、可回歸。

- 補 API tests。
- 補 sanitizer、risk scoring、evaluator unit tests。
- 建立 red-team prompt corpus。
- 統一 error response。
- 回傳 request id / trace id。
- 補 health/readiness endpoint。
- 補 ruff 到 `requirements.txt` 或調整 Makefile。

完成條件：

- CI 可跑 `ruff check app tests` 與 `pytest -q`。
- policy/evaluator 變更會觸發安全 regression tests。

## Phase 2：Persistence 與 Migration

目標：讓 policy decision、run、incident 可審計。

- 導入 Alembic。
- 建立初始 migrations。
- API 或 orchestrator 寫入 `AgentRun`。
- policy deny/escalate 寫入 `Incident`。
- evaluation result persist。
- 設計 artifact reference，避免大型 output 直接塞 DB。
- 補 DB integration tests。

完成條件：

- 每個 request 都有可查詢的 audit trail。
- schema 變更有 migration 與 rollback plan。

## Phase 3：Kafka Worker 與 Async Execution

目標：把 request ingestion 與 execution 解耦。

- API policy allow 後 publish `agent.requests`。
- 實作 worker consumer group。
- worker re-run policy check。
- publish `agent.executions`、`agent.evaluations`、`agent.incidents`。
- 實作 retry/backoff。
- 建立 DLQ。
- 監控 consumer lag。

完成條件：

- API 可快速接收任務。
- worker 可水平擴充。
- 失敗任務不會造成資料遺失或無限重試。

## Phase 4：Redis State、Approval 與 Circuit Breaker

目標：補上短期狀態、審核狀態與保護開關。

- `run:{run_id}:state`。
- approval token/state。
- tenant/user rate limit。
- tool circuit breaker。
- idempotency key。
- high-risk tenant risk window。

完成條件：

- 高風險 action 可 fail closed。
- operator 可暫停特定 tool。
- 重複 event 不會造成重複執行。

## Phase 5：真實 Tool Executor 與 Sandbox

目標：從 demo executor 進入可控的實際執行。

- 定義 executor interface。
- 實作 tool ACL。
- 限制 network、filesystem、process、secret。
- 記錄 tool transcript 與 artifact refs。
- destructive action 前強制 approval gate。
- 每個 task type 定義允許 tool set。
- 加入 timeout、resource limit、cost limit。

完成條件：

- agent 無法呼叫未授權 tool。
- destructive action 沒有人審不能執行。
- 所有 tool action 可追蹤。

## Phase 6：Evaluator 升級

目標：讓評估更接近真實安全與品質需求。

- 建立 task-specific rubrics。
- 對 code tasks 檢查 diff、test result、lint result。
- 對 data_query 檢查來源與 query lineage。
- 導入 LLM-as-judge，但只作為 evaluator signal，不作為唯一 hard deny。
- 建立 labeled evaluation dataset。
- 追蹤 precision / recall。

完成條件：

- evaluator false positive/false negative 可量化。
- 新 evaluator rule 有資料集驗證。

## Phase 7：Human Approval Workflow

目標：讓 `escalated` 有完整處理流程。

- 建立 review queue。
- 顯示 request summary、flags、risk score、artifact refs。
- 記錄 reviewer、decision、reason。
- 支援 approve once、deny、request changes。
- 對 approval 設 TTL。
- 審核操作進 audit trail。

完成條件：

- `escalated` 不再只是 terminal response，而是可被處理的 workflow。

## Phase 8：Production Observability 與治理

目標：服務能被營運與安全團隊長期管理。

- OpenTelemetry tracing。
- Dashboard。
- Alert rules。
- SLO：API latency、worker completion latency、incident response。
- Backup/restore drill。
- Data retention policy。
- Secret rotation。
- Tenant quota 與 SLA。

完成條件：

- incident 可被發現、定位、處理與回顧。
- production 變更有可靠 release gate。

## 優先順序

最高優先：

- 測試與安全 regression corpus。
- Persistence/audit trail。
- Worker retry/DLQ。
- Tool ACL 與 approval gate。

中優先：

- LLM-as-judge evaluator。
- Dashboard 與 SLO。
- Tenant quota。

較後期：

- 自動 policy tuning。
- anomaly detection。
- 多租戶 billing/cost governance。
