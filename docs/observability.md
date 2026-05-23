# Observability

本專案已提供 Prometheus metrics 與 structured logging 的基礎。正式環境應補上 tracing、log correlation、alert rules 與 dashboard。

## Metrics

Endpoint：

```text
GET /v1/metrics
```

現有 metrics：

| Metric | Type | Labels | 說明 |
| --- | --- | --- | --- |
| `agent_requests_total` | Counter | `status` | 依 response status 統計 API requests |
| `agent_policy_risk_score` | Histogram | 無 | policy risk score 分布 |
| `agent_evaluation_final_score` | Histogram | 無 | evaluation final score 分布 |

## 建議新增 Metrics

API：

- `http_request_duration_seconds`
- `http_requests_total{method,path,status}`
- `agent_policy_decisions_total{decision}`
- `agent_policy_flags_total{flag}`

Worker：

- `agent_worker_jobs_total{status}`
- `agent_worker_duration_seconds`
- `agent_worker_retries_total`
- `agent_worker_dlq_total`

Kafka：

- consumer lag。
- publish failures。
- retry count。

Safety：

- escalation rate。
- deny rate。
- incident count by severity。
- average risk score。
- evaluator review rate。

## Logs

目前 `orchestrator.py` 在 policy block 時會使用 `structlog` 記錄：

- `run_id`
- `trace_id`
- `reason`
- `risk_score`
- `flags`

正式環境建議所有 request 與 worker job 都帶：

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

## Tracing

建議導入 OpenTelemetry，trace span 可切成：

- API request。
- prompt sanitation。
- risk scoring。
- policy evaluation。
- Kafka publish。
- worker consume。
- tool execution。
- evaluation。
- persistence。

`trace_id` 應進入 response header、event envelope、logs 與 database。

## Alert 建議

| Alert | 條件範例 | 可能原因 |
| --- | --- | --- |
| High deny rate | deny rate > baseline 3 倍 | prompt attack、policy 設太嚴、上游異常 |
| High escalation rate | escalation rate > 30% | false positive、使用者行為改變、攻擊 |
| Kafka lag high | lag 持續增加 10 分鐘 | worker 掛掉、執行器太慢 |
| Incident spike | medium+ incidents 突增 | evaluator 偵測大量異常 |
| Metrics endpoint down | scrape failed | API 不健康 |
| Risk score drift | 平均風險快速上升 | 需求型態變化或攻擊 |

## Dashboard 建議

營運 dashboard：

- request rate。
- latency。
- status distribution。
- error rate。
- Kafka lag。
- worker throughput。

安全 dashboard：

- allow/escalate/deny trend。
- top policy flags。
- incident severity trend。
- evaluator final score distribution。
- review queue size。
