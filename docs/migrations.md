# Database Migrations Guide

> **Migration Baseline**: `v1.5.2`  
> **Tool**: Alembic  
> **Current Head Revision**: `202606010004` (`expand_v151_finops_core`)

RewardHacking employs Alembic to manage relational schema evolutions across PostgreSQL audit tables. The `v1.5.2` release maintains strict schema compatibility with revision `202606010004`.

---

## Migration Revision History

```text
migrations/versions/
├── 202605190001_create_audit_tables.py           (Baseline: agent_runs, incidents)
├── 202605240001_add_v04_v05_audit_tables.py      (Worker attempts, tool executions, approvals)
├── 202605240002_add_v06_tenant_governance.py     (Tenant ID backfill, governance events)
├── 202605250001_add_v07_tenant_governance_center.py (DB-managed tenants & hashed API keys)
├── 202605250002_add_v08_operations_gate.py       (Operations evidence & release gates)
├── 202605310001_add_v12_control_plane.py         (Workflow & tool registries, execution plans)
├── 202606010001_add_v13_trace_replay.py          (Trace metadata & step-level audit tables)
├── 202606010002_add_v14_checkpoints_replay.py    (Submit/resume checkpoints & replay attempts)
├── 202606010003_add_v15_finops_core.py           (FinOps pricing catalog, usage & cost events)
└── 202606010004_expand_v151_finops_core.py       (Head: Expanded pricing metadata & source audits)
```

---

## Standard Operations

### Apply Pending Migrations
```bash
alembic upgrade head
```

### Inspect Current Database Revision
```bash
alembic current
```

### Rollback Previous Migration
```bash
alembic downgrade -1
```

### Generating a New Migration
When altering SQLAlchemy models under `app/models/`:
```bash
alembic revision --autogenerate -m "describe_schema_change"
```

---

## Docker Execution Context

When running in containerized environments, invoke migrations inside the API container to leverage internal container network hostnames:

```bash
docker compose exec -T api alembic upgrade head
docker compose exec -T api alembic current
```
