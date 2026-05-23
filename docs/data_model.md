# 資料模型

本專案目前使用 SQLAlchemy models 搭配 Alembic migration 管理 Postgres audit tables。API submit 後會將 run 與 incident 寫入資料庫，dashboard 與查詢 API 都從同一份資料讀取。

## SQLAlchemy Models

### `AgentRun`

Table：`agent_runs`

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | integer | primary key |
| `run_id` | string | 對外追蹤用 UUID，唯一 |
| `user_id` | string | 使用者或租戶內使用者 |
| `task_type` | string | 任務類型 |
| `prompt` | text | 任務 prompt |
| `goal` | text | 任務目標 |
| `status` | string | `denied`、`escalated`、`queued`、`completed` 等 |
| `decision_reason` | text | policy decision reason |
| `risk_score` | float | policy risk score |
| `policy_flags` | JSON | 命中的 policy flags |
| `request_payload` | JSON | 原始 request payload |
| `execution_output` | text | executor output 摘要 |
| `evaluation_summary` | JSON | policy / execution / evaluation event summary |
| `result` | JSON | response、execution output、evaluation summary 的整合快照 |
| `requires_human_review` | boolean | 是否需人工審核 |
| `created_at` | timestamp | 建立時間 |
| `updated_at` | timestamp | 更新時間 |

索引：

- `run_id` unique index。
- `user_id` index。
- `status` index。

### `Incident`

Table：`incidents`

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | integer | primary key |
| `run_id` | string | 關聯 run |
| `severity` | string | `low`、`medium`、`high`、`critical` |
| `code` | string | incident code |
| `detail` | text | incident detail |
| `created_at` | timestamp | 建立時間 |

目前 incident 建立規則：

- `status=denied`：建立 `policy_denied` incident。
- `status=escalated`：建立 `policy_escalated` incident。
- evaluator verdict 為 `review`：建立 `eval_review_required` incident。

## Event Schemas

所有 event 繼承 `BaseEvent`：

| 欄位 | 說明 |
| --- | --- |
| `event_id` | event UUID |
| `trace_id` | 跨 component 追蹤 ID |
| `run_id` | 單次 agent run ID |
| `event_type` | event 類型 |
| `ts` | ISO timestamp |
| `payload` | event-specific JSON |

目前 event types：

- `policy.decision`
- `execution.result`
- `evaluation.result`
- `incident`

## 資料生命週期建議

### Hot Data

近期 run state、approval state、circuit breaker 可放 Redis，TTL 依用途設定 5 分鐘到 24 小時。這部分尚未在本段實作。

### Audit Data

policy decision、execution summary、evaluation summary、incident 已進 Postgres，保留期可依合規需求設定。

### Artifacts

程式碼 diff、test report、tool transcript、模型輸出全文可能太大，不建議全部塞進 `agent_runs.result`。正式環境可加入 object storage，Postgres 只存 artifact URI 與 hash。

## 隱私與資料保護

- Prompt 可能包含敏感資料，正式環境需做 PII detection 與 redaction。
- Log 中不應輸出完整 prompt 或 tool output。
- `metadata` 欄位應避免存 access token、password、private key。
- Audit retention 到期後需要清除或匿名化。
