# Redis Keyspace & Rate Limiting

> **Specification Version**: `v1.5.2`  
> **Redis Client**: `redis-py` (asyncio)  
> **Default URI**: `redis://localhost:6379/0`

Redis serves as the high-throughput, low-latency ephemeral datastore in **RewardHacking**, handling runtime state caching, sliding-window rate limiting, and tool circuit breakers.

---

## Keyspace Convention

- **Delimiter**: Colon `:` separated hierarchical namespaces.
- **Tenant Prefix**: Multi-tenant keys always prefix with `tenant:{tenant_id}:`.
- **Mandatory TTL**: All temporary keys must have an explicit Time-To-Live (TTL) to avoid memory leaks.
- **PII Exclusion**: Raw prompts, personally identifiable information, or credentials must never be written to Redis.

---

## Key Catalog

| Key Pattern | Type | Recommended TTL | Description |
| --- | --- | --- | --- |
| `run:{run_id}:state` | String (JSON) | 86,400s (24h) | Current run lifecycle state (`queued`, `running`, `completed`) |
| `run:{run_id}:approval` | String (JSON) | 3,600s (1h) | Pending approval session state and reviewer claim |
| `tenant:{tenant_id}:quota:requests:{YYYYMMDDHHMM}` | String (Int) | 120s | Sliding-window request counter per minute |
| `tenant:{tenant_id}:quota:high_risk:{YYYYMMDDHH}` | String (Int) | 7,200s (2h) | Hourly budget counter for high-risk operations |
| `breaker:{tool_name}` | String (JSON) | 300s - 1,800s | Tool circuit breaker state (`open`, `closed`, `half_open`) |
| `idempotency:run:{run_id}:worker:{attempt}` | String | 86,400s (24h) | Worker execution attempt deduplication lock |

---

## Circuit Breaker Semantics

The circuit breaker halts invocations of a malfunctioning or abused tool:
- **State `open`**: All agent tasks requesting the tool are immediately rejected with error code `tool_circuit_breaker_tripped`.
- **Tripping Condition**: When consecutive failures for a given tool exceed configured thresholds within a 5-minute sliding window.
- **Manual Reset**:
  ```bash
  redis-cli del "breaker:{tool_name}"
  ```
