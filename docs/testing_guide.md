# 測試指南 v0.7

目前測試涵蓋 policy 周邊邏輯、API persistence、queued submit、worker processing、worker retry/DLQ、approval queue/TTL/request changes、Redis breaker、dashboard HTML response、tool execution audit、evaluator/observability behavior、tenant-aware audit、API key auth、tenant quota、governance API、Tenant Governance Center 與 Governance Dashboard。

目前測試基線：`v0.7.0`

## 執行測試

```bash
pytest -q
```

或：

```bash
make test
```

執行 lint：

```bash
ruff check app tests
```

或：

```bash
make lint
```

## 現有測試範圍

| 檔案 | 範圍 |
| --- | --- |
| `tests/test_policy_engine.py` | low-risk allow、高風險 escalate/deny |
| `tests/test_prompt_sanitizer.py` | prompt injection pattern 偵測 |
| `tests/test_risk_scoring.py` | risk score baseline、clamp |
| `tests/test_evaluator.py` | shortcut、policy bypass signal |
| `tests/test_api.py` | health/readyz、queued submit、worker update、approval API、breaker fail closed、incident、summary、dashboard pages |
| `tests/test_v04_v05.py` | worker attempts/DLQ、tool execution audit、approval TTL/request changes、evaluation results、observability dashboard |
| `tests/test_v06_governance.py` | tenant_id persistence/filter、API key auth、tenant mismatch、request quota、high-risk quota fail-closed、governance summary、Dashboard governance、Dashboard Basic Auth |
| `tests/test_v07_governance_center.py` | tenant CRUD、DB-managed API key 建立/撤銷、RBAC-lite、tenant scope、tenant quota override、Governance Dashboard tenant/API key 區塊 |

API tests 使用 SQLite in-memory database 覆寫 `get_session()`，並用 in-memory state store / event publisher 取代 Redis/Kafka，不需要啟動 Docker Compose。v0.6 quota 測試同時覆蓋 Redis state store 介面的 in-memory 實作，Docker smoke 則驗證實際 stack 可啟動。

## CI Gate

GitHub Actions 目前會執行：

```bash
ruff check app tests
pytest -q
```

workflow 位於 `.github/workflows/ci.yml`。

## 建議補強測試

- Alembic migration downgrade smoke test。
- auth-enabled Docker stack smoke test。
- Dashboard mobile/responsive interaction flow。
- request validation error envelope。
- red-team prompt corpus regression。
- Kafka consumer lag integration tests。
- 真實 OS sandbox integration tests。

## Manual Verification

1. 啟動服務：

```bash
docker compose up -d --build
```

2. 確認 API：

```bash
curl http://localhost:8000/v1/healthz
curl http://localhost:8000/v1/readyz
```

3. 開啟 Dashboard：

```text
http://localhost:8000/dashboard
```

4. 在 `/dashboard/playground` 送一筆低風險 request，確認 run 先是 `queued`，worker 處理後變成 `completed`，且可輸入 `tenant_id`。
5. 送一筆高風險或 destructive request，確認進入 `awaiting_approval` 或 `denied`。
6. 到 `/dashboard/approvals` 測試 approve once、deny 或 request changes。
7. 到 `/dashboard/worker` 確認 worker attempts 與 DLQ。
8. 到 `/dashboard/observability` 確認 worker、approval、incident、evaluator 與 SLO summary。
9. 到 `/dashboard/runs?tenant_id=default` 或輸入指定 tenant，確認 run list tenant filter 正常。
10. 到 `/dashboard/governance` 查詢 tenant summary、quota limited count 與 governance events。
11. 若要驗證 auth，設定 `AUTH_ENABLED=true` 與 `API_KEYS=admin-key:default:admin`，確認 valid `X-API-Key` 可建立 tenant/API key、invalid key 回 `401`、tenant mismatch 回 `403`、viewer submit 回 `403 insufficient_role`。
12. 確認 `/dashboard` summary、run detail、tool transcript、event timeline、tool executions 與 incidents 都有更新。
