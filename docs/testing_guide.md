# 測試指南 v0.3

目前測試涵蓋 policy 周邊邏輯、API persistence、queued submit、worker processing、approval queue、Redis breaker、dashboard HTML response 與 basic evaluator behavior。

目前測試基線：`v0.3.0`

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

API tests 使用 SQLite in-memory database 覆寫 `get_session()`，並用 in-memory state store / event publisher 取代 Redis/Kafka，不需要啟動 Docker Compose。

## CI Gate

GitHub Actions 目前會執行：

```bash
ruff check app tests
pytest -q
```

workflow 位於 `.github/workflows/ci.yml`。

## 建議補強測試

- Alembic migration upgrade/downgrade smoke test。
- Dashboard form POST 的完整 HTML flow。
- `GET /v1/runs` status/task_type filter。
- `GET /v1/incidents` severity filter。
- request validation error envelope。
- red-team prompt corpus regression。
- Kafka worker retry/DLQ integration tests。
- approval TTL、request changes 後續流程。

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

4. 在 `/dashboard/playground` 送一筆低風險 request，確認 run 先是 `queued`，worker 處理後變成 `completed`。
5. 送一筆高風險或 destructive request，確認進入 `awaiting_approval` 或 `denied`。
6. 到 `/dashboard/approvals` 測試 approve once、deny 或 request changes。
7. 確認 `/dashboard` summary、`/dashboard/runs` list、run detail、tool transcript、event timeline 與 incidents 都有更新。
