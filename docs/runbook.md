# Runbook v0.8

此 runbook 用於本機 demo、維運檢查與 incident response。它反映目前實作狀態：API、Dashboard、Postgres audit trail、Kafka worker、Redis run state、approval queue、worker retry/DLQ、tool execution audit、evaluator/observability summary 與 tenant governance 已接入 request path。

目前營運基線：`v0.8.0`

## 快速檢查

API health：

```bash
curl http://localhost:8000/v1/healthz
```

Postgres readiness：

```bash
curl http://localhost:8000/v1/readyz
```

Prometheus metrics：

```bash
curl http://localhost:8000/v1/metrics
```

常用 Dashboard：

- `http://localhost:8000/dashboard/worker`
- `http://localhost:8000/dashboard/observability`
- `http://localhost:8000/dashboard/governance`
- `http://localhost:8000/dashboard/operations`

Dashboard：

```text
http://localhost:8000/dashboard
```

Docker Compose：

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker
```

## 1. API 無法啟動

症狀：

- `api` container exit。
- `/v1/healthz` 無回應。
- logs 出現 import error、dependency error、migration error 或 env parse error。

處理步驟：

1. 查看 logs：

```bash
docker compose logs api
```

2. 確認 `.env` 存在且 `POSTGRES_DSN` 指向可連線的 Postgres；主機端使用 `localhost:5432`，Docker container 內由 `docker-compose.yml` 覆蓋成 `postgres:5432`。
3. 確認 dependencies 已安裝：

```bash
pip install -r requirements.txt
```

4. 本機執行測試：

```bash
pytest -q
```

5. 若是 image 問題，重建：

```bash
docker compose build --no-cache api
```

## 2. Readiness 失敗

症狀：

- `/v1/healthz` 成功，但 `/v1/readyz` 失敗。
- Dashboard 或 submit API 回 500。

可能原因：

- Postgres container 未啟動或未 healthy。
- `POSTGRES_DSN` 錯誤。
- Alembic migration 尚未套用。
- DB 密碼或網路設定錯誤。

處理步驟：

1. 查看 Postgres 狀態：

```bash
docker compose ps postgres
docker compose logs postgres
```

2. 套用 migration。若正在驗證 Docker stack，優先從 API container 執行：

```bash
docker compose exec -T api alembic upgrade head
```

主機端直接驗證 migration 時可執行：

```bash
alembic upgrade head
```

3. 確認 `.env` 在 host 直接跑時使用 `localhost`；Docker Compose 內的 `api` / `worker` 會由 `docker-compose.yml` 覆蓋為 `postgres` hostname。

## 3. Submit 後 Dashboard 沒更新

症狀：

- `POST /v1/agent/submit` 成功，但 `/dashboard` total runs 沒增加。
- `/v1/runs` 查不到資料。

處理步驟：

1. 確認 submit endpoint 回傳的 `run_id`。
2. 查詢 API：

```bash
curl http://localhost:8000/v1/runs
curl http://localhost:8000/v1/dashboard/summary
```

3. 查看 API logs 是否有 persistence error。
4. 確認服務實際連到同一個 Postgres database。

若 run 停在 `queued` 太久：

```bash
docker compose ps worker
docker compose logs worker
```

確認 worker 有啟動且可連到 Kafka、Redis 與 Postgres。

## 4. Dashboard Playground Submit 回 internal_server_error

症狀：

- 在 `/dashboard/playground` 按下 Submit Run 後，頁面回傳 `{"error":{"code":"internal_server_error"}}`。
- API logs 顯示 stack trace 指向 `request.form()`。
- 錯誤訊息提到需要 `python-multipart` 才能解析 form。

可能原因：

- Docker image 或本機 runtime 沒有安裝 `python-multipart`。
- `requirements.txt` 與 `pyproject.toml` 的 runtime dependencies 不同步。
- 修改 Dashboard form route 後只測 GET page render，沒有測 POST submit flow。

處理步驟：

1. 查看 API logs：

```bash
docker compose logs api
```

2. 確認 `requirements.txt` 與 `pyproject.toml` 都包含 `python-multipart`。
3. 若是 Docker runtime 問題，重新 build：

```bash
docker compose up -d --build
```

4. 用表單 endpoint 做回歸驗證：

```bash
curl -X POST http://localhost:8000/dashboard/playground \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "user_id=demo-user&task_type=code_fix&allowed_tools=read_repo%2C+run_tests&max_tool_calls=8&goal=fix+bug1699&prompt=fix+bug&no_external_network=on"
```

預期結果：HTTP 200，頁面內容包含 `queued`，且不包含 `internal_server_error`。

## 5. Escalation / Deny Rate 突然升高

症狀：

- 大量 request 回 `escalated` 或 `denied`。
- `policy_flags` 中某些 flags 激增。
- incidents 明顯增加。

可能原因：

- prompt injection attack。
- 上游 prompt template 改壞。
- `DEFAULT_RISK_THRESHOLD` 調太低。
- 新 rule false positive。
- 使用者開始要求 external network、PII 或 destructive actions。

處理步驟：

1. 在 `/dashboard/runs` 依 status 篩選。
2. 查看 run detail 的 `policy_flags` 與 request payload。
3. 若是 false positive，調整 sanitizer、risk scoring 或 policy rule，並補 regression test。
4. 若是攻擊，先維持 fail closed，再加強 rate limit 或上游阻擋。

## 6. Evaluator Review Rate 過高

症狀：

- 許多 allow 後的任務最後 status 是 `awaiting_approval`。
- run detail 中 `evaluation_summary` verdict 為 `review`。

可能原因：

- executor output 過短。
- 任務缺乏驗證證據。
- evaluator heuristic 過嚴。
- 任務品質真的下降。

處理步驟：

1. 抽樣查看 `evaluation_summary`。
2. 比對 `shortcut_risk`、`hallucination_risk`、`policy_violation_risk`。
3. 對 code task 要求 test report 或 diff artifact。
4. 調整 evaluator 前先補測試資料。

## 7. Destructive Action Request

症狀：

- request constraints 中 `allow_destructive_actions=true`。
- 沒有 `requires_human_approval=true`。
- policy flags 出現 `destructive_without_approval`。

處理步驟：

1. 預設進入 `awaiting_approval` 或 `denied`。
2. 確認 incident 已建立。
3. 到 `/dashboard/approvals` 要求人工審核與 rollback plan。
4. 不要為了單一流程放寬全域規則；應建立特定 workflow policy。

## 8. PII Access Request

症狀：

- `allow_pii_access=true`。
- policy flags 出現 `pii_access_requested`。

處理步驟：

1. 預設 escalated。
2. 確認合法目的、資料最小化與保留期限。
3. 要求 PII redaction 或 masked dataset。
4. 確認 logs 不輸出 PII。

## 9. Tenant Quota / Governance 問題

症狀：

- submit 回 `429 tenant_request_quota_exceeded`。
- high-risk request 沒有執行，run 進入 `awaiting_approval` 且 `policy_flags` 包含 `tenant_high_risk_quota_exceeded`。
- `/dashboard/governance` 的 quota limited count 增加。

排查重點：

1. 確認 `.env` 或部署環境中的 quota 設定：

```bash
QUOTA_ENABLED=true
TENANT_REQUEST_LIMIT_PER_MINUTE=60
TENANT_HIGH_RISK_LIMIT_PER_HOUR=10
```

2. 查詢治理事件：

```bash
curl "http://localhost:8000/v1/governance/events?tenant_id=default"
curl "http://localhost:8000/v1/governance/summary?tenant_id=default"
```

3. 檢查 Redis quota keys：

```bash
docker compose exec redis redis-cli keys 'tenant:*:quota:*'
```

處理方向：

- 若是測試環境需要重跑，可暫時降低或關閉 `QUOTA_ENABLED`，但要在 `docs/test_report.md` 記錄實際設定。
- 若是正式環境流量暴增，先確認是否為單一 tenant 異常、是否有 prompt attack 或 retry storm，再調整 quota。
- request quota 超過時不會建立 run，只會寫入 `governance_events`；high-risk quota 超過時會建立 run 並 fail closed 進入人工審核。

## 10. Kafka Topic 或 Consumer 問題

目前 Kafka topics 會由 Docker Compose 建立，API 會 publish `agent.requests`，worker 會消費 queued run。`v0.6.0` 保留 retry/backoff 與 DLQ audit；lag alert 尚未實作。

若 `kafka-init` 或 worker 消費失敗：

```bash
docker compose logs kafka
docker compose logs kafka-init
docker compose logs worker
docker compose up kafka-init
```

常見檢查：

- `.env` 在 Docker Compose 中應使用 `KAFKA_BOOTSTRAP_SERVERS=kafka:9092`。
- queued run 若沒有被 worker 更新，先確認 worker container 是否健康。
- 重複 event 會被 Redis idempotency key 擋下，不應重複執行。

## 11. Release Gate 失敗

症狀：

- `/dashboard/operations` 顯示 readiness 為 `fail`。
- `POST /v1/operations/release-gates` 建立的 snapshot 狀態為 `fail`。

處理步驟：

1. 查看 `/dashboard/operations` 的 Readiness Checks，確認是哪個 `check_key` 阻擋。
2. 若是 `worker_dlq`、`stale_queued_runs` 或 `blocking_incidents`，先處理 worker、approval 或 incident，不要直接 release。
3. 若是 `backup_restore_evidence` 或 `secret_rotation_evidence` 為 `warn`，補做人工 drill 後透過 Dashboard 或 API 建立 evidence。
4. 若是 `bootstrap_keys_in_prod`，確認正式環境不要依賴 `.env` 的 `API_KEYS` fallback，改用 DB-managed API key 或正式 IAM。

注意：v0.8 的 Operations Gate 是審計與 readiness 層，不會自動執行 backup/restore、secret rotation 或 retention purge。

## 12. Redis 問題

目前 API 與 worker flow 依賴 Redis 管理短期 run state、idempotency key、tool circuit breaker 與 tenant quota。Redis 不保存唯一 audit trail，重要資料仍以 Postgres 為準。

檢查：

```bash
docker compose logs redis
docker compose exec redis redis-cli keys 'run:*:state'
docker compose exec redis redis-cli keys 'breaker:*'
docker compose exec redis redis-cli keys 'tenant:*:quota:*'
```

若某個 tool 被暫停，policy flag 會出現 `tool_circuit_breaker_open:<tool_name>`，run 會 fail closed 進入 `awaiting_approval`。

## Severity 分級

| Severity | 定義 | 例子 |
| --- | --- | --- |
| `critical` | 可能造成資料外洩、未授權 destructive action、系統性 policy bypass | PII exfiltration、production delete |
| `high` | 高風險請求大量通過或 evaluator 失效 | deny/escalate 無效、worker bypass policy |
| `medium` | 需人工審核但尚未造成確認損害 | evaluator review spike |
| `low` | 單一可控異常或文件/設定問題 | 低量 false positive |

## 營運 KPI

- request rate。
- tenant quota limited count。
- policy allow/escalate/deny rate。
- average risk score。
- top policy flags。
- evaluator pass/review rate。
- incident count by severity。
- Dashboard run count。
- Postgres readiness。
- worker completion count。
- approval queue count。
- current：worker job duration、retry/DLQ、approval/evaluator/governance summary。
- future：Kafka consumer lag、human approval turnaround time-series。
