# Runbook

此 runbook 用於值班、維運、incident response 與 production troubleshooting。現階段部分項目是正式化後才會啟用的操作，但先定義流程可避免上線後臨場補救。

## 快速檢查

### API 健康狀態

```bash
curl http://localhost:8000/v1/healthz
```

預期：

```json
{"status":"ok"}
```

### Metrics

```bash
curl http://localhost:8000/v1/metrics
```

確認是否看到：

- `agent_requests_total`
- `agent_policy_risk_score`
- `agent_evaluation_final_score`

### Docker Compose 狀態

```bash
docker compose ps
docker compose logs -f api
```

## 常見事故

## 1. API 無法啟動

症狀：

- `api` container exit。
- `/v1/healthz` 無回應。
- logs 出現 import error、dependency error 或 env parse error。

處理步驟：

1. 查看 API logs：

```bash
docker compose logs api
```

2. 確認 `.env` 存在且格式正確。
3. 確認 `requirements.txt` 安裝成功。
4. 本機執行：

```bash
pytest -q
```

5. 若是 container build 問題，重新 build：

```bash
docker compose build --no-cache api
```

## 2. Kafka Topic 建立失敗

症狀：

- `kafka-init` exit code 非 0。
- API 啟動前卡住。
- logs 顯示 broker not ready。

處理步驟：

1. 查看 Kafka health：

```bash
docker compose logs kafka
docker compose logs kafka-init
```

2. 確認 `kafka` service healthy。
3. 重新執行 init：

```bash
docker compose up kafka-init
```

4. 若要從 host 連 Kafka，檢查 advertised listener 是否支援 `localhost:9092`。

## 3. Kafka Consumer Lag 過高

目前 worker 尚未完整實作；正式接入後使用此流程。

症狀：

- `agent.requests` lag 持續增加。
- evaluation events 延遲。
- 使用者看到任務長時間 queued。

可能原因：

- worker replicas 不足。
- tool executor 太慢。
- 下游 Postgres/Redis/Kafka 不穩。
- 某些 task 卡住或 retry storm。

處理步驟：

1. 查看 consumer lag。
2. 查看 worker logs，依 `run_id` 或 `trace_id` 追蹤。
3. 檢查 tool execution duration。
4. 暫停高風險或高成本 tool。
5. 增加 worker replicas。
6. 若特定 payload 反覆失敗，送入 DLQ 並建立 incident。

## 4. Escalation Rate 突然升高

症狀：

- 大量 request 回 `escalated`。
- `agent_policy_flags_total` 中某些 flags 激增。
- review queue 變長。

可能原因：

- prompt injection attack。
- 上游 prompt template 改壞。
- policy threshold 太低。
- 新增 rule false positive。
- 使用者開始要求 external network、PII 或 destructive actions。

處理步驟：

1. 依 flag 分群查看 top patterns。
2. 抽樣檢查 prompt，注意不要在公開 log 暴露敏感內容。
3. 若是 false positive，回滾最近 policy 變更或調整 pattern。
4. 若是攻擊，啟用更嚴格 rate limit、封鎖來源、提高審核門檻。
5. 更新安全 regression tests。

## 5. Deny Rate 突然升高

症狀：

- 大量 request 直接 `denied`。
- `risk_score >= 0.85` 的比例升高。

處理步驟：

1. 查看最近部署與 config 變更。
2. 檢查 `DEFAULT_RISK_THRESHOLD`。
3. 找出主要 flags。
4. 若風險真實，維持 deny 並通知安全或產品團隊。
5. 若誤判，調整 scoring weight 或 pattern，並補 regression test。

## 6. Evaluator Review Rate 過高

症狀：

- 許多允許執行的任務最後狀態變成 `queued`。
- evaluator verdict 大量為 `review`。

可能原因：

- executor output 過短。
- 任務沒有驗證證據。
- evaluator heuristic 過嚴。
- 真實任務品質下降。

處理步驟：

1. 抽樣查看 `evaluation_summary`。
2. 分析 `shortcut_risk`、`hallucination_risk`、`policy_violation_risk`。
3. 對 code tasks 要求 test report 或 diff artifact。
4. 調整 evaluator threshold 前先補資料集評估。
5. 將不確定案例導向人工審核。

## 7. Destructive Action Request

症狀：

- request constraints 中 `allow_destructive_actions=true`。
- 沒有 `requires_human_approval=true`。
- flag 出現 `destructive_without_approval`。

處理步驟：

1. 預設 escalated 或 denied。
2. 建立 incident。
3. 要求人工審核與明確 rollback plan。
4. 確認 tool ACL 不允許繞過審核。
5. 若是合法常態流程，建立特定 workflow policy，不要放寬全域規則。

## 8. PII Access Request

症狀：

- `allow_pii_access=true`。
- flag 出現 `pii_access_requested`。

處理步驟：

1. 預設 escalated。
2. 確認合法目的、資料最小化與保留期限。
3. 要求 PII redaction 或 masked dataset。
4. 確認 logs 不輸出 PII。
5. 記錄審核者與審核理由。

## 9. Postgres 不可用

目前 API flow 與 dashboard 都依賴 Postgres audit trail；若 Postgres 不可用，submit、run 查詢與 dashboard 都會受影響。

症狀：

- persistence error。
- run/incident 查不到。
- migration job 失敗。

處理步驟：

1. 檢查 DB health 與連線數。
2. 確認 DSN secret。
3. 查看 migration 狀態。
4. 若寫入失敗，worker 應 retry；避免 offset commit 造成資料遺失。
5. 若資料庫持續不可用，暫停 worker 或將 events 保留在 Kafka。

## 10. Redis 不可用

目前 API flow 尚未依賴 Redis；正式接入後使用此流程。

症狀：

- rate limit/circuit breaker 失效。
- approval state 查不到。
- worker coordination error。

處理步驟：

1. 檢查 Redis health。
2. 確認 memory usage 與 eviction。
3. 若 Redis 無法恢復，系統應 fail closed 對高風險 actions escalate。
4. 不要只依賴 Redis 保存 audit-critical 資料。

## Severity 分級

| Severity | 定義 | 例子 |
| --- | --- | --- |
| `critical` | 可能造成資料外洩、未授權 destructive action、系統性 policy bypass | PII exfiltration、production delete |
| `high` | 高風險請求大量通過或 evaluator 失效 | deny/escalate 無效、worker bypass policy |
| `medium` | 需人工審核但尚未造成確認損害 | evaluator review spike |
| `low` | 單一可控異常或文件/設定問題 | health check 文案錯誤、低量 false positive |

## 營運 KPI

- request rate。
- policy allow/escalate/deny rate。
- average risk score。
- top policy flags。
- evaluator pass/review rate。
- incident count by severity。
- human approval turnaround time。
- incident closure time。
- Kafka consumer lag。
- worker job duration。
- false positive / false negative rate on sampled labels。

## 事後檢討模板

每次 medium 以上 incident 應記錄：

- 事件時間線。
- 影響範圍。
- 觸發條件。
- 哪些 guardrail 有效。
- 哪些 guardrail 失效。
- 是否需要新增 policy rule、test corpus、alert 或 runbook。
- owner 與完成期限。
