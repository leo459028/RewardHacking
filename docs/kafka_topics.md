# Kafka Event Topics & Schema Contracts

> **Contract Version**: `v1.5.2`  
> **Broker Client**: `aiokafka`  
> **Default Cluster**: `localhost:9092`

Apache Kafka serves as the distributed event backbone in RewardHacking, decoupling synchronous API ingress from long-running agent tool invocations, policy evaluations, and incident processing.

---

## Event Topic Catalog

| Topic Name | Primary Producer | Primary Consumer | Event Description |
| --- | --- | --- | --- |
| `agent.requests` | API Gateway / Approval Queue | Background Worker | Queued tasks approved by policy engine or human operator. |
| `agent.decisions`| API Gateway / Worker | Audit Trail / UI | Emitted whenever a policy verdict (`allow`, `escalate`, `deny`) occurs. |
| `agent.executions`| Worker Bounded Executor | Evaluator / Audit | Emitted upon completion of bounded tool calls with transcript summary. |
| `agent.evaluations`| Evaluator / Worker | Alerting / Audit | Emitted when task evaluator scores rubric compliance. |
| `agent.incidents` | Policy Engine / Evaluator | Incident Service | Security breaches, policy denials, and jailbreak detections. |

---

## Standard Event Envelope

All Kafka messages conform to the standardized envelope generated via `app/workers/producer.py`:

```json
{
  "event_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "trace_id": "trace_a1b2c3d4e5f67890",
  "run_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "event_type": "policy.decision",
  "ts": "2026-10-13T10:00:00Z",
  "payload": {
    "run_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "tenant_id": "default",
    "status": "queued",
    "decision_reason": "Low risk task permitted for execution."
  }
}
```

---

## Consumer Guarantees & Fail-Closed Logic

1. **Partition Key**: All events published to `agent.requests` use the unique `run_id` as the message key, ensuring strict sequential processing for a given agent task.
2. **Missing Plan Fail-Closed**: When a worker consumes a request from `agent.requests`, it verifies the existence of the corresponding pre-execution plan in PostgreSQL. If the plan is missing or corrupted, the worker halts execution, reverts the task to `awaiting_approval`, and raises a `missing_execution_plan` security flag.
3. **At-Least-Once Delivery**: Workers process tasks idempotently by checking and locking run state in Redis prior to mutating tool environments.
