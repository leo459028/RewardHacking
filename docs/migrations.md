# Migration 指南 v1.5.1

本專案已導入 Alembic，初始 migration 會建立：

目前 migration baseline：`v1.5.1`

- `agent_runs`
- `incidents`

目前 head 會一路套用到 `202606010004`。`v0.5.0` 在原本 `agent_runs`、`incidents` 之外，新增 worker、tool、approval 與 evaluation audit tables。`v0.6.0` 新增 `agent_runs.tenant_id`，既有資料 backfill 為 `default`，並建立 `governance_events` audit table。`v0.7.0` 新增 `tenants` 與 `tenant_api_keys`，並從既有 `agent_runs.tenant_id` backfill tenant 設定。`v0.8.0` 新增 `operations_evidence`、`release_gate_runs` 與 `release_gate_items`，用於保存 release readiness、營運證據與 release gate snapshot。`v1.0.0` 沒有新增資料表或 migration，`safety_regression` 沿用 `operations_evidence.evidence_type` 保存。`v1.1.0` 沒有新增 migration；Prompt Guard 的 language、flags 與 signals 保存於既有 `agent_runs.evaluation_summary.prompt_guard`。`v1.2.0` 新增 `workflow_definitions`、`tool_definitions` 與 `execution_plans`，用於 Control Plane registry 與 run-level execution plan。`v1.3.0` 新增 `execution_plans.trace_id`、`execution_plans.trace_metadata` 與 `execution_plan_steps`，用於 Trace / Replay Readiness。`v1.4.0` 新增 `run_checkpoints` 與 `replay_attempts`，用於 Checkpoint / Resume / Replay MVP。`v1.5.0` 新增 `pricing_catalog`、`usage_events`、`cost_events`、`budget_policies` 與 `budget_violations`，用於 FinOps 成本帳本。`v1.5.1` 擴充 FinOps schema，並新增 `pricing_import_batches`、`pricing_change_logs` 與 `pricing_source_documents`。

Migration 檔案位於：

```text
migrations/versions/202605190001_create_audit_tables.py
migrations/versions/202605240001_add_v04_v05_audit_tables.py
migrations/versions/202605240002_add_v06_tenant_governance.py
migrations/versions/202605250001_add_v07_tenant_governance_center.py
migrations/versions/202605250002_add_v08_operations_gate.py
migrations/versions/202605310001_add_v12_control_plane.py
migrations/versions/202606010001_add_v13_trace_replay.py
migrations/versions/202606010002_add_v14_checkpoints_replay.py
migrations/versions/202606010003_add_v15_finops_core.py
migrations/versions/202606010004_expand_v151_finops_core.py
```

## 常用指令

套用 migration：

```bash
alembic upgrade head
```

## V1.5 Migration

使用的 migration：
- `202606010003_add_v15_finops_core.py`

變更內容：
- 新增 `pricing_catalog`，保存 deterministic pricing catalog。
- 新增 `usage_events`，保存 submit、worker completion、resume、replay 與 manual usage records。
- 新增 `cost_events`，保存 estimated / actual cost ledger。
- 新增 `budget_policies`，保存 tenant/run/workflow/model budget policy metadata；V1.5 實作 run scope 檢查。
- 新增 `budget_violations`，保存 run budget exceeded 與 missing pricing rule 等可查詢事件。

驗證命令：
```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

如果是在 Docker stack 內驗證，建議從 API container 執行，確保使用 container network hostnames：

```bash
docker compose exec -T api alembic upgrade head
```

查看目前版本：

```bash
alembic current
```

建立新 migration：

```bash
alembic revision --autogenerate -m "describe change"
```

回退一版：

```bash
alembic downgrade -1
```

## 設定來源

`migrations/env.py` 會讀取 `app.core.config.get_settings()`，使用 `POSTGRES_DSN` 作為 migration 連線字串。

目前設定分工：

- 主機端 `.env` / `.env.example` 使用 `localhost:5432`，讓 `alembic upgrade head` 可以直接從 PowerShell 執行。
- Docker Compose 的 `api` 與 `worker` service 會覆蓋成 `postgres:5432`，讓容器內服務透過 Docker network 連線。

Dockerfile 會在啟動 API 前執行：

```bash
alembic upgrade head
```

因此 Docker Compose 啟動時會先建立 audit tables，再啟動 `uvicorn`。

## Migration 規範

- 每次 schema 變更都要有 migration。
- migration 應在 CI 中驗證可從空 DB 套到 head。
- destructive schema change 需要 data backfill plan。
- 欄位刪除採兩階段：
  - 先讓程式不再讀寫。
  - 確認無使用後再移除欄位。
- event payload schema 應保持 backward compatible。

## Rollback Playbook

每個 migration PR 應附：

- 變更摘要。
- 是否 backward compatible。
- rollback 指令或 forward-fix 方式。
- 預估資料量與鎖表風險。
- 是否需要維護視窗。

## 後續建議

正式環境除了現有 models，可考慮新增或強化：

- `policy_decisions`
- `artifacts`
- `execution_plan_steps`（若後續需要 step-level 查詢或 replay）
- `run_checkpoints` / `replay_attempts` 的外部 artifact/object storage 關聯
- `users` 管理表與完整 OAuth/SSO RBAC 關聯
- audit retention / purge migration 策略
## V1.3 Migration

最新 migration：

- `202606010001_add_v13_trace_replay.py`

變更內容：

- `execution_plans` 新增 `trace_id` 與 `trace_metadata`。
- 新增 `execution_plan_steps`，保存 step-level audit、時間欄位、摘要與 trace/exporter metadata。

升級指令：

```bash
alembic upgrade head
```

## V1.4 Migration

最新 migration：

- `202606010002_add_v14_checkpoints_replay.py`

變更內容：

- 新增 `run_checkpoints`，保存 submit/resume checkpoint、resume cursor 與 state payload。
- 新增 `replay_attempts`，保存 dry-run replay attempt、result payload 與 limitations。

升級指令：

```bash
alembic upgrade head
```
## V1.5.1 Migration 補充

目前 V1.5.1 head 會套用到：

```text
202606010004_expand_v151_finops_core
```

此 migration 會在 V1.5 原有 `pricing_catalog`、`usage_events`、`cost_events`、`budget_policies`、`budget_violations` 上補齊 V1.5.1 欄位，並新增：

- `pricing_import_batches`
- `pricing_change_logs`
- `pricing_source_documents`

驗證命令：

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

若使用 Docker，請先開啟 Docker Desktop，再執行：

```bash
docker compose exec -T api alembic upgrade head
```

完整欄位清單請看 [`docs/v1.5.1_finops_core.md`](v1.5.1_finops_core.md)。
