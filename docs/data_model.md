# 資料模型

本專案目前使用 SQLAlchemy models 搭配 Alembic migration 管理 Postgres audit tables。API submit 後會將 run 與 incident 寫入資料庫，dashboard 與查詢 API 都從同一份資料讀取。

## SQLAlchemy Models

### `AgentRun`

Table：`agent_runs`

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | integer | primary key |
| `run_id` | string | 對外追蹤用 UUID，唯一 |
| `tenant_id` | string | run 所屬租戶，既有資料 migration 後預設為 `default` |
| `user_id` | string | 使用者或租戶內使用者 |
| `task_type` | string | 任務類型 |
| `prompt` | text | 任務 prompt |
| `goal` | text | 任務目標 |
| `status` | string | `denied`、`escalated`、`queued`、`running`、`awaiting_approval`、`completed`、`failed` 等 |
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

`v1.1.0` 起的 Prompt Guard 不新增欄位或資料表；分析結果保存在 `evaluation_summary.prompt_guard`，包含：

- `language`：例如 `en`、`zh`、`mixed_zh`、`zh_classical`。
- `normalized_prompt`：去除頭尾與重複空白後的 prompt。
- `flags`：例如 `classical_chinese_obfuscation`、`classical_prompt_injection`。
- `signals`：每個命中規則的 category、canonical intent 與 matched text。

索引：

- `run_id` unique index。
- `tenant_id` index。
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
- `status=awaiting_approval`：建立 `approval_required` incident。
- `status=failed`：建立 `worker_failed` incident。
- evaluator verdict 為 `review`：建立 `eval_review_required` incident。

### `WorkerAttempt`

Table：`worker_attempts`

記錄 worker 每次處理 run 的 attempt number、status、failure reason、next retry time。`status=retry_scheduled` 表示還會重試，`status=dlq` 表示已超過重試上限。

### `ToolExecution`

Table：`tool_executions`

記錄每個 tool action 的 tool name、status、detail、sandbox policy 與 artifact refs。目前 sandbox policy 是可審計契約，不代表已啟動真實 OS sandbox。

### `ApprovalEvent`

Table：`approval_events`

記錄 approval pending、approve once、deny、request changes、reviewer、reason、expires_at 與 expired 狀態。

### `EvaluationResultRecord`

Table：`evaluation_results`

記錄 evaluator final score、verdict、task-specific rubric、judge signal 與 explanation。LLM-as-judge 目前預設為 deterministic fake signal，且 `used_for_hard_deny=false`。

### `GovernanceEvent`

Table：`governance_events`

記錄 v0.6 tenant governance 事件，例如 request quota exceeded 與 high-risk quota fail-closed。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | integer | primary key |
| `tenant_id` | string | 事件所屬租戶 |
| `user_id` | string | 觸發事件的租戶內使用者 |
| `run_id` | string | 若事件已建立 run，保存對應 run id；request quota 直接阻擋時可為空字串 |
| `event_type` | string | 例如 `request_quota_exceeded`、`high_risk_quota_exceeded` |
| `decision` | string | 例如 `blocked`、`approval_required` |
| `reason` | text | 人可讀原因 |
| `limit_value` | integer | quota limit |
| `used` | integer | 事件發生時已使用數量 |
| `remaining` | integer | 剩餘 quota |
| `details` | JSON | quota bucket 等補充資訊 |
| `created_at` | timestamp | 建立時間 |

### `Tenant`

Table：`tenants`

記錄 v0.7 Tenant Governance Center 的 tenant 設定。`tenant_id` 仍是各 audit table 的邏輯關聯鍵；本版不新增 foreign key，避免和既有 demo/backfill 資料產生部署耦合。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | integer | primary key |
| `tenant_id` | string | tenant 識別，唯一 |
| `display_name` | string | Dashboard 顯示名稱 |
| `status` | string | `active` 或後續管理狀態；停用 tenant 的 DB-managed key 會被拒絕 |
| `request_limit_per_minute` | integer | tenant-level request quota |
| `high_risk_limit_per_hour` | integer | tenant-level high-risk quota |
| `created_at` | timestamp | 建立時間 |
| `updated_at` | timestamp | 更新時間 |

### `TenantApiKey`

Table：`tenant_api_keys`

記錄 DB-managed API key。明文 key 只在建立時回傳一次；資料庫只保存 hash 與 prefix。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | integer | primary key |
| `key_id` | string | 對外管理用 key id |
| `tenant_id` | string | key 所屬 tenant |
| `name` | string | key 顯示名稱 |
| `key_prefix` | string | Dashboard 顯示用 prefix |
| `key_hash` | string | SHA-256 hash，唯一 |
| `role` | string | `viewer`、`operator` 或 `admin` |
| `status` | string | `active` 或 `revoked` |
| `revoked` | boolean | 是否已撤銷 |
| `created_at` | timestamp | 建立時間 |
| `revoked_at` | timestamp | 撤銷時間 |

### `OperationsEvidence`

Table：`operations_evidence`

記錄 Operations Gate 使用的人工營運證據，例如 backup/restore drill、secret rotation、safety regression 或 retention review。這是審計證據，不會自動執行實際備份、還原、輪替或 red-team 測試。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | integer | primary key |
| `evidence_type` | string | 例如 `backup_restore_drill`、`secret_rotation`、`safety_regression`、`retention_review` |
| `status` | string | `pass`、`warn` 或 `fail` |
| `summary` | text | 人可讀證據摘要 |
| `reference_url` | string | 文件、測試報告或外部紀錄位置 |
| `metadata_payload` | JSON | 補充資訊 |
| `created_by` | string | 建立者或 key 來源 |
| `created_at` | timestamp | 建立時間 |

### `ReleaseGateRun` / `ReleaseGateItem`

Tables：`release_gate_runs`、`release_gate_items`

保存某次 release gate snapshot 與每個檢查項目的結果。`release_gate_runs.status` 由 items 彙總而來：只要任一 item 為 `fail`，整體為 `fail`；否則任一 item 為 `warn` 時整體為 `warn`；其餘為 `pass`。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `gate_id` | string | 對外查詢 release gate 的 ID |
| `release_version` | string | 例如 `v1.4.0` |
| `status` | string | `pass`、`warn` 或 `fail` |
| `summary` | text | gate 結果摘要 |
| `created_by` | string | 建立者 |
| `trace_id` | string | 建立 gate 時的 `X-Trace-Id` |
| `check_key` | string | item 檢查鍵，例如 `backup_restore_evidence`、`safety_regression_evidence` |
| `title` / `detail` | string / text | item 顯示文字 |
| `metadata_payload` | JSON | item 補充資訊 |

### `WorkflowDefinition`

Table：`workflow_definitions`

記錄 V1.2 Control Plane 的 DB-managed workflow。系統也內建 builtin workflows；DB 中相同 `workflow_id` 與 `version` 的資料會覆蓋 builtin 顯示結果。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `workflow_id` | string | workflow 識別，例如 `analysis-basic` |
| `version` | string | workflow 版本 |
| `task_type` | string | 對應 `AgentRequest.task_type` |
| `display_name` | string | Dashboard 顯示名稱 |
| `status` | string | `active` 時可被 planner 選用 |
| `definition` | JSON | workflow steps 或後續 registry metadata |

### `ToolDefinition`

Table：`tool_definitions`

記錄 V1.2 Control Plane 的 DB-managed tool registry。系統也內建 builtin tools；DB 中相同 `tool_name` 的資料會覆蓋 builtin 顯示結果。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `tool_name` | string | tool 識別，例如 `read_data` |
| `display_name` | string | Dashboard 顯示名稱 |
| `status` | string | `active` 時可被 planner 選用 |
| `task_types` | JSON | 可使用此 tool 的任務類型 |
| `requires_approval` | boolean | 是否強制進 approval gate |
| `risk_level` | string | `low`、`medium`、`high` 等風險標註 |
| `metadata_payload` | JSON | registry 補充資訊 |

### `ExecutionPlan`

Table：`execution_plans`

V1.3 在低風險 run 進入 worker queue 前建立 execution plan，並把 HTTP `X-Trace-Id` 寫入 `trace_id` 與 `trace_metadata`。Worker 若找不到對應 plan，會 fail closed，將 run 轉為 `awaiting_approval` 並加入 `missing_execution_plan` flag。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `plan_id` | string | execution plan 對外識別 |
| `run_id` | string | 對應 `agent_runs.run_id`，唯一 |
| `tenant_id` | string | run 所屬 tenant |
| `workflow_id` / `workflow_version` | string | planner 選出的 workflow |
| `status` | string | `planned` 或 `requires_approval` |
| `model_route` | JSON | deterministic model route metadata，不代表外部模型呼叫 |
| `selected_tools` | JSON | planner 選出的工具與風險資訊 |
| `steps` | JSON | policy、approval、tool execution、evaluation 等步驟 |
| `approval_gates` | JSON | tool 或 registry 造成的 approval gates |
| `policy_snapshot` | JSON | submit 時的 policy decision 快照 |
| `trace_id` | string | 與 run request metadata 對齊的 trace id |
| `trace_metadata` | JSON | 保留 `exporter_type`、`external_trace_id`、`span_id`、`parent_span_id` 等未來 exporter 欄位 |

### `ExecutionPlanStep`

Table：`execution_plan_steps`

V1.3 新增 step-level audit，用來讓 Trace / Replay API 與 Dashboard 查詢每個 execution plan step 的狀態與摘要。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `step_id` | string | step 對外識別，例如 `step_1_plan` |
| `plan_id` | string | 對應 `execution_plans.plan_id` |
| `run_id` | string | 對應 `agent_runs.run_id` |
| `trace_id` | string | 與 execution plan / run metadata 對齊 |
| `step_index` | integer | step 在 timeline 中的順序 |
| `step_type` | string | `policy_check`、`tool_execution`、`evaluation` 等 |
| `status` | string | `planned`、`completed`、`failed` 等 |
| `started_at` / `finished_at` | timestamp | step 時間戳 |
| `duration_ms` | integer | step duration |
| `input_summary` / `output_summary` / `error_summary` | text | replay bundle 與 Dashboard 用的摘要 |
| `metadata_payload` | JSON | step metadata 與未來 exporter 欄位 |

### `RunCheckpoint`

Table：`run_checkpoints`

V1.4 新增 checkpoint timeline。Submit flow 會建立 `checkpoint_type=submit` 的 checkpoint；`POST /v1/runs/{run_id}/resume` 會建立 `checkpoint_type=resume` 的 checkpoint，讓 run 的恢復點可以被 API 與 Dashboard 查詢。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `checkpoint_id` | string | checkpoint 對外識別 |
| `run_id` / `tenant_id` / `trace_id` | string | 對應 run、tenant 與 trace |
| `checkpoint_type` | string | `submit` 或 `resume` |
| `status` | string | checkpoint 建立當下的 run status |
| `resume_cursor` | string | 建議恢復位置，例如 `worker_queue` 或 `approval_queue` |
| `state_payload` | JSON | request、policy、execution plan 摘要、step count、replay readiness 與 resume reason |
| `created_by` | string | 建立來源，例如 `dashboard`、API key 或 reviewer |
| `created_at` | timestamp | 建立時間 |

### `ReplayAttempt`

Table：`replay_attempts`

V1.4 新增 dry-run replay attempt。Replay attempt 只重建 policy、workflow 與 step context，不呼叫真實外部工具，也不修改外部狀態。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `replay_id` | string | replay attempt 對外識別 |
| `source_run_id` | string | 原始 run id |
| `tenant_id` / `trace_id` | string | 對應 tenant 與 trace |
| `mode` | string | 目前只支援 `dry_run` |
| `status` | string | replay attempt 狀態，目前成功建立即為 `completed` |
| `result_payload` | JSON | replay 的 source status、policy snapshot、workflow id、step count 與外部執行標記 |
| `limitations` | JSON | 明確記錄不呼叫外部工具、不改外部狀態 |
| `created_by` | string | 建立者 |
| `created_at` | timestamp | 建立時間 |

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

注意：目前 Kafka event envelope 本身未放 `tenant_id` 欄位；tenant 資訊會放在 `payload.request.tenant_id`，並在 Postgres audit tables 中保存。

目前 `evaluation_summary.event_timeline` 與 Kafka payload 常見 event types：

- `agent.requested`
- `policy.decision`
- `execution.result`
- `evaluation.result`
- `incident`
- `run.state`
- `worker.retry_scheduled`
- `worker.failed`
- `approval.decision`

`v1.4.0` 延續 v1.0 的 HTTP `X-Trace-Id` 基線：trace id 會寫入 submit request 的 `metadata.trace_id`，並同步到 `execution_plans.trace_id`、`execution_plan_steps.trace_id`、`run_checkpoints.trace_id`、`replay_attempts.trace_id`、trace API 與 replay bundle。`agent.requests` 的 `payload.request.metadata.trace_id` 仍會傳給 worker；release gate snapshot 也會保存建立當下的 trace id。`BaseEvent.trace_id` 目前由 event helper 另行產生 UUID；後續若導入完整 OpenTelemetry，應把 API response header、Kafka envelope、logs 與 database 的 trace id 對齊。

`v1.4.0` 延續 `execution_plan_steps`，並新增 `run_checkpoints` 與 `replay_attempts`；Kafka event envelope 尚未直接內嵌完整 plan、step audit、checkpoint 或 replay bundle，worker 會用 `run_id` 從 Postgres 讀取對應 execution plan。

## 資料生命週期建議

### Hot Data

近期 run state、idempotency key、tool circuit breaker 目前已放 Redis，TTL 依用途設定 5 分鐘到 24 小時。Approval 的長期決策、TTL 與 reviewer event 會寫入 Postgres audit trail。

### Audit Data

policy decision、execution summary、evaluation summary、incident 已進 Postgres，保留期可依合規需求設定。

### Artifacts

程式碼 diff、test report、完整 tool output、模型輸出全文可能太大，不建議全部塞進 `agent_runs.result`。目前保存 bounded tool transcript、tool execution audit 與 artifact refs；正式環境可加入 object storage，Postgres 只存 artifact URI 與 hash。

## 隱私與資料保護

- Prompt 可能包含敏感資料，正式環境需做 PII detection 與 redaction。
- Log 中不應輸出完整 prompt 或 tool output。
- `metadata` 欄位應避免存 access token、password、private key。
- Audit retention 到期後需要清除或匿名化。
## V1.3 Trace / Replay Data Model 補充

V1.3 在 `execution_plans` 增加：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `trace_id` | string | 沿用 HTTP `X-Trace-Id`，用來串起 run、plan 與 step audit。 |
| `trace_metadata` | JSON | 內部 Trace Contract metadata，保留 `exporter_type`、`external_trace_id`、`span_id`、`parent_span_id` 與未來 exporter 提示。 |

V1.3 新增 `execution_plan_steps`：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `step_id` | string | plan step id，例如 `policy-check`。 |
| `plan_id` / `run_id` / `tenant_id` | string | 對應 execution plan、run 與 tenant。 |
| `trace_id` | string | 與 `execution_plans.trace_id` 一致。 |
| `step_type` | string | `policy_check`、`approval_gate`、`tool_execution`、`evaluation` 等。 |
| `status` | string | `completed`、`planned`、`pending`、`blocked` 或 `failed`。 |
| `started_at` / `finished_at` / `duration_ms` | timestamp / integer | step audit 的時間欄位；尚未執行的 step 可為空。 |
| `input_summary` / `output_summary` / `error_summary` | text | 給 Dashboard、稽核與 replay bundle 使用的摘要。 |
| `metadata_payload` | JSON | step-level span/exporter metadata。 |
