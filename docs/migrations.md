# Database Migrations Guide

> **Migration Baseline**: `v1.5.2`  
> **Tool**: Alembic  
> **Current Head Revision**: `202611120002` (`add_v160_runtime_foundations`)

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
├── 202606010004_expand_v151_finops_core.py       (Expanded pricing metadata & source audits)
├── 202611070001_add_v158_outcome_review_queue.py (run_outcomes, evaluation_reviews, tenant_evaluation_policies, evaluation_results evidence columns)
├── 202611080001_add_v159_benchmark_honeypot_attestation.py (benchmark_samples, benchmark_runs, honeypot_probes, honeypot_launches, tenant_attestation_keys; cohort / honeypot / attestation columns on run_outcomes, cohort / finding_labels / export link on evaluation_reviews, evaluation_results.suspicion_score)
├── 202611090001_add_v1510_judge_triage_sampling.py (judge_assessments, review_sampling_decisions; triage_priority / triage_score / triage_source / judge_assessment_id on evaluation_reviews; benchmark_runs.judge_config)
├── 202611100001_add_v1511_judge_profiles.py (judge_assessments.profile; tenant_evaluation_policies.allow_external_judge / external_judge_profiles)
├── 202611120001_add_v1512_judge_activation.py (judge_profile_configs, judge_profile_states)
└── 202611120002_add_v160_runtime_foundations.py (Head: legal_holds, retention_purges, retention_tombstones)
```

### Revision ID Rule (since `v1.6.0`)

Revision IDs look like dates (`YYYYMMDD` + a 4-digit serial), so they must not invent dates:

1. Use the **real creation date** plus serial `0001` (`YYYYMMDD0001`).
2. If that is not greater than the current head, keep the **head's date** and increment its serial (for example, head `202611120001` on 2026-11-09 gives `202611120002`). The gap closes once the calendar passes the head's date and never grows again.
3. The file name starts with the revision ID, `down_revision` is the previous head, and the docstring `Create Date` is the real creation time.

IDs therefore increase monotonically, file-name order equals execution order and there is one head. `202611080001`–`202611120001` (`v1.5.9`–`v1.5.12`) predate this rule and carry dates later than their real creation dates; already-applied revisions are never renamed. `tests/test_v160_migration_ids.py` enforces the rule for every revision from `202611120002` on.

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
alembic revision --autogenerate --rev-id <YYYYMMDDNNNN> -m "describe_schema_change"  # see Revision ID Rule
```

---

## Docker Execution Context

When running in containerized environments, invoke migrations inside the API container to leverage internal container network hostnames:

```bash
docker compose exec -T api alembic upgrade head
docker compose exec -T api alembic current
```
