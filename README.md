# Agent Reward-Hacking Guardrail

這是一個以 FastAPI、PostgreSQL、Redis、Kafka 組成的 agent guardrail 服務骨架，用來降低 agent 系統在過度最佳化目標時出現 reward hacking、捷徑行為、policy bypass、未授權 tool usage 與缺乏驗證的風險。

主要文件已整理到 [docs/README.md](docs/README.md)。請從該入口開始閱讀。

## 快速啟動

1. 複製環境設定：

```bash
cp .env.example .env
```

2. 啟動服務：

```bash
docker compose up -d --build
```

3. 檢查健康狀態：

```bash
curl http://localhost:8000/v1/healthz
```

4. 開啟 Dashboard：

```text
http://localhost:8000/dashboard
```

5. 送出測試請求：

```bash
curl -X POST http://localhost:8000/v1/agent/submit-with-details \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-1001",
    "task_type": "code_fix",
    "prompt": "Fix the bug in the payment workflow without changing business rules.",
    "goal": "Return a correct patch",
    "allowed_tools": ["read_repo", "write_patch", "run_tests"],
    "constraints": {
      "no_external_network": true,
      "requires_human_approval": false,
      "max_tool_calls": 8
    }
  }'
```

## 文件導覽

- [專案總覽](docs/README.md)
- [架構文件](docs/architecture.md)
- [API 文件](docs/api.md)
- [Dashboard 使用說明](docs/dashboard.md)
- [設定文件](docs/configuration.md)
- [部署文件](docs/deployment.md)
- [Runbook](docs/runbook.md)
- [Roadmap](docs/roadmap.md)
