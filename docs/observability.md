# Observability v1.3

## V1.3 Trace / Replay Observability 補充

V1.3 新增 metric：

| Metric | Type | Labels | 說明 |
| --- | --- | --- | --- |
| `agent_control_plane_steps_total` | Counter | `step_type`, `status` | 每次建立 execution plan 時，依 step type 與 status 記錄 step audit row 數量。 |

Trace 現況：HTTP `X-Trace-Id` 會進入 run request metadata、`execution_plans.trace_id`、`execution_plan_steps.trace_id`、`/v1/runs/{run_id}/trace` 與 replay bundle。`trace_metadata` 與 step `metadata` 已保留 `exporter_type=internal`、`external_trace_id`、`span_id`、`parent_span_id`，但 V1.3 尚未把資料實際送到 OTLP 或 LangSmith-style exporter。

本專案目前提供 Prometheus metrics 與 structured logging 的基礎。Dashboard 會從 Postgres audit trail 讀取 runs/incidents/governance events/operations evidence；Prometheus metrics 仍由 `/v1/metrics` 提供。`AUTH_ENABLED=true` 時，`/v1/metrics` 需要 `X-API-Key`，允許 `viewer/operator/admin` 讀取。

目前 observability 基線：`v1.3.0`

## Metrics Endpoint

```text
GET /v1/metrics
```

現有 metrics：

| Metric | Type | Labels | 說明 |
| --- | --- | --- | --- |
| `http_requests_total` | Counter | `method`、`path`、`status` | API HTTP request count |
| `http_request_duration_seconds` | Histogram | `method`、`path`、`status` | API HTTP request latency |
| `agent_requests_total` | Counter | `status` | 依 response status 統計 `/v1/agent/submit` 與 `/v1/agent/submit-with-details` |
| `agent_policy_risk_score` | Histogram | 無 | policy risk score 分布 |
| `agent_evaluation_final_score` | Histogram | 無 | detailed submit 的 evaluation final score 分布 |
| `agent_policy_decisions_total` | Counter | `status` | policy decision 對應的 run status |
| `agent_prompt_guard_flags_total` | Counter | `flag` | deterministic prompt analysis 命中的 prompt guard flags |
| `agent_control_plane_steps_total` | Counter | `step_type`、`status` | V1.3 step-level audit persistence counter |
| `agent_worker_jobs_total` | Counter | `status` | worker job 結果 |
| `agent_worker_duration_seconds` | Histogram | 無 | worker job duration |
| `agent_worker_retries_total` | Counter | 無 | worker retry count |
| `agent_worker_dlq_total` | Counter | 無 | worker DLQ count |
| `agent_evaluator_verdicts_total` | Counter | `verdict` | evaluator pass/review count |
| `agent_incidents_total` | Counter | `severity`、`code` | incident 建立時依嚴重度與代碼遞增 |

注意：`agent_evaluation_final_score` 目前只在 `/v1/agent/submit-with-details` 中更新；兩個 submit endpoint 都會更新 request count 與 policy risk score。

## Dashboard Data

內建 Dashboard 使用 Postgres audit tables，不直接讀 Prometheus：

- `agent_runs`
- `incidents`
- `worker_attempts`
- `tool_executions`
- `approval_events`
- `evaluation_results`
- `governance_events`
- `operations_evidence`
- `release_gate_runs`
- `release_gate_items`
- `execution_plans`
- `execution_plan_steps`

主要畫面：

- `/dashboard`：summary、recent runs、recent incidents。
- `/dashboard/runs`：run list 與 filters。
- `/dashboard/runs/{run_id}`：request、policy、execution output、evaluation summary、event timeline、tool transcript、approval state、incidents。
- `/dashboard/runs/{run_id}`：顯示 Prompt Guard 區塊，可查古文/文言式繞語攻擊的 language、flags 與 signals；v1.2 也會顯示 Execution Plan 區塊，可查 workflow version、selected tools、steps、approval gates 與 policy snapshot。
- `/dashboard/control-plane`：顯示 workflow registry、tool registry 與 latest execution plans。
- `/dashboard/approvals`：awaiting approval queue 與審核操作。
- `/dashboard/worker`：worker attempts、retry failure reason 與 DLQ。
- `/dashboard/observability`：worker、incident、approval、evaluator 與 SLO summary。
- `/dashboard/governance`：tenant runs、average risk、quota limited count 與 governance events。
- `/dashboard/operations`：release readiness、operations evidence 與 release gate snapshot。
- `/dashboard/playground`：送出測試 request。

## Logs

目前 `orchestrator.py` 在 policy block 時會使用 `structlog` 記錄：

- `run_id`
- `trace_id`
- `reason`
- `risk_score`
- `flags`

正式環境建議所有 request 都帶：

- `trace_id`
- `run_id`
- `user_id` 或 hashed user id。
- `tenant_id`
- `decision`
- `status`
- latency。

避免記錄：

- 完整 prompt。
- PII。
- secret。
- access token。
- 完整 tool output。

## 建議新增 Metrics

API：

- `http_request_duration_seconds`
- `http_requests_total{method,path,status}`
- `agent_policy_decisions_total{status}`：已加入，label 使用最終 run/request status。
- `agent_incidents_total{severity,code}`
- `agent_prompt_guard_flags_total{flag}`：已加入，label 使用固定 policy flag 名稱，避免使用 prompt 內容造成高 cardinality。

Database：

- Postgres connection failures。
- audit persistence duration。
- audit persistence failures。

Worker：

- `agent_worker_jobs_total{status}`：已加入。
- `agent_worker_duration_seconds`：已加入。
- `agent_worker_retries_total`：已加入。
- `agent_worker_dlq_total`：已加入。
- Kafka consumer lag。

Safety：

- escalation rate。
- deny rate。
- evaluator review rate。
- average risk score。
- tenant quota limited count。
- governance event count。

## Tracing

`v1.0.0` 延續 `X-Trace-Id` 基線：API 會沿用 request header 或產生 trace id，並回傳 response header；submit flow 會把 trace id 放入 request metadata 與 Kafka event payload；release gate snapshot 會保存建立當下的 trace id。後續仍建議導入 OpenTelemetry，trace span 可切成：

- API request。
- prompt sanitation。
- risk scoring。
- policy evaluation。
- prompt guard analysis。
- bounded tool execution。
- evaluation。
- audit persistence。
- dashboard query。
- Kafka publish。
- worker consume。

`trace_id` 應進入 response header、event envelope、logs 與 database。

## Alert 建議

| Alert | 條件範例 | 可能原因 |
| --- | --- | --- |
| API down | `/v1/healthz` failed | API process 不健康 |
| DB readiness failed | `/v1/readyz` failed | Postgres/DSN/migration 問題 |
| High deny rate | deny rate > baseline 3 倍 | prompt attack、policy 設太嚴、上游異常 |
| High escalation rate | escalation rate > 30% | false positive、使用者行為改變、攻擊 |
| Incident spike | medium+ incidents 突增 | evaluator 偵測大量異常 |
| Risk score drift | 平均風險快速上升 | 需求型態變化或攻擊 |
| Kafka lag high | lag 持續增加 10 分鐘 | worker 掛掉、執行器太慢 |

## Dashboard 建議

目前 Dashboard 已涵蓋：

- total runs。
- completed / escalated / denied / queued / running / awaiting approval / failed。
- average risk score。
- recent runs。
- recent incidents。
- run detail。
- playground。

目前已補：

- worker/DLQ summary。
- approval summary。
- evaluator pass/review summary。
- SLO target summary。
- tenant governance summary。
- operations readiness 與 release gate snapshot。

後續可補：

- 真正 time-series trend。
- top policy flags。
- incident severity trend。
- 更細的 tenant/user time-series filters。
- 真正的 OpenTelemetry span 與 Kafka consumer lag alert。

補充：v1.0 已讓 request path 在建立 incident 時遞增 `agent_incidents_total{severity,code}`；Dashboard incident 趨勢仍以 Postgres audit tables 與 summary 為主要資料來源。
