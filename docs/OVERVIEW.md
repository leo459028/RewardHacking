# 專案文件總覽 v1.5

本目錄收錄 Agent Reward-Hacking Guardrail 的繁體中文文件。專有名詞保留英文，方便與程式碼、API schema、infra component 對照。

## 版本

目前版本：`v1.5.0`

`v0.6.0` 對應 Tenant Governance MVP。此版本保留 v0.5 的 queued submit、Kafka request event、worker consumer、Redis run state / idempotency / circuit breaker、approval API、bounded executor transcript、worker retry/DLQ、approval TTL/event、tool execution audit、evaluation result、內建 Dashboard worker/observability 頁，並新增 `tenant_id` audit、API key auth、Dashboard Basic Auth、tenant quota、governance events、governance summary API 與 Governance Dashboard。

`v0.7.0` 對應 Tenant Governance Center。此版本在 v0.6 基礎上新增 DB-backed tenant 設定、API key hash 儲存、RBAC-lite role、tenant-level quota override、tenant/API key 管理 API，以及 Governance Dashboard 的 tenant/API key 查詢區塊。

`v0.8.0` 對應 Production Operations Gate。此版本新增 Operations Dashboard、Operations API、operations evidence、release gate snapshot 與 `X-Trace-Id` 基線，用來保存 release readiness 與營運證據；真實自動備份還原、secret rotation 與完整 OpenTelemetry 仍是後續項目。

`v1.0.0` 對應 Production Ready：A + C-lite。此版本將 release gate 升級為 production readiness blocker，新增 safety regression evidence、敏感 read API auth coverage、HTTP metrics、incident counter increment 與 deterministic red-team corpus regression；真實 OS sandbox、外部 LLM judge、OAuth/SSO 與自動備份還原仍是後續項目。

`v1.1.0` 對應 Classical Chinese Prompt Guard。此版本新增古文/文言式繞語攻擊偵測，正常文言文不會只因語言形式被阻擋；若古文 prompt 包含忽略指令、繞過政策、憑證外洩、破壞資料或惡意程式意圖，會提高 risk score 並進入人工審核或拒絕。API、Dashboard、metrics 與測試都能查詢這些 signals。

`v1.2.0` 對應 Control Plane Execution Plan。此版本新增 builtin + DB-backed workflow/tool registry、`execution_plans`、Control Plane API、Run Detail Execution Plan 區塊與 `/dashboard/control-plane`，讓低風險 run 進入 worker queue 前已有可查詢、可審計的執行計畫。這仍是控制面骨架，不包含真實 MCP Gateway、LangGraph checkpoint/resume、replay/time travel 或外部 model router。

`v1.3.0` 對應 Trace / Replay Readiness。此版本把 V1.2 的 execution plan 串上 `X-Trace-Id`，新增 `execution_plan_steps` step-level audit、`/v1/runs/{run_id}/trace`、`/v1/runs/{run_id}/replay-bundle`，並在 Run Detail 與 Control Plane Dashboard 顯示 Trace / Replay 資訊。V1.3 先提供內部 trace contract 與 replay bundle，不實作真正自動重放、OTLP exporter、LangSmith-style exporter、LangGraph checkpoint/resume 或 MCP Gateway。

`v1.4.0` 對應 Checkpoint / Resume / Replay MVP。此版本新增 `run_checkpoints`、`replay_attempts`、`/v1/runs/{run_id}/checkpoints`、`POST /v1/runs/{run_id}/resume`、`POST /v1/runs/{run_id}/replay`，並在 Run Detail 與 Control Plane Dashboard 顯示 checkpoint、resume 與 dry-run replay 狀態。V1.4 仍不呼叫真實外部工具，也不等同完整 LangGraph checkpoint/resume。
`v1.5.0` 對應 Agent FinOps Core / Cost Ledger。此版本新增 `pricing_catalog`、`usage_events`、`cost_events`、`budget_policies`、`budget_violations`、FinOps API、Run Detail Cost Summary 與 `/dashboard/costs`。V1.5 只做成本資料層，不做 cost-aware routing、Token Broker、Safe MCP 或外部費率同步。

## 專案定位

這個專案是一個 guardrail service prototype，用來示範如何在 agent 系統外層建立風險治理流程：

- 請求進入時做 prompt sanitation 與 risk scoring。
- 對古文/文言式繞語攻擊做 Prompt Guard 分析。
- 使用 deterministic policy engine 做 `allow`、`escalate`、`deny`。
- 對低風險任務建立 queued run，交由 worker 進行 bounded execution。
- 在 queued 前建立 Control Plane execution plan，保存 workflow version、selected tools、steps、approval gates 與 policy snapshot。
- 將 execution plan、step audit 與 run metadata 綁到同一個 trace id，提供 trace 查詢與 replay bundle 匯出。
- 保存 submit/resume checkpoint，並提供 dry-run replay attempt，讓 run 具備可恢復與可稽核重建的最小閉環。
- 對 destructive 或需審核任務建立 approval queue，不直接執行工具。
- 用 evaluator 檢查輸出是否有 reward hacking、shortcut、未驗證或 policy violation 訊號。
- 將 run、policy decision、evaluation summary 與 incident 保存到 Postgres audit trail。
- 用內建 dashboard 查看 runs、incidents、risk score、event timeline、tool transcript、execution plan、Trace / Replay、Checkpoint / Resume、approval queue、tenant governance、control plane、operations gate 與 playground 測試結果。
- 以 API key auth 對應 tenant，支援 `.env` bootstrap fallback 與 DB-managed API key，並以 Redis quota 對 tenant request 與 high-risk request 做 fail-closed 控制。
- 以 operations evidence 保存 backup/restore、secret rotation 與 safety regression 證據，讓 production release gate 可 fail closed。

## 文件清單

| 文件 | 用途 |
| --- | --- |
| [架構文件](architecture.md) | 系統設計、模組責任、資料流與 production architecture |
| [API 文件](api.md) | HTTP endpoints、request/response schema、範例 |
| [Dashboard 使用說明](dashboard.md) | 內建 UI、playground 與 audit trail 查詢 |
| [設定文件](configuration.md) | `.env`、環境變數、預設值與調整建議 |
| [部署文件](deployment.md) | Docker Compose、本機與正式部署注意事項 |
| [開發指南](development.md) | 專案結構、開發流程、擴充方式 |
| [測試指南](testing_guide.md) | 目前測試、建議測試策略與 CI gate |
| [資料模型](data_model.md) | SQLAlchemy model、event schema、資料生命週期 |
| [Kafka Topics](kafka_topics.md) | Topic 設計、payload 建議與運維注意事項 |
| [Redis Keys](redis_keys.md) | Key convention、TTL、state/cache/breaker 用途 |
| [Migration 指南](migrations.md) | Alembic migration、常用指令與 rollback 建議 |
| [Observability](observability.md) | Metrics、logging、tracing 與 alert 建議 |
| [安全設計](security.md) | Threat model、guardrail 原則、production hardening |
| [Runbook](runbook.md) | 常見事故處理、診斷步驟與營運 KPI |
| [Roadmap](roadmap.md) | 從 skeleton 到 production 的分階段路線 |
| [V1.1 架構圖資](assets/README.md) | `docs/assets/*-v11.png`，歷史架構圖、流程圖、資料模型、Dashboard、metrics 與 threat model；V1.5 文件已補 FinOps Core 文字說明，尚未新增 v15 圖資 |

## V1.1 架構圖資

新版圖資保存在 `docs/assets/*-v11.png`，舊版 `*-v1.png` 已保留作為歷史圖資。V1.5 目前尚未新增 `*-v15.png` 圖資，因此若需要圖片仍可引用 V1.1 圖資，但文字文件已以 V1.5 Agent FinOps Core / Cost Ledger 為準。

![V1.1 Architecture](assets/architecture-ad-marketing-v11.png)

## 快速啟動

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/v1/healthz
```

打開 dashboard：

```text
http://localhost:8000/dashboard
```

送出一個完整回傳的測試請求：

```bash
curl -X POST http://localhost:8000/v1/agent/submit-with-details \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "default",
    "user_id": "u-1001",
    "task_type": "analysis",
    "prompt": "Analyze the incident and summarize the likely root cause.",
    "goal": "Summarize the root cause",
    "allowed_tools": ["read_logs"],
    "constraints": {
      "no_external_network": true,
      "requires_human_approval": false,
      "max_tool_calls": 4,
      "allow_destructive_actions": false,
      "allow_pii_access": false
    }
  }'
```

## 目前實作狀態

已實作：

- FastAPI routes。
- Pydantic schema。
- Prompt injection pattern detection。
- Risk scoring。
- Deterministic policy engine。
- Bounded executor interface、tool transcript 與 sandbox boundary policy。
- Heuristic evaluator、task-specific rubric 與 deterministic fake judge signal。
- Prometheus metrics。
- HTTP request metrics 與 incident counter。
- Alembic migration。
- Postgres audit trail。
- Run / incident query APIs。
- Server-rendered dashboard UI。
- Dashboard playground。
- Kafka request publish 與 worker consumer。
- Redis run state、idempotency key 與 circuit breaker。
- Approval queue API 與 Dashboard。
- Tenant-aware audit、API key auth 與 Dashboard Basic Auth MVP。
- Tenant request quota、high-risk quota fail-closed 與 governance events。
- `/v1/governance/events`、`/v1/governance/summary` 與 `/dashboard/governance`。
- DB-backed tenant 設定、API key hash 儲存、RBAC-lite 與 tenant quota override。
- `/v1/tenants` 與 `/v1/tenants/{tenant_id}/api-keys` 管理 API。
- `/v1/operations/readiness`、operations evidence、release gate snapshot 與 `/dashboard/operations`。
- `safety_regression` evidence、`safety_regression_evidence` readiness check 與 red-team corpus regression。
- Classical Chinese Prompt Guard、`evaluation_summary.prompt_guard`、Dashboard Prompt Guard 區塊與 `agent_prompt_guard_flags_total{flag}` metric。
- Control Plane execution plan、workflow/tool registry、`/v1/control-plane/*` API、`/v1/runs/{run_id}/execution-plan` 與 `/dashboard/control-plane`。
- `X-Trace-Id` response header 與 submit metadata / Kafka payload trace id。
- Checkpoint / Resume / Replay MVP、checkpoint API、resume API、dry-run replay API 與 Dashboard 顯示。
- Docker Compose for Postgres、Redis、Kafka、API、worker。
- API 與 service unit tests。

預留但尚未完成：

- 真實 tool sandbox 與 artifact store。
- 真實 MCP Gateway、LangGraph checkpoint/resume、真正 replay execution 與外部 model router。
- Kafka consumer lag alert。
- 完整 OAuth/SSO、user/per-tool quota、retention purge、backup/restore 自動化、secret rotation 自動化、外部 LLM judge、真實 OS sandbox 與完整 production observability。

## 閱讀順序建議

1. 先讀 [架構文件](architecture.md)，理解資料流和模組邊界。
2. 再讀 [Dashboard 使用說明](dashboard.md) 與 [API 文件](api.md)，確認目前可操作的介面。
3. 若要啟動或部署，讀 [設定文件](configuration.md) 與 [部署文件](deployment.md)。
4. 若要接手開發，讀 [開發指南](development.md)、[測試指南](testing_guide.md) 與 [Roadmap](roadmap.md)。
5. 若要營運服務，讀 [Runbook](runbook.md)、[Observability](observability.md)、[Kafka Topics](kafka_topics.md)、[Redis Keys](redis_keys.md)。
