# 測試指南 v0.2

目前測試涵蓋 policy 周邊邏輯、API persistence、dashboard HTML response 與 basic evaluator behavior。

目前測試基線：`v0.2.0`

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
| `tests/test_api.py` | health/readyz、submit persistence、incident、summary、dashboard pages |

API tests 使用 SQLite in-memory database 覆寫 `get_session()`，不需要啟動 Docker Compose。

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
- 未來 Kafka worker 的 retry/DLQ integration tests。

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

4. 在 `/dashboard/playground` 送一筆低風險 request 與一筆高風險 request。
5. 確認 `/dashboard` summary、`/dashboard/runs` list、run detail 與 incidents 都有更新。
