# Kafka Topics

Kafka 在此專案中扮演 event bus，用來解耦 API ingress、worker execution、evaluation 與 incident processing。`v0.3.0` 起 API 會在低風險 request 建立 `queued` run 後 publish `agent.requests`，worker 會消費該 topic 並寫回 Postgres audit trail。retry/backoff、DLQ 與 lag alert 尚未納入本版。

## Topic 清單

| Topic | Producer | Consumer | 說明 |
| --- | --- | --- | --- |
| `agent.requests` | API / Approval | Worker | policy allow 或 approve once 後的待執行任務 |
| `agent.decisions` | API / Worker | Audit / UI | policy decision event |
| `agent.executions` | Worker | Evaluator / Audit | tool execution result |
| `agent.evaluations` | Evaluator / Worker | UI / Alerting / Audit | evaluation result |
| `agent.incidents` | API / Worker / Evaluator | Incident processor / Alerting | policy 或 evaluation 產生的事件 |

## Event Envelope

建議所有 topic 使用一致 envelope：

```json
{
  "event_id": "uuid",
  "trace_id": "uuid",
  "run_id": "uuid",
  "event_type": "policy.decision",
  "ts": "2026-05-17T00:00:00Z",
  "payload": {}
}
```

## `agent.requests`

用途：policy engine 已允許任務後，或 operator approve once 後，送給 worker 執行。

建議 key：`run_id`

Payload 範例：

```json
{
  "user_id": "u-1001",
  "task_type": "code_fix",
  "prompt": "Fix the bug.",
  "goal": "Return a correct patch",
  "allowed_tools": ["read_repo", "write_patch", "run_tests"],
  "constraints": {
    "no_external_network": true,
    "requires_human_approval": false,
    "max_tool_calls": 8,
    "allow_destructive_actions": false,
    "allow_pii_access": false
  },
  "metadata": {}
}
```

## `agent.decisions`

用途：保存 policy decision，讓 UI、audit pipeline 或安全團隊能觀察 allow/escalate/deny 分布。

Payload 範例：

```json
{
  "decision": "escalate",
  "reason": "One or more policy flags require review.",
  "risk_score": 0.7,
  "flags": ["external_network_allowed"],
  "requires_human_review": true
}
```

## `agent.executions`

用途：記錄 bounded executor 的結果摘要。正式環境避免把大型 artifact 或敏感 output 直接放入 Kafka，應改存 artifact store，payload 只放 URI、hash、summary。

Payload 範例：

```json
{
  "status": "completed",
  "output_preview": "Executed code_fix with 3 bounded tool steps.",
  "artifact_refs": []
}
```

## `agent.evaluations`

用途：記錄 evaluator 對結果的判斷。

Payload 範例：

```json
{
  "run_id": "uuid",
  "goal_alignment": 0.75,
  "shortcut_risk": 0.0,
  "hallucination_risk": 0.1,
  "policy_violation_risk": 0.0,
  "final_score": 0.78,
  "verdict": "pass",
  "explanation": "goal_alignment=0.75, shortcut_risk=0.00"
}
```

## `agent.incidents`

用途：記錄需要後續追蹤的安全、品質或合規事件。

Payload 範例：

```json
{
  "severity": "medium",
  "code": "eval_review_required",
  "detail": "shortcut_risk=0.30, policy_violation_risk=0.25"
}
```

## Partition 與 Ordering

建議使用 `run_id` 作為 key，讓同一個 run 的 events 進入同一 partition，保留相對順序。若未來需要 tenant-level ordering，可用 `tenant_id:run_id` 或另外設計 routing key。

## Retention 建議

- `agent.requests`：短 retention，例如 1 到 7 天，因為正式資料應落 DB。
- `agent.decisions` / `agent.evaluations` / `agent.incidents`：依 audit 需求 7 到 30 天。
- 大型 artifact 不進 Kafka。

## 錯誤處理建議

正式 worker 應加入：

- retry with exponential backoff。
- dead-letter topic，例如 `agent.requests.dlq`。
- idempotency by `event_id` 或 `run_id`。`v0.3.0` 已用 Redis idempotency key 避免同一 run 重複執行。
- schema version，例如 payload 加上 `schema_version`。
- consumer lag alert。
