# 專案文件總覽

本目錄收錄 Agent Reward-Hacking Guardrail 的繁體中文文件。專有名詞保留英文，方便與程式碼、API schema、infra component 對照。

## 專案定位

這個專案是一個 guardrail service prototype，用來示範如何在 agent 系統外層建立風險治理流程：

- 請求進入時做 prompt sanitation 與 risk scoring。
- 使用 deterministic policy engine 做 `allow`、`escalate`、`deny`。
- 對允許執行的任務進行 bounded execution。
- 用 evaluator 檢查輸出是否有 reward hacking、shortcut、未驗證或 policy violation 訊號。
- 將 run、policy decision、evaluation summary 與 incident 保存到 Postgres audit trail。
- 用內建 dashboard 查看 runs、incidents、risk score 與 playground 測試結果。

## 文件清單

| 文件 | 用途 |
| --- | --- |
| [架構文件](architecture.md) | 系統設計、模組責任、資料流與 production architecture |
| [API 文件](api.md) | HTTP endpoints、request/response schema、範例 |
| [Dashboard 使用說明](dashboard.md) | 內建 UI、playground 與 audit trail 查詢 |
| [設定文件](configuration.md) | `.env`、環境變數、預設值與調整建議 |
| [部署文件](deployment.md) | Docker Compose、本機與正式部署注意事項 |
| [開發指南](development.md) | 專案結構、開發流程、擴充方式 |
| [測試指南](testing.md) | 目前測試、建議測試策略與 CI gate |
| [資料模型](data_model.md) | SQLAlchemy model、event schema、資料生命週期 |
| [Kafka Topics](kafka_topics.md) | Topic 設計、payload 建議與運維注意事項 |
| [Redis Keys](redis_keys.md) | Key convention、TTL、state/cache/breaker 用途 |
| [Migration 指南](migrations.md) | 目前 schema 狀態與 Alembic 導入建議 |
| [Observability](observability.md) | Metrics、logging、tracing 與 alert 建議 |
| [安全設計](security.md) | Threat model、guardrail 原則、production hardening |
| [Runbook](runbook.md) | 常見事故處理、診斷步驟與營運 KPI |
| [Roadmap](roadmap.md) | 從 skeleton 到 production 的分階段路線 |

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
- Dummy bounded executor。
- Heuristic evaluator。
- Prometheus metrics。
- Alembic migration。
- Postgres audit trail。
- Run / incident query APIs。
- Server-rendered dashboard UI。
- Dashboard playground。
- Docker Compose for Postgres、Redis、Kafka、API。
- API 與 service unit tests。

預留但尚未完成：

- API event publish 到 Kafka。
- Worker consumer loop。
- Redis state/circuit breaker flow。
- Human approval workflow。
- 真實 tool executor、sandbox、artifact store。

## 閱讀順序建議

1. 先讀 [架構文件](architecture.md)，理解資料流和模組邊界。
2. 再讀 [Dashboard 使用說明](dashboard.md) 與 [API 文件](api.md)，確認目前可操作的介面。
3. 若要啟動或部署，讀 [設定文件](configuration.md) 與 [部署文件](deployment.md)。
4. 若要接手開發，讀 [開發指南](development.md)、[測試指南](testing.md) 與 [Roadmap](roadmap.md)。
5. 若要營運服務，讀 [Runbook](runbook.md)、[Observability](observability.md)、[Kafka Topics](kafka_topics.md)、[Redis Keys](redis_keys.md)。
