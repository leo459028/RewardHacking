# Observability v0.2

本專案目前提供 Prometheus metrics 與 structured logging 的基礎。Dashboard 會從 Postgres audit trail 讀取 runs/incidents；Prometheus metrics 仍由 `/v1/metrics` 提供。

目前 observability 基線：`v0.2.0`

## Metrics Endpoint

```text
GET /v1/metrics
```

現有 metrics：

| Metric | Type | Labels | 說明 |
| --- | --- | --- | --- |
| `agent_requests_total` | Counter | `status` | 依 response status 統計 `/v1/agent/submit` 與 `/v1/agent/submit-with-details` |
| `agent_policy_risk_score` | Histogram | 無 | policy risk score 分布 |
| `agent_evaluation_final_score` | Histogram | 無 | detailed submit 的 evaluation final score 分布 |

注意：`agent_evaluation_final_score` 目前只在 `/v1/agent/submit-with-details` 中更新；兩個 submit endpoint 都會更新 request count 與 policy risk score。

## Dashboard Data

內建 Dashboard 使用 Postgres audit tables，不直接讀 Prometheus：

- `agent_runs`
- `incidents`

主要畫面：

- `/dashboard`：summary、recent runs、recent incidents。
- `/dashboard/runs`：run list 與 filters。
- `/dashboard/runs/{run_id}`：request、policy、execution output、evaluation summary、incidents。
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
- `agent_policy_decisions_total{decision}`
- `agent_policy_flags_total{flag}`
- `agent_incidents_total{severity,code}`

Database：

- Postgres connection failures。
- audit persistence duration。
- audit persistence failures。

Worker（Phase 3 後）：

- `agent_worker_jobs_total{status}`
- `agent_worker_duration_seconds`
- `agent_worker_retries_total`
- `agent_worker_dlq_total`
- Kafka consumer lag。

Safety：

- escalation rate。
- deny rate。
- evaluator review rate。
- average risk score。

## Tracing

建議導入 OpenTelemetry，trace span 可切成：

- API request。
- prompt sanitation。
- risk scoring。
- policy evaluation。
- dummy/tool execution。
- evaluation。
- audit persistence。
- dashboard query。
- future：Kafka publish、worker consume。

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
| Future Kafka lag high | lag 持續增加 10 分鐘 | worker 掛掉、執行器太慢 |

## Dashboard 建議

目前 Dashboard 已涵蓋：

- total runs。
- completed / escalated / denied / queued。
- average risk score。
- recent runs。
- recent incidents。
- run detail。
- playground。

後續可補：

- time-series trend。
- top policy flags。
- incident severity trend。
- evaluator final score distribution。
- review queue。
- tenant/user filters。
