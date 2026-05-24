# Redis Keys

Redis 用於短期 state、cache、approval token、rate limit、circuit breaker 與 worker coordination。`v0.3.0` 起已接入最小 API/worker flow：run state、idempotency key 與 tool circuit breaker。rate limit、tenant quota 與完整 approval token TTL 仍在後續版本。

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
| `tenant:{tenant_id}:quota` | 租戶 quota/rate limit | 依 quota window |
| `tenant:{tenant_id}:risk_window` | 近期風險統計 | 5 分鐘到 1 小時 |
| `breaker:{tool_name}` | tool circuit breaker | 5 到 30 分鐘 |
| `risk:{run_id}` | risk score 快取 | 1 到 24 小時 |
| `idempotency:{event_id}` | event 去重 | 1 到 7 天 |

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

`v0.3.0` 的實作會把命中的 breaker 加入 policy flags，例如 `tool_circuit_breaker_open:read_data`，並讓 run 進入 `awaiting_approval`。這是 fail closed 行為：operator 暫停工具後，任務不會繼續執行該工具。

## Rate Limit 建議

可用 Redis 記錄：

- 每個 user/tenant 每分鐘 request 數。
- 每個 tenant 每小時高風險任務數。
- 每個 tool 每分鐘呼叫數。

超過限制時，API 應回傳明確錯誤或 `escalate`，而不是靜默丟棄。

目前 `v0.5.0` 尚未實作 rate limit；此段保留為 `v0.6+` 設計方向。

## 運維注意事項

- 正式環境要啟用 auth/TLS 或部署在私有網路。
- Redis 不應作為唯一 audit store。
- 重要決策仍需落 Postgres 或長期 event storage。
- 設定 memory policy 前要確認 key TTL 與 eviction 行為。
