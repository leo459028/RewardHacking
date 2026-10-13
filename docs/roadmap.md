# Project Roadmap: Evolutionary Milestones

> **Roadmap Baseline**: `v1.5.2`  
> **Status**: Active Open-Source Track  
> **Next Target**: `v1.6.0` (Safe MCP Gateway & Token Broker)

This roadmap outlines the past development trajectory and forward-looking engineering milestones for the **RewardHacking** platform.

---

## Release Cadence & Timeline

| Release | Status | Focus Area | Description |
| --- | --- | --- | --- |
| `v0.2.0` | Completed | Synchronous Prototype | Initial API endpoints, SQLite/Postgres audit trail, and server-rendered HTML dashboard. |
| `v0.3.0` | Completed | Guarded Async Execution MVP | Decoupled Kafka worker queues, Redis run state, bounded executor, and human approval queues. |
| `v0.4.0` | Completed | Execution Hardening | Worker retries with DLQ, approval TTL timers, tool execution audit tables, and circuit breakers. |
| `v0.5.0` | Completed | Evaluator & Observability | Task-specific evaluator rubrics, synthetic judge signals, Prometheus metrics, and SLO tracking. |
| `v0.6.0` | Completed | Tenant Governance MVP | API key auth, Dashboard Basic Auth, Redis sliding-window tenant quotas, and Governance Dashboard. |
| `v0.7.0` | Completed | Tenant Governance Center | DB-backed tenant settings, hashed API keys, RBAC-lite roles, and tenant quota overrides. |
| `v0.8.0` | Completed | Operations Gate | Operations Dashboard, deployment readiness checks, operational evidence logging, and `X-Trace-Id`. |
| `v1.0.0` | Completed | Production Readiness Baseline | Fail-closed release gates in `APP_ENV=prod`, safety regression evidence, and red-team regression corpus. |
| `v1.1.0` | Completed | Classical Chinese Prompt Guard | Specialized deterministic detector for literary and classical Chinese adversarial jailbreaks. |
| `v1.2.0` | Completed | Control Plane Execution Plan | Pre-execution planning, workflow definitions, and tool registry snapshots before task queueing. |
| `v1.3.0` | Completed | Trace & Replay Readiness | Step-level audit persistence, distributed trace propagation, and self-contained Replay Bundle exports. |
| `v1.4.0` | Completed | Checkpoint / Resume / Replay MVP | Submit checkpoints, resume checkpoint re-dispatching, and non-destructive dry-run replay attempts. |
| `v1.5.0` | Completed | Agent FinOps Core / Cost Ledger | Standardized model pricing catalog, immutable usage/cost ledger, and budget violation tracking. |
| `v1.5.1` | Completed | Pricing Seed Importer & Admin UI | Ingestion of official model pricing YAML seeds, dry-run validation, and Admin FinOps pricing portal. |
| `v1.5.2` | **Current** | **Open-Source & Codex OSS Baseline** | Full English internationalization, Apache-2.0 licensing, community governance standards, secret scan automation. |
| `v1.6.0+`| Planned | Safe MCP Gateway & Token Broker | Safe Model Context Protocol (MCP) gateway, dynamic token brokers, microVM tool sandboxing (gVisor). |

---

## Current Release: `v1.5.2` Open-Source Distribution

The `v1.5.2` milestone prepares the project for global open-source adoption and aligns with the **OpenAI Codex for Open Source** program:
1. **Full English Technical Documentation**: Complete rewrite of the entire `docs/` hierarchy and root `README.md`.
2. **Community Governance Standards**: Formal adoption of Apache License 2.0, `CONTRIBUTING.md`, Contributor Covenant `CODE_OF_CONDUCT.md` v2.1, and responsible disclosure `SECURITY.md`.
3. **Automated Secret Scanning**: Integration of `scripts/security_check.py` and GitHub Actions CI checks to prevent credential leakage.
4. **Operations Gate Version Bump**: Operations release gate default set to `v1.5.2`.

---

## Future Horizon: `v1.6.0` and Beyond

- **Safe MCP Gateway**: Validating Model Context Protocol tool declarations and enforcing schema boundaries before invocation.
- **Dynamic Token Broker**: Predictive token leasing and dynamic model downgrade routing based on tenant budget burn rates.
- **MicroVM Process Sandboxing**: Replacing simulated bounded executor runs with isolated ephemeral environments (e.g., Firecracker or gVisor).
- **OpenTelemetry Standard Exporter**: Direct OTLP span forwarding to Jaeger, Grafana Tempo, or LangSmith.
