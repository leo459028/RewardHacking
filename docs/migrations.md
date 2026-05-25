# Migration 指南 v0.8

本專案已導入 Alembic，初始 migration 會建立：

目前 migration baseline：`v0.8.0`

- `agent_runs`
- `incidents`

目前 head 會一路套用到 `202605250002`。`v0.5.0` 在原本 `agent_runs`、`incidents` 之外，新增 worker、tool、approval 與 evaluation audit tables。`v0.6.0` 新增 `agent_runs.tenant_id`，既有資料 backfill 為 `default`，並建立 `governance_events` audit table。`v0.7.0` 新增 `tenants` 與 `tenant_api_keys`，並從既有 `agent_runs.tenant_id` backfill tenant 設定。`v0.8.0` 新增 `operations_evidence`、`release_gate_runs` 與 `release_gate_items`，用於保存 release readiness、營運證據與 release gate snapshot。

Migration 檔案位於：

```text
migrations/versions/202605190001_create_audit_tables.py
migrations/versions/202605240001_add_v04_v05_audit_tables.py
migrations/versions/202605240002_add_v06_tenant_governance.py
migrations/versions/202605250001_add_v07_tenant_governance_center.py
migrations/versions/202605250002_add_v08_operations_gate.py
```

## 常用指令

套用 migration：

```bash
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
- `users` 管理表與完整 OAuth/SSO RBAC 關聯
- audit retention / purge migration 策略
