# Redis Keys

Redis 用於短期 state、cache、approval token、rate limit、circuit breaker 與 worker coordination。`v1.4.0` 目前沿用既有 API/worker flow 的 run state、worker idempotency key、tool circuit breaker、tenant request quota 與 high-risk quota。Operations Gate、Prompt Guard、Control Plane Trace / Replay、Checkpoint、Resume 與 dry-run Replay 不新增 Redis key；release readiness、safety regression evidence、release gate snapshot、Prompt Guard summary、execution plan、step audit、checkpoint、replay attempt 與 replay bundle 皆以 Postgres audit tables 為準。Resume API 只會把 run state 重新設為 `queued` 並 publish worker event。

## Key 命名原則

- 使用冒號分段：`namespace:{id}:field`。
- 有 tenant 時把 tenant 放前面：`tenant:{tenant_id}:quota`。
- 所有短期狀態都應設定 TTL。
- 避免存完整 prompt、PII、secret 或大型 artifact。

## 建議 Key

| Key | 用途 | TTL 建議 |
| --- | --- | --- |
| `run:{run_id}:state` | run 當前狀態，例如 queued/running/completed | 1 到 24 小時 |
| `run:{run_id}:policy` | policy decision 快取 | 1 到 24 小時 |
| `run:{run_id}:evaluation` | evaluation summary 快取 | 1 到 24 小時 |
| `run:{run_id}:approval` | 人工審核 token 或狀態 | 15 分鐘到 2 小時 |
| `tenant:{tenant_id}:quota:requests:{YYYYMMDDHHMM}` | 租戶每分鐘 request quota | 約 60 秒 |
| `tenant:{tenant_id}:quota:high_risk:{YYYYMMDDHH}` | 租戶每小時 high-risk quota | 約 3600 秒 |
| `tenant:{tenant_id}:quota` | 舊版或延伸設計用的租戶 quota/rate limit | 依 quota window |
| `tenant:{tenant_id}:risk_window` | 近期風險統計 | 5 分鐘到 1 小時 |
| `breaker:{tool_name}` | tool circuit breaker | 5 到 30 分鐘 |
| `risk:{run_id}` | risk score 快取 | 1 到 24 小時 |
| `idempotency:run:{run_id}:worker:{attempt_number}` | worker attempt 去重 | 1 到 24 小時 |
| `idempotency:{event_id}` | 後續 event 去重延伸設計 | 1 到 7 天 |

## Value 範例

`run:{run_id}:state`

```json
{
  "status": "running",
  "updated_at": "2026-05-17T00:00:00Z",
  "worker_id": "worker-1",
  "attempt": 1
}
```

`breaker:{tool_name}`

```json
{
  "status": "open",
  "reason": "high_failure_rate",
  "opened_at": "2026-05-17T00:00:00Z"
}
```

## Circuit Breaker 建議

以下情況可打開 breaker：

- 某 tool error rate 突然升高。
- 某 tool 觸發大量 policy violation。
- 外部依賴不穩定，導致 worker backlog。
- 安全團隊手動暫停特定 tool。

Policy engine 在評估時可查 Redis breaker，若 tool 被停用，直接 `escalate` 或 `deny`。

目前實作會把命中的 breaker 加入 policy flags，例如 `tool_circuit_breaker_open:read_data`，並讓 run 進入 `awaiting_approval`。這是 fail closed 行為：operator 暫停工具後，任務不會繼續執行該工具。

## Rate Limit 建議

可用 Redis 記錄：

- 每個 user/tenant 每分鐘 request 數。
- 每個 tenant 每小時高風險任務數。
- 每個 tool 每分鐘呼叫數。

超過限制時，API 應回傳明確錯誤或 `escalate`，而不是靜默丟棄。

目前已實作 submit path 的 tenant request quota 與 high-risk quota。request quota 超過時回 `429 tenant_request_quota_exceeded`；high-risk quota 超過時會 fail closed 進入 `awaiting_approval`，並寫入 `governance_events`。若 `tenants` 資料表有該 tenant，quota limit 會優先使用 DB tenant 設定；找不到 tenant 時才使用 `.env` 全域預設。

## 運維注意事項

- 正式環境要啟用 auth/TLS 或部署在私有網路。
- Redis 不應作為唯一 audit store。
- 重要決策仍需落 Postgres 或長期 event storage。
- 設定 memory policy 前要確認 key TTL 與 eviction 行為。
