# Redis Keyspace & Rate Limiting

> **Specification Version**: `v1.5.7`
> **Redis Client**: `redis-py` (asyncio)
> **Default URI**: `redis://localhost:6379/0`

Redis serves as the high-throughput, low-latency ephemeral datastore in **RewardHacking**, handling runtime state caching, sliding-window rate limiting (tenant, user, and tool scopes), Dynamic Token Broker budget leases, and tool circuit breakers.

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
| `tenant:{tenant_id}:quota:requests:{YYYYMMDDHHMM}` | String (Int) | 120s | Sliding-window tenant request counter per minute |
| `tenant:{tenant_id}:quota:high_risk:{YYYYMMDDHH}` | String (Int) | 7,200s (2h) | Hourly budget counter for high-risk operations per tenant |
| `tenant:{tenant_id}:user:{user_id}:quota:requests:{YYYYMMDDHHMM}` | String (Int) | 120s | Sliding-window per-user request counter per minute (`USER_REQUEST_LIMIT_PER_MINUTE`) |
| `tenant:{tenant_id}:tool:{tool_name}:quota:calls:{YYYYMMDDHHMM}` | String (Int) | 120s | Sliding-window per-tool invocation counter per minute (`TOOL_CALL_LIMIT_PER_MINUTE`) |
| `tenant:{tenant_id}:lease:{run_id}` | String (JSON) | 900s (default) | Dynamic Token Broker pre-execution budget lease (`FINOPS_TOKEN_LEASE_TTL_SECONDS`) |
| `circuit:tool:{tool_name}` | String (`open`/`closed`) | 300s (default) | Tool circuit breaker state (`TOOL_BREAKER_TTL_SECONDS`) |
| `breaker:{tool_name}:failures` | String (Int) | 300s | Consecutive worker failure counter for automatic circuit breaker tripping (`TOOL_BREAKER_FAILURE_THRESHOLD`) |
| `tenant:{tenant_id}:quota:mcp:{mcp_server_id}:calls:{YYYYMMDDHHMM}` | String (Int) | 120s | `v1.5.7` per-MCP-server fixed-window (minute bucket) call counter; one increment per requested tool bound to the server (`quota_per_minute` or `MCP_SERVER_CALL_LIMIT_PER_MINUTE`) |
| `circuit:mcp:{mcp_server_id}` | String (JSON) | `TOOL_BREAKER_TTL_SECONDS` | `v1.5.7` MCP server circuit breaker; kept in a separate namespace so tool breaker listings (`breaker:*`) never include MCP servers |
| `idempotency:run:{run_id}:worker:{attempt}` | String | 86,400s (24h) | Worker execution attempt deduplication lock |

---

### Keys Removed by a Retention Purge (`v1.6.0`)

Applying a retention plan deletes, best effort, the purged run's `run:{run_id}:state`, `run:{run_id}:approval` and `idempotency:run:{run_id}:*` keys. Token lease keys are never touched (lease state belongs to the FinOps broker). These keys expire within 24 hours anyway, so runs older than the retention period normally have none.

---

## Circuit Breaker Semantics

The circuit breaker halts unsupervised invocations of a malfunctioning or abused tool:
- **State `open`**: Agent tasks requesting a tool with an open breaker are flagged with `tool_circuit_breaker_open` and escalated to `awaiting_approval` for human operator review before execution.
- **Tripping Condition**: Automatically tripped when consecutive worker failures for a given tool reach `TOOL_BREAKER_FAILURE_THRESHOLD` (default `3`), or manually tripped via `POST /v1/control-plane/tools/{tool_name}/breaker/open` or `/dashboard/control-plane`.
- **Resetting Condition**: Automatically cleared on successful worker execution or manually reset via `POST /v1/control-plane/tools/{tool_name}/breaker/close` or `/dashboard/control-plane`:
  ```bash
  redis-cli del "circuit:tool:{tool_name}" "breaker:{tool_name}:failures"
  ```
