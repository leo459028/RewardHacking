# 開發指南

## 專案結構

```text
app/
  api/            FastAPI routes and dependencies
  core/           settings and logging
  infra/          Kafka / Redis / Postgres clients
  models/         SQLAlchemy models
  observability/  Prometheus metrics
  schemas/        Pydantic schemas
  services/       policy engine, evaluator, orchestrator, executor, state, events
  workers/        Kafka worker consumer
docs/             project documents
tests/            unit tests
```

`bak/` 是備份目錄，文件與開發盤點時不納入。

## 本機開發

安裝相依套件：

```bash
pip install -r requirements.txt
```

或使用 Makefile：

```bash
make install
```

啟動 API：

```bash
make run
```

執行測試：

```bash
make test
```

執行 lint：

```bash
make lint
```

`requirements.txt` 目前包含 runtime 與 test dependencies；`pyproject.toml` 另外定義了 `dev` optional dependencies。

## Dashboard Form 開發注意

Dashboard route 若使用 `request.form()` 解析 HTML form，runtime dependencies 必須包含 `python-multipart`。新增或修改 form submit 行為時，測試不能只覆蓋 GET page render，也要補 POST route test，確認實際 submit 不會回 `internal_server_error`。

若新增或移除 runtime dependency，請同步更新 `requirements.txt` 與 `pyproject.toml`；Docker runtime 只會從 image 內的 dependencies 判斷，不會因本機環境已安裝而自動可用。

## Tenant Governance 開發注意

v0.6 的租戶治理路徑橫跨 schema、auth、quota、audit、API 與 Dashboard。新增或修改相關功能時，請同步檢查：

1. `AgentRequest`、`AgentResponse`、`RunRecord` 與 SQLAlchemy model 是否仍包含必要的 `tenant_id`。
2. `app/services/auth.py` 的 API key tenant mapping 與 `/v1` route dependency 是否符合預期。
3. `app/services/quota.py` 與 `app/services/run_state.py` 的 Redis / in-memory quota 行為是否一致。
4. `governance_events` 寫入、查詢 API 與 `/dashboard/governance` 是否能查到同一批資料。
5. migration 是否包含 backfill 或 downgrade 策略。
6. `tests/test_v06_governance.py`、API 文件、Dashboard 文件、設定文件與 `docs/test_report.md` 是否同步更新。

## Operations Gate 開發注意

v1.0 的 Operations Gate 橫跨 API、Dashboard、schema、model、migration、safety regression 與營運文件。新增或修改 release readiness、evidence 或 gate 行為時，請同步檢查：

1. `app/services/operations.py` 的 readiness check 是否有明確 `check_key`、`status`、`detail` 與 metadata。
2. `app/schemas/audit.py`、`app/models/operations.py` 與 migration 是否維持欄位同步。
3. `/v1/operations/readiness`、`/v1/operations/evidence`、`/v1/operations/release-gates` 與 `/dashboard/operations` 是否看到同一批資料。
4. Auth 行為是否符合 RBAC-lite：viewer/operator/admin 可讀，只有 admin 可建立 evidence 與 release gate。
5. `X-Trace-Id` 是否仍會回到 response header，submit flow 是否保存到 `request_payload.metadata.trace_id`。
6. `tests/test_v08_operations_gate.py`、API 文件、Dashboard 文件、Runbook、Migration 文件與 `docs/test_report.md` 是否同步更新。

## 新增 Policy Rule

建議流程：

1. 在 `app/services/risk_scoring.py` 調整 risk contribution。
2. 在 `app/services/policy_engine.py` 加上對應 flag 或 hard rule。
3. 在 `tests/test_policy_engine.py` 新增 allow/escalate/deny 測試。
4. 更新 [安全設計](security.md)、[API 文件](api.md) 或 [Runbook](runbook.md) 中的 flag 說明。

Policy rule 應該可解釋、可測試、可回滾。避免把難以重現的 model guess 直接寫成 hard deny。

## 新增 Prompt Injection Pattern

修改 `app/services/prompt_sanitizer.py` 的 `INJECTION_PATTERNS`。

注意事項：

- pattern 需盡量明確，降低 false positive。
- 若 pattern 對多語系 prompt 有效，請補測試。
- 被偵測到的 pattern 會進入 `prompt_flags`，再影響 risk score 與 policy flags。

## 新增 Evaluator Signal

修改 `app/services/evaluator.py`。

建議 evaluator signal 分成：

- shortcut risk：跳過驗證、過度簡短、只宣稱完成。
- hallucination risk：缺來源、缺證據、憑空推論。
- policy violation risk：嘗試 bypass、override、使用未授權能力。
- goal alignment：是否回應原始 goal。

新增 signal 後應補測試，並在 [Observability](observability.md) 加上對應 metric 或 log 建議。

## 接入真實 Tool Executor

目前 `BoundedToolExecutor` 會記錄受控 tool transcript 與 artifact refs，但不呼叫真實外部工具。正式 executor 應至少具備：

- tool ACL：只允許 request 中被核准的 tools。
- sandbox：限制檔案、網路、process、secret access。
- budget enforcement：tool calls、wall time、token、成本上限。
- audit artifact：紀錄 tool input/output、diff、test result。
- approval gate：destructive action 前確認人工核准。

目前已先抽出 executor interface，後續可由不同 task type 實作不同 adapter。

## 接入 Kafka Worker

`app/workers/consumer.py` 已可消費 `agent.requests` 並依 run id 處理 queued run。目前 worker flow：

1. Consume `agent.requests`。
2. Validate event schema。
3. Re-run policy check，避免 API 與 worker 之間狀態漂移。
4. Execute bounded tool plan。
5. Evaluate result。
6. Persist `AgentRun` 與 `Incident`。
7. Publish `agent.executions`、`agent.evaluations`、`agent.incidents`。
8. Commit offset。

失敗時目前會把 run 標記為 `failed` 並寫入 audit trail。後續應區分 transient error 與 permanent error，並導向 retry 或 dead-letter topic。

## Coding Style

- 保持 service class 小而清楚。
- schema 變更要同步更新 API docs 與測試。
- 安全相關邏輯要有單元測試。
- 不把 production secret 寫入 repo。
- 對外 response 不回傳敏感 prompt、PII 或完整 tool output，除非經過明確設計。
