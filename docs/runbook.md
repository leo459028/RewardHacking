# Runbook v0.2

此 runbook 用於本機 demo、維運檢查與 incident response。它反映目前實作狀態：API、Dashboard、Postgres audit trail 已接入；Kafka worker 與 Redis state 尚未接入 request path。

目前營運基線：`v0.2.0`

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

Dashboard：

```text
http://localhost:8000/dashboard
```

Docker Compose：

```bash
docker compose ps
docker compose logs -f api
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

2. 確認 `.env` 存在且 `POSTGRES_DSN` 指向可連線的 Postgres。
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

2. 套用 migration：

```bash
alembic upgrade head
```

3. 確認 `.env` 在 Docker Compose 中使用 `postgres` hostname，在 host 直接跑時使用 `localhost`。

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

## 4. Escalation / Deny Rate 突然升高

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

## 5. Evaluator Review Rate 過高

症狀：

- 許多 allow 後的任務最後 status 是 `queued`。
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

## 6. Destructive Action Request

症狀：

- request constraints 中 `allow_destructive_actions=true`。
- 沒有 `requires_human_approval=true`。
- policy flags 出現 `destructive_without_approval`。

處理步驟：

1. 預設 escalated 或 denied。
2. 確認 incident 已建立。
3. 要求人工審核與 rollback plan。
4. 不要為了單一流程放寬全域規則；應建立特定 workflow policy。

## 7. PII Access Request

症狀：

- `allow_pii_access=true`。
- policy flags 出現 `pii_access_requested`。

處理步驟：

1. 預設 escalated。
2. 確認合法目的、資料最小化與保留期限。
3. 要求 PII redaction 或 masked dataset。
4. 確認 logs 不輸出 PII。

## 8. Kafka Topic 或 Consumer 問題

目前 Kafka topics 會由 Docker Compose 建立，但 API 尚未 publish events，worker 也尚未實作。Kafka lag、DLQ、consumer group 相關 runbook 會在 Phase 3 導入 worker 後正式啟用。

目前若 `kafka-init` 失敗：

```bash
docker compose logs kafka
docker compose logs kafka-init
docker compose up kafka-init
```

## 9. Redis 問題

目前 API flow 尚未依賴 Redis。Redis 會在 Phase 4 用於 run state、approval token、rate limit、circuit breaker。正式接入後，Redis 故障時高風險 action 應 fail closed。

## Severity 分級

| Severity | 定義 | 例子 |
| --- | --- | --- |
| `critical` | 可能造成資料外洩、未授權 destructive action、系統性 policy bypass | PII exfiltration、production delete |
| `high` | 高風險請求大量通過或 evaluator 失效 | deny/escalate 無效、worker bypass policy |
| `medium` | 需人工審核但尚未造成確認損害 | evaluator review spike |
| `low` | 單一可控異常或文件/設定問題 | 低量 false positive |

## 營運 KPI

- request rate。
- policy allow/escalate/deny rate。
- average risk score。
- top policy flags。
- evaluator pass/review rate。
- incident count by severity。
- Dashboard run count。
- Postgres readiness。
- future：Kafka consumer lag、worker job duration、human approval turnaround time。
