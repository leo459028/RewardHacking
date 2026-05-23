# 測試指南

目前測試集中在 policy engine，檔案為 `tests/test_policy_engine.py`。

## 執行測試

```bash
pytest -q
```

或：

```bash
make test
```

## 現有測試範圍

- 低風險 request 應該 `allow`。
- 同時包含 prompt injection、external network、destructive action、PII、large tool budget 的 request 應該 `escalate` 或 `deny`。

## 建議補強測試

### Unit Tests

- `sanitize_prompt()`：
  - 偵測每個 injection pattern。
  - 不應誤判常見無害句子。
- `compute_risk_score()`：
  - 每種 constraint 的加權。
  - score clamp 在 `0.0` 到 `1.0`。
  - 小數 rounding。
- `PolicyEngine.evaluate()`：
  - `invalid_tool_budget`。
  - `destructive_without_approval`。
  - `pii_access_requested`。
  - `external_network_allowed`。
  - `large_tool_budget`。
  - threshold 邊界值 `0.65` 與 `0.85`。
- `Evaluator.evaluate()`：
  - `not tested` 增加 shortcut risk。
  - `bypass` 或 `override` 增加 policy violation risk。
  - `analysis` / `data_query` 缺少 source 時 hallucination risk 增加。

### API Tests

使用 `httpx` 或 FastAPI `TestClient`：

- `GET /v1/healthz` 回傳 `ok`。
- `POST /v1/agent/submit` 回傳 `AgentResponse`。
- schema 錯誤時回傳 `422`。
- `submit-with-details` 包含 `policy`、`execution_event`、`evaluation_event`。

### Integration Tests

在 Docker Compose 環境中測試：

- API container 啟動。
- Kafka topics 建立成功。
- Postgres 與 Redis health check 成功。
- 未來 worker 接入後，測試 request event 到 evaluation event 的完整 flow。

### Safety Regression Tests

建立固定 red-team prompt corpus，測試：

- prompt injection。
- destructive action without approval。
- PII exfiltration。
- reward hacking shortcut，例如只回 `done`。
- 要求忽略 policy 或 reveal system prompt。

這類測試應在每次調整 policy/evaluator 後執行，避免安全規則被無意中放寬。

## CI Gate 建議

最小 CI：

```bash
ruff check app tests
pytest -q
```

正式化後加入：

- type checking。
- migration check。
- container build。
- dependency vulnerability scan。
- API schema compatibility check。
- safety regression corpus。
