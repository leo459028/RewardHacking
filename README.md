# Agent Reward-Hacking Guardrail

這是一套幫 AI agent 加上安全護欄、審核流程與事後紀錄的系統。

白話一點說：當 AI agent 被交代一個任務時，它可能會為了「看起來完成目標」而走捷徑，例如跳過測試、誤用工具、讀取不該讀的資料、繞過規則，甚至執行破壞性操作。這個專案的目標，是在任務執行前、中、後都加上檢查與紀錄，讓團隊比較容易發現風險、擋下高風險行為，並在事後追查發生了什麼。

## 專案版號

目前版本：`v0.6.0`

`v0.5.0` 是 V0.4 Execution Hardening 與 V0.5 Evaluator + Observability 的整合版。該版本已經有 queue、worker retry/DLQ、Redis run state、approval TTL/event、bounded executor transcript、tool execution audit、evaluation result 與 Dashboard 查詢入口，但還不是完整的 production sandbox、auth 或多租戶 production safety platform。

`v0.6.0` 在 v0.5 基礎上加入 Tenant Governance MVP：每筆 run 會保存 `tenant_id`，API 可用 `X-API-Key` 對應 tenant，Redis 會做 tenant request quota 與 high-risk quota 檢查，並新增 governance audit 與 `/dashboard/governance` 查詢入口。

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

以下是這個專案完成完整 Roadmap 後，希望能支援的目標使用情境。這些情境不代表目前 `v0.6.0` 已全部完成。

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

## 目前 v0.6 做到哪裡

`v0.6.0` 目前是可展示、可查詢、可審計的 guarded async execution + tenant governance MVP。

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
- Dashboard 可以查看 runs、incidents、risk score、policy flags、evaluation summary、event timeline、tool transcript 與 approval queue。
- 已導入 Alembic migration。
- 已有 API tests、service unit tests、dashboard HTML tests 與 GitHub CI。
- 已有最小 API key auth、Dashboard Basic Auth、tenant quota 與 governance audit。

尚未完成：

- 還沒有完整 OS sandbox、network/filesystem/process/secret 邊界。
- 還沒有完整 human approval workflow，例如 reviewer 報表、TTL 與 request changes 後續流程。
- 還沒有 Kafka consumer lag alert 與完整 OpenTelemetry tracing。
- LLM-as-judge 目前是可插拔 signal，預設使用 deterministic fake judge 且不作為唯一 hard deny。
- 還沒有完整 OAuth、RBAC、tenant 管理 UI、retention governance、backup/restore、secret rotation 或真實 OS sandbox。

所以目前 `v0.6.0` 適合用來 demo guarded async execution hardening 與 tenant governance MVP，驗證 queue/worker retry/DLQ/approval/evaluator/observability/governance 的產品閉環；不建議直接當成完整 production 安全平台。

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
- [部署文件](docs/deployment.md)
- [Runbook](docs/runbook.md)

## 下一步

如果你要繼續開發，建議先看 [Roadmap](docs/roadmap.md)。下一階段通常會優先補：

- 完整 OAuth/RBAC、tenant 管理 UI 與更細的 user quota。
- Data retention、backup/restore drill、secret rotation 與 rollback/release gate。
- OpenTelemetry tracing、Kafka consumer lag alert 與跨 component trace id。
- 真實 tool sandbox 與外部 artifact/object storage。
