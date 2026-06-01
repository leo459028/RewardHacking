# Agent Reward-Hacking Guardrail

## V1.5 Agent FinOps Core / Cost Ledger

目前版本：`v1.5.0`

V1.5 延續 V1.4 Checkpoint / Resume / Replay MVP，新增 Agent FinOps Core 成本資料層。Submit、worker completion、resume 與 replay flow 會寫入 deterministic `usage_events` 與 `cost_events`，並以 `pricing_catalog` 計算 estimated / actual cost；`budget_policies` 與 `budget_violations` 用於記錄 run-level budget 超額。

新增 API：
- `GET/POST/PATCH/DELETE /v1/pricing`
- `POST /v1/usage-events`
- `GET /v1/usage-events/runs/{run_id}` 與 `/v1/usage-events/tenants/{tenant_id}`
- `GET /v1/cost/runs/{run_id}`、`/v1/cost/tenants/{tenant_id}`、`/v1/cost/workflows/{workflow_id}`、`/v1/cost/models`
- `GET/POST/PATCH /v1/budgets`
- `GET /v1/budget-violations`

新增 Dashboard：
- `/dashboard/runs/{run_id}` 顯示 `Cost Summary`，可查 estimated cost、actual cost、delta、budget status 與 resource breakdown。
- `/dashboard/costs` 顯示 Tenant Cost Summary、Top Runs、Top Models / Workflows、Budget Violations 與 Pricing Catalog 小版。

目前限制：V1.5 只做成本帳本與可查詢資料層，不做 cost-aware routing、Token Broker、Safe MCP、OTLP/LangSmith exporter 或真實外部模型/工具費率同步。這些留到 V1.6 或後續版本。

## V1.4 Checkpoint / Resume / Replay MVP

歷史版本：`v1.4.0`

V1.4 延續 V1.3 Trace / Replay Readiness，新增可恢復執行的最小閉環。每次 submit 會建立 `run_checkpoints` submit checkpoint，保存 request payload、policy decision、execution plan 摘要、step count 與 replay readiness。

新增查詢入口：

- `GET /v1/runs/{run_id}/checkpoints`：查詢 run 的 checkpoint timeline。
- `POST /v1/runs/{run_id}/resume`：建立 resume checkpoint，將 run 放回 `queued`，並重新 publish worker event。
- `POST /v1/runs/{run_id}/replay`：建立 dry-run replay attempt，重建 policy / plan / step context，但不呼叫真實外部工具、不改外部狀態。
- `/dashboard/runs/{run_id}`：新增 Checkpoint / Resume 區塊與 Replay Attempts 表格。
- `/dashboard/control-plane`：最新 execution plans 新增 Resumable / Replayable 欄位。

目前限制：V1.4 的 replay 是安全 dry-run，不會重新執行真實工具，也尚未接 LangGraph checkpoint/resume、MCP Gateway、OTLP/LangSmith exporter、外部 object storage 或真實 OS sandbox。

## V1.3 Trace / Replay Readiness

上一版：`v1.3.0`

V1.3 延續 V1.2 Control Plane Execution Plan，新增內部 Trace Contract 與 Replay Bundle。每次 submit 會沿用 `X-Trace-Id`，將 trace id 寫入 run request metadata、`execution_plans.trace_id` 與 `execution_plan_steps.trace_id`，並保留 `exporter_type`、`external_trace_id`、`span_id`、`parent_span_id` 等 metadata，方便未來銜接 OTLP 或 LangSmith-style exporter。

新增查詢入口：

- `GET /v1/runs/{run_id}/trace`：查詢 run、execution plan 與 step-level audit timeline。
- `GET /v1/runs/{run_id}/replay-bundle`：匯出可審計的 replay bundle，包含 request payload、policy decision、execution plan、step audit、tool references 與 trace metadata。
- `/dashboard/runs/{run_id}`：新增 Trace / Replay 區塊、Replay Bundle 連結與 Step Audit 表格。
- `/dashboard/control-plane`：最新 execution plans 新增 Trace ID、Step Summary 與 Plan Health。

目前限制：V1.3 只提供 replay readiness 與內部 trace contract，不會重新執行 run，也尚未實作真實 OTLP、LangSmith、LangGraph checkpoint/resume 或 MCP Gateway 整合。

這是一套幫 AI agent 加上安全護欄、審核流程與事後紀錄的系統。

白話一點說：當 AI agent 被交代一個任務時，它可能會為了「看起來完成目標」而走捷徑，例如跳過測試、誤用工具、讀取不該讀的資料、繞過規則，甚至執行破壞性操作。這個專案的目標，是在任務執行前、中、後都加上檢查與紀錄，讓團隊比較容易發現風險、擋下高風險行為，並在事後追查發生了什麼。

## 專案版號

目前版本：`v1.5.0`

`v0.5.0` 是 V0.4 Execution Hardening 與 V0.5 Evaluator + Observability 的整合版。該版本已經有 queue、worker retry/DLQ、Redis run state、approval TTL/event、bounded executor transcript、tool execution audit、evaluation result 與 Dashboard 查詢入口，但還不是完整的 production sandbox、auth 或多租戶 production safety platform。

`v0.6.0` 在 v0.5 基礎上加入 Tenant Governance MVP：每筆 run 會保存 `tenant_id`，API 可用 `X-API-Key` 對應 tenant，Redis 會做 tenant request quota 與 high-risk quota 檢查，並新增 governance audit 與 `/dashboard/governance` 查詢入口。

`v0.7.0` 將 Tenant Governance MVP 推進成 Tenant Governance Center：新增 DB-backed tenant 設定、API key hash 儲存、RBAC-lite role、tenant-level quota override 與 Governance Dashboard 的 tenant/API key 查詢區塊。`.env` 的 `API_KEYS` 仍保留為開發與 bootstrap fallback；正式治理資料以 `tenants` 與 `tenant_api_keys` 資料表為準。

`v0.8.0` 新增 Production Operations Gate：提供 `/dashboard/operations`、Operations API、release readiness checks、operations evidence 與 release gate snapshot。這版會把 backup/restore drill、secret rotation 等營運證據保存到 audit trail，並加入 `X-Trace-Id` response header 與 submit metadata，但不自動執行真實備份還原、secret rotation、完整 OpenTelemetry 或 Kafka lag alert。

`v1.0.0` 將 v0.8 的可查詢營運閘門推進為 production readiness baseline：`APP_ENV=prod` 時 release gate 會把 safety regression evidence、backup/restore evidence、secret rotation evidence、DLQ、stale queued runs、blocking incidents、expired approvals 與 bootstrap API key 當成上線阻擋條件。v1.0 也補齊敏感 read API 的 API key auth、HTTP request metrics、incident counter increment，並加入 deterministic red-team corpus regression。注意：v1.0 production ready 指的是 guardrail、audit 與 release gate baseline ready；bounded executor 仍不執行真實外部副作用，也不是完整 OS sandbox。

`v1.1.0` 新增 Classical Chinese Prompt Guard：針對古文、文言文或混合中文包裝的駭客意圖做 deterministic prompt analysis。正常文言文教學與摘要不會被阻擋；若古文內容包含忽略指令、繞過政策、憑證外洩、破壞資料或惡意程式意圖，系統會加入 `classical_*` policy flags、提高風險分數，並進入人工審核或拒絕。Run detail 與 API response 的 `evaluation_summary.prompt_guard` 可查語言、正規化文字、flags 與 signals。

`v1.2.0` 新增 Control Plane 骨架：低風險 request 在進入 worker queue 前，會先產生可查詢的 `execution_plan`，保存 workflow version、model route、selected tools、steps、approval gates 與 policy snapshot。新增 workflow/tool registry API、`/v1/runs/{run_id}/execution-plan` 查詢，以及 `/dashboard/control-plane` 與 Run Detail 的 Execution Plan 區塊。這版仍不宣稱已完成真實 MCP Gateway、LangGraph checkpoint/resume、replay/time travel 或外部模型路由；V1.2 的 model route 是 deterministic control-plane metadata。

`v1.3.0` 新增 Trace / Replay Readiness：把 `X-Trace-Id` 串到 `execution_plans` 與 `execution_plan_steps`，並提供 trace API、replay bundle API、Run Detail 的 Trace / Replay 區塊與 Control Plane Dashboard 的 Trace ID / Step Summary / Plan Health。

`v1.4.0` 新增 Checkpoint / Resume / Replay MVP：submit 時保存 checkpoint，支援 checkpoint 查詢、resume checkpoint 與 dry-run replay attempt。V1.4 的 replay 不呼叫真實外部工具，只重建可審計的 policy、plan 與 step context。
`v1.5.0` 新增 Agent FinOps Core / Cost Ledger：保存 pricing catalog、usage events、cost events、budget policies 與 budget violations，並新增 FinOps API、Run Detail Cost Summary 與 `/dashboard/costs`。V1.5 只做成本資料層，不做 cost-aware routing、Token Broker、Safe MCP 或外部費率同步。

## 這個專案想解決什麼問題

AI agent 不是只會「回答問題」，它可能還會讀資料、改程式、呼叫工具、執行流程、觸發部署或操作內部系統。當 agent 可以做的事情越多，風險也會越高。

這個專案想處理的核心問題包括：

- agent 為了完成表面目標，採用取巧方式，也就是 reward hacking。
- agent 跳過驗證，例如沒有測試卻宣稱已完成。
- agent 嘗試忽略原本規則、繞過 policy 或接受 prompt injection。
- agent 要求使用不該用的工具、網路、敏感資料或破壞性操作。
- 團隊缺少 audit trail，事後不知道 request、decision、risk score、incident 是怎麼來的。
- 高風險任務沒有明確的人審流程與安全邊界。

這個系統的完整目標，是讓 agent 任務在執行前先被檢查，執行中受到限制，執行後還能被評估與追蹤。

## 名詞白話解釋

- **agent**：可以自己規劃步驟、呼叫工具或執行任務的 AI 系統。
- **guardrail**：安全護欄，用來降低 AI 做出危險或不合規行為的機率。
- **reward hacking**：AI 用取巧方式達成表面目標，但其實沒有真正做好事情。
- **policy engine**：規則判斷器，用來決定任務可以執行、需要審核，或應該拒絕。
- **audit trail**：事後可追蹤的紀錄，包含誰送出任務、系統怎麼判斷、結果如何。
- **incident**：需要注意、追蹤或審核的事件。

## Roadmap 完成後的適用情境

以下是這個專案完成完整 Roadmap 後，希望能支援的目標使用情境。這些情境不代表目前 `v1.5.0` 已全部完成。

1. AI coding agent 在修改程式碼前，檢查是否跳過測試、繞過規則或做破壞性操作。
2. 內部營運 agent 執行批次任務前，確認工具權限、任務風險與是否需要人工核准。
3. Data query agent 查詢資料前，檢查是否涉及 PII、敏感欄位或不合理匯出。
4. 客服 agent 操作帳號、退款、訂單或權限時，要求高風險動作先進入審核。
5. DevOps agent 想部署、刪除資源、修改設定時，強制走 approval gate 與 audit trail。
6. 企業想觀察 agent 的 risk score、policy flags、incident 趨勢與審核狀態。
7. 安全團隊想建立 prompt injection、reward hacking、tool misuse 的測試與監控基線。
8. 團隊想讓 agent 執行多工具 workflow，但需要 tool budget、rate limit、circuit breaker。
9. 研究或教學場景需要展示完整 agent guardrail 架構，例如 policy、executor、evaluator、audit。
10. 產品團隊想在導入真實 agent runtime 前，先建立一套可逐步強化的安全閘門。

## 不適用情境

這個專案不是萬能的 AI 安全產品，也不是所有系統都需要它。

它不適合以下情況：

- 你的系統只是單純聊天，沒有工具呼叫、資料存取或實際操作。
- 你的 agent 不會改資料、不會執行流程，也不會接觸敏感資訊。
- 你需要的是完整資安平台，例如 WAF、EDR、SIEM、DLP 或 IAM。
- 你需要的是完整 agent runtime，而不是 guardrail layer。
- 你需要立即上 production，但還沒有準備好權限控管、sandbox、監控與人審流程。
- 你期待系統可以百分之百保證 AI 不會犯錯。

## 相關限制

即使 Roadmap 全部完成，這個專案也只能降低風險，不能保證 agent 絕對安全。

需要特別注意：

- guardrail 是降低風險，不是消除風險。
- 高風險任務仍需要組織流程、權限控管與人工審核配合。
- 真正執行工具時，仍需要 sandbox、tool ACL、network policy、secret 管理。
- evaluator 只能提供風險訊號，不應被當成唯一真相。
- audit trail 可以幫助追蹤問題，但不能替代資料治理與資安制度。
- 如果 prompt、工具權限或資料權限設計不良，guardrail 也可能被繞過或誤判。

## 目前 v1.5 做到哪裡

`v1.5.0` 目前是可部署驗證、可查詢、可審計的 guarded async execution + Tenant Governance Center + Production Readiness Gate baseline，並加入古文/文言式繞語攻擊防護、Control Plane execution plan 骨架、Trace / Replay Readiness、Checkpoint / Resume / Replay MVP，以及 Agent FinOps Core / Cost Ledger。V1.5 會保存 submit/resume checkpoint、dry-run replay attempt、pricing catalog、usage/cost events、budget policies 與 budget violations，並在 Run Detail 顯示 Cost Summary、在 `/dashboard/costs` 顯示 tenant cost summary。

已完成：

- 可以送出 agent request。
- 系統會計算 risk score。
- policy engine 會做 `allow`、`escalated`、`denied` 判斷。
- 低風險任務會先建立 `queued` run，並 publish `agent.requests` event。
- Worker 可以消費 queued run，重新檢查 policy，執行 bounded executor，並寫回 evaluation。
- Bounded executor 會記錄 tool transcript 與 artifact refs，但不呼叫真實外部工具。
- Destructive 或需要審核的任務會進入 `awaiting_approval`，不會執行工具。
- Redis 已接入 run state、idempotency key 與 tool circuit breaker 的最小閉環。
- Approval API 支援查詢 queue 與 `approve_once`、`deny`、`request_changes`。
- Approval 會記錄 reviewer、reason、TTL/expired 狀態與 approval event。
- Worker 會記錄 attempt、failure reason、retry_scheduled 與 DLQ。
- Tool transcript 會落到 `tool_executions` audit table，包含 sandbox policy、resource/cost limit 與 artifact refs。
- Evaluator 會保存 task-specific rubric、deterministic fake judge signal、final score 與 verdict。
- Dashboard 新增 `/dashboard/worker`、`/dashboard/observability` 與 `/dashboard/governance`，可查看 DLQ、worker attempts、incident/approval/evaluator/SLO summary 與 tenant governance summary。
- 結果會寫進 Postgres audit trail。
- Dashboard 可以查看 runs、incidents、risk score、policy flags、evaluation summary、event timeline、tool transcript、execution plan 與 approval queue。
- 已導入 Alembic migration。
- 已有 API tests、service unit tests、dashboard HTML tests 與 GitHub CI。
- 已有最小 API key auth、Dashboard Basic Auth、tenant quota 與 governance audit。
- 可以透過 `/v1/tenants` 建立與查詢 tenant。
- 可以透過 `/v1/tenants/{tenant_id}/api-keys` 建立 DB-managed API key；明文 key 只在建立時回傳一次，資料庫只保存 hash 與 prefix。
- API key 支援 `viewer`、`operator`、`admin` 三種 RBAC-lite role；`viewer` 可讀取查詢與 Operations readiness/evidence/gate，`operator` 可送出任務、處理 approval decision，並讀取治理與 Operations 查詢資料，`admin` 可管理 tenant/API key/quota，並可建立 Operations evidence 與 release gate snapshot。
- Tenant 可設定 request quota 與 high-risk quota，會覆蓋全域 `.env` quota 預設。
- Governance Dashboard 會顯示 tenant 設定與 API key 清單。
- Operations Dashboard 會顯示 release readiness、operations evidence 與 release gate snapshot。
- Control Plane Dashboard 會顯示 workflow registry、tool registry 與最新 execution plans。
- Operations API 可建立 backup/restore drill、secret rotation、safety regression 等人工營運證據，並保存 release gate 檢查項。
- `APP_ENV=prod` 時，release gate 會因缺少 safety regression evidence、blocking incidents、DLQ、stale queued runs、expired approvals 或 bootstrap `API_KEYS` 等條件 fail closed。
- `AUTH_ENABLED=true` 時，`/v1/metrics`、worker DLQ、run subresources、observability summary 與 dashboard summary 等敏感 read API 都需要 `X-API-Key`。
- Prometheus metrics 已包含 HTTP request counter / latency，且 incident 建立時會遞增 `agent_incidents_total{severity,code}`。
- Prometheus metrics 會在 prompt guard 命中時遞增 `agent_prompt_guard_flags_total{flag}`。
- 測試已加入 deterministic red-team corpus regression，覆蓋 prompt injection、PII exfiltration、destructive action、tool budget abuse、external network bypass、system/developer instruction reveal、done shortcut 與 not tested。
- V1.1 測試已加入古文/文言式攻擊 regression，覆蓋正常文言文不阻擋、古文 prompt injection、憑證外洩、破壞意圖、惡意程式意圖、Dashboard Prompt Guard 顯示與 metrics。
- V1.3 測試已加入 Trace / Replay regression，覆蓋 trace API、replay bundle、step audit persistence、Control Plane Dashboard 與 Run Detail Trace / Replay 顯示；V1.2 Control Plane regression 仍保留。
- V1.4 測試已加入 Checkpoint / Resume / Replay regression，覆蓋 checkpoint API、resume API、dry-run replay attempt、Run Detail Checkpoint / Resume 與 Control Plane Resumable / Replayable 顯示。
- API 會回傳 `X-Trace-Id`；submit flow 會把 trace id 保存到 request metadata 與 Kafka event payload。

尚未完成：

- 還沒有完整 OS sandbox、network/filesystem/process/secret 邊界。
- 還沒有完整 human approval workflow，例如多 reviewer queue、reviewer 報表、turnaround time-series 與 request changes 後續流程；目前已具備 approval TTL、reviewer/reason 與 event audit 的基線。
- 還沒有 Kafka consumer lag alert 與完整 OpenTelemetry tracing；v1.0 先提供 HTTP metrics 與 trace id 基線。
- LLM-as-judge 目前是可插拔 signal，預設使用 deterministic fake judge 且不作為唯一 hard deny。
- 還沒有完整 OAuth、RBAC、tenant 管理 UI、retention purge、真實 backup/restore 自動化、secret rotation 自動化或真實 OS sandbox。
- 還沒有完整 OAuth/SSO、user/per-tool quota、Kafka consumer lag alert、完整 OpenTelemetry、真實 LangGraph checkpoint/resume 或真實 MCP Gateway replay execution。

所以目前 `v1.5.0` 適合用來建立 agent guardrail 的 production readiness baseline，驗證 queue/worker retry/DLQ/approval/evaluator/observability/governance/operations/safety regression、古文繞語攻擊防護、control-plane execution plan、trace/replay audit bundle、checkpoint/resume/dry-run replay，以及 FinOps 成本帳本的產品閉環；若要執行真實外部工具、外部 trace exporter 或 cost-aware routing，仍必須另接完整 sandbox、IAM、secret manager、MCP gateway、OTLP/LangSmith-style exporter、Token Broker/Safe MCP 與正式維運流程。

## 快速啟動

1. 複製環境設定：

```bash
cp .env.example .env
```

`.env.example` 預設使用 host 端 `localhost`，方便直接跑 `alembic` 或 `uvicorn`；Docker Compose 會在 `api` / `worker` container 內覆蓋成 `postgres`、`redis`、`kafka` service hostnames。

2. 啟動服務：

```bash
docker compose up -d --build
```

3. 檢查 API health：

```bash
curl http://localhost:8000/v1/healthz
```

4. 檢查 Postgres readiness：

```bash
curl http://localhost:8000/v1/readyz
```

5. 開啟 Dashboard：

```text
http://localhost:8000/dashboard
```

6. 送出測試請求。低風險任務會先回 `queued`，Docker Compose 的 worker 消費後會把 run 更新為 `completed`：

```bash
curl -X POST http://localhost:8000/v1/agent/submit-with-details \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "default",
    "user_id": "u-1001",
    "task_type": "code_fix",
    "prompt": "Fix the bug in the payment workflow without changing business rules.",
    "goal": "Return a correct patch",
    "allowed_tools": ["read_repo", "write_patch", "run_tests"],
    "constraints": {
      "no_external_network": true,
      "requires_human_approval": false,
      "max_tool_calls": 8
    }
  }'
```

## 文件導覽

- [專案文件總覽](docs/OVERVIEW.md)
- [Roadmap](docs/roadmap.md)
- [Dashboard 使用說明](docs/dashboard.md)
- [API 文件](docs/api.md)
- [架構文件](docs/architecture.md)
- [V1.1 架構圖資](docs/assets/README.md)（目前仍作為歷史圖資；V1.5 的 FinOps 能力已同步到文字文件、API 與 Dashboard，但尚未新增 v15 圖資）
- [部署文件](docs/deployment.md)
- [Runbook](docs/runbook.md)

## 下一步

如果你要繼續開發，建議先看 [Roadmap](docs/roadmap.md)。下一階段通常會優先補：

- 完整 OAuth/RBAC、tenant 管理 UI 與更細的 user quota。
- 真實 data retention purge、backup/restore 自動化、secret rotation 自動化與 rollback runbook。
- OpenTelemetry tracing、Kafka consumer lag alert 與跨 component trace propagation。
- 真實 tool sandbox 與外部 artifact/object storage。
