# 測試紀錄

這份文件用來記錄本專案每次完成實作或文件同步後，實際執行過的測試與驗證結果。

請不要只填預期結果；如果某項驗證沒有執行，要清楚寫明原因。例如：純文件修改未啟動 Docker、Docker Desktop 尚未開啟、或本次沒有影響 Dashboard。

## 2026-05-24 - v0.6 文件同步檢查

- 版本：`v0.6.0`
- 變更範圍：檢查並同步 README、API、Dashboard、架構、設定、部署、開發、測試指南、資料模型、Kafka、Migration、Observability、Security、Runbook 與 `.env.example`。重點修正 v0.5 舊版現況描述、auth coverage、tenant/governance 路由、`governance_events` 資料表、Redis quota 與 Settings 環境變數清單。
- 測試環境：Windows PowerShell、Python 3.11；本次為文件與 `.env.example` 同步，未重新啟動 Docker stack。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `python -m pytest -q` | 通過，27 passed。 |
| `python -m ruff check app tests` | 通過，All checks passed。 |
| `git diff --check` | 通過，沒有 whitespace error。 |
| 舊版目前狀態掃描 | 通過，無命中；`docs/test_report.md` 與 `docs/roadmap.md` 保留歷史版本紀錄與 milestone，不視為目前狀態落差。 |
| `Settings` vs `docs/configuration.md` / `.env.example` 欄位比對 | 通過，`missing_docs=`、`missing_env=` 皆為空。 |
| FastAPI route list | 通過，列出 `/v1/governance/events`、`/v1/governance/summary`、`/dashboard/governance`、`/dashboard/runs`、`/dashboard/playground` 等路由，與文件入口同步。 |

補充命令細節：

```powershell
rg -n "# .*v0\.3|# .*v0\.5|目前.*v0\.5|基線：`v0\.5\.0`|部署版本：`v0\.5\.0`|目前 API 版本：`v0\.5\.0`|Auth、tenant isolation|Dashboard 尚未加入登入|tenant/user filters|資料與操作 API 需要 `X-API-Key`|tenant_id` \| request payload" README.md docs --glob '!docs/test_report.md' --glob '!docs/roadmap.md'

$fields = python -c "from app.core.config import Settings; print('\n'.join(k.upper() for k in Settings.model_fields.keys()))"
$docs = Get-Content -Raw -Encoding UTF8 docs\configuration.md
$env = Get-Content -Raw -Encoding UTF8 .env.example
$missingDocs = @($fields | Where-Object { $docs -notmatch [regex]::Escape($_) })
$missingEnv = @($fields | Where-Object { $env -notmatch [regex]::Escape($_) })

python -c "from app.main import app; routes=[]
for route in app.routes:
    methods=getattr(route, 'methods', None)
    path=getattr(route, 'path', '')
    if methods and (path.startswith('/v1') or path.startswith('/dashboard')):
        routes.append('{} {}'.format(','.join(sorted(methods)), path))
print('\n'.join(sorted(routes)))"
```

### Dashboard 驗證結果

- 本次未改 Dashboard route 或 template，因此未重新做瀏覽器點擊測試。
- Dashboard 相關文件已同步目前程式碼入口：`/dashboard/governance`、`/dashboard/runs` tenant filter、`/dashboard/playground` tenant input、Dashboard Basic Auth。
- 實際 Dashboard 行為由同日「v0.6 Dashboard 完整互動測試」與 `tests/test_v06_governance.py` 覆蓋，包含 governance render、runs tenant filter 與 Dashboard Basic Auth。

### Docker 驗證結果

- 本次未重新啟動 Docker stack；原因是變更範圍為文件與 `.env.example`，未修改 runtime code、migration 或 Docker Compose。
- 同日「v0.6 Tenant Governance MVP」紀錄已完成 Docker Compose、container migration、health/ready 與 Dashboard smoke。

### 限制與已知問題

- `docs/test_report.md` 保留 v0.3/v0.5 歷史測試紀錄，部分歷史掃描結果仍提到舊版標題，屬當時實際結果，不代表目前文件狀態。
- `docs/roadmap.md` 保留 v0.3/v0.5 milestone 作為歷史脈絡，未改成 v0.6 標題。

## 2026-05-24 - v0.6 Tenant Governance MVP

- 版本：`v0.6.0`
- 變更範圍：新增 tenant-aware audit、API key auth、Dashboard Basic Auth、Redis tenant quota、governance events、governance summary API 與 `/dashboard/governance`。
- 測試環境：Windows PowerShell、Python 3.11、Docker Desktop、Docker Compose stack `rewardhacking-*`。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `python -m pytest tests\test_v06_governance.py -q` | 通過，7 passed。 |
| `python -m pytest -q` | 通過，27 passed。 |
| `python -m ruff check app tests` | 通過，All checks passed。 |
| `alembic upgrade head` | 第一次在 Docker Postgres 未啟動時失敗，錯誤為 `ConnectionRefusedError [WinError 1225]`；Docker stack 啟動後重跑通過。 |
| `docker compose up -d --build` | 通過，api、worker、postgres、redis、kafka 啟動，postgres/redis/kafka healthy。 |
| `docker compose exec -T api alembic upgrade head` | 通過，PostgresqlImpl migration context，無 pending migration 錯誤。 |
| `Invoke-RestMethod http://localhost:8000/v1/healthz` | 通過，回傳 `{"status":"ok"}`。 |
| `Invoke-RestMethod http://localhost:8000/v1/readyz` | 通過，回傳 `{"status":"ready"}`。 |
| `POST http://localhost:8000/v1/agent/submit` | 通過，回傳 `queued` run，包含 `tenant_id="default"`。 |
| `GET http://localhost:8000/v1/governance/summary?tenant_id=default` | 通過，回傳 tenant summary。 |

### Dashboard 驗證結果

- `/dashboard/governance`：HTTP 200。
- `/dashboard/runs?tenant_id=default`：HTTP 200。
- `/dashboard/playground`：HTTP 200。
- Dashboard Basic Auth：測試環境中 `DASHBOARD_AUTH_ENABLED=true` 時，未帶 Basic Auth 回 `401`，正確帳密回 `200`。

### Docker 驗證結果

- `docker compose up -d --build` 成功重建 API/worker image。
- `docker compose exec -T api alembic upgrade head` 成功。
- API health、ready、Dashboard governance/runs/playground smoke 均成功。

### API / Governance 驗證結果

- `AUTH_ENABLED=true` 時，缺少 `X-API-Key` 回 `401 auth_required`。
- 錯誤 API key 回 `401 invalid_api_key`。
- API key tenant 與 body `tenant_id` 不一致回 `403 tenant_mismatch`。
- request quota 超過時回 `429 tenant_request_quota_exceeded`，並寫入 `governance_events`。
- high-risk quota 超過時 run 進入 `awaiting_approval`，policy flags 包含 `tenant_high_risk_quota_exceeded`。

### 限制與已知問題

- v0.6 是 MVP，尚未包含 OAuth、完整 RBAC、tenant 管理 UI、retention purge、backup/restore、secret rotation 或真實 OS sandbox。
- Docker smoke 使用預設 `AUTH_ENABLED=false`，auth 行為由 pytest 的 in-memory API 測試覆蓋。
- PowerShell `Invoke-WebRequest` 需使用 `-UseBasicParsing` 才能在此環境讀取 Dashboard HTTP status。

## 2026-05-24 - v0.6 Dashboard 完整互動測試

- 版本：`v0.6.0`
- 變更範圍：針對 v0.6 Dashboard 做瀏覽器互動驗證，包含表單輸入、篩選、連結開啟、審核按鈕與手機尺寸 smoke。
- 測試環境：Windows PowerShell、Python 3.11、Docker Compose stack `rewardhacking-*`、Codex in-app Browser。

### 後端與 Migration 驗證

| 命令 | 實際結果 |
| --- | --- |
| `docker compose ps` | 通過，api、worker、postgres、redis、kafka 皆 Up；postgres/redis/kafka healthy。 |
| `python -m pytest -q` | 通過，27 passed。 |
| `python -m ruff check app tests` | 通過，All checks passed。 |
| `alembic upgrade head` | 通過，PostgresqlImpl migration context，無錯誤。 |
| `docker compose exec -T api alembic upgrade head` | 通過，container 內 migration 可重複執行。 |

### Dashboard Browser 互動驗證

| 流程 | 實際結果 |
| --- | --- |
| `/dashboard` 載入 | 通過，title 為 `Dashboard | 儀表板`，DOM 含 `Guardrail Overview`，console error/warn 為空。 |
| Playground 低風險 submit | 通過，填入 tenant `qa-ui-tenant`、user、task type、tools、goal、prompt 後送出，頁面顯示 `Result / 執行結果` 與 `queued`。 |
| Run detail link | 通過，點擊 `Open run detail` 後進入 run detail，顯示 `Policy` 與 request/user 資訊。 |
| Runs tenant filter | 通過，在 `/dashboard/runs` 輸入 `qa-ui-tenant` 並點擊 Apply，URL 變為 `/dashboard/runs?status=&task_type=&tenant_id=qa-ui-tenant`，表格顯示該 tenant run。 |
| Governance filter | 通過，開啟 `/dashboard/governance?tenant_id=qa-ui-tenant`，顯示 `Governance / 治理中心`、tenant summary 與 tenant runs。 |
| Playground destructive submit | 通過，填入 tenant `qa-approval-tenant`、task type `workflow`、tool `deploy_service`，勾選 destructive actions 後送出，頁面顯示 `awaiting_approval`。 |
| Approval decision click | 通過，在 `/dashboard/approvals` 找到新 awaiting run，填寫 reason `Browser QA deny click` 並點擊 Deny，該 run 從 approval queue 移除。 |
| Denied run detail | 通過，開啟 denied run detail，狀態為 `denied`，Policy 顯示 `Denied by dashboard-operator: Browser QA deny click`。 |
| 手機尺寸 smoke | 通過，viewport `390x844` 開啟 governance page，DOM 含 `Governance`、`治理中心` 與 `qa-ui-tenant`，console error/warn 為空。 |

### 截圖證據

- 已透過 Codex in-app Browser 輸出截圖：Runs tenant filter、Governance filter、Playground approval result、Approval Queue、Denied run detail、手機尺寸 governance。
- 截圖沒有寫入 repo；嘗試寫入本機暫存路徑時環境拒絕存取，因此改用 Browser runtime 直接回傳影像。

### 限制與已知問題

- 本次 Docker stack 使用目前 `.env` 預設值，因此 Dashboard auth 在瀏覽器 smoke 中未開啟；Basic Auth 行為由 pytest `test_dashboard_basic_auth_can_protect_dashboard` 覆蓋。
- 手機尺寸測試為 smoke，不包含所有表單互動；桌面尺寸已覆蓋主要輸入與點擊流程。

## 2026-05-24 - v0.5 主機端與 Docker DSN 設定分離

- 版本：v0.5.0
- 變更範圍：修正 `.env.example` 與 Docker Compose 的設定分工，讓主機端可直接執行 `alembic upgrade head`，Docker container 內仍使用 `postgres`、`redis`、`kafka` service hostnames。
- 測試環境：本機 PowerShell、Python 3.11、Docker Desktop；使用既有預設 `rewardhacking-*` Docker stack。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `alembic upgrade head` | 通過，直接使用本機 `.env` 的 `localhost:5432`，不需臨時覆蓋 `POSTGRES_DSN`。 |
| `python -m pytest -q` | 通過，20 passed。 |
| `python -m ruff check app tests` | 通過，All checks passed。 |
| `docker compose up -d` | 通過，套用新的 compose service environment；api、worker、postgres、redis、kafka 皆維持 Up。 |
| `docker compose exec -T api alembic upgrade head` | 通過，container 內使用 `postgres:5432`，無 pending migration 錯誤。 |
| `docker compose exec -T api python -c "import os; ..."` | 通過，確認 api container 內 `POSTGRES_DSN=postgresql+asyncpg://app:app@postgres:5432/app`、`REDIS_URL=redis://redis:6379/0`、`KAFKA_BOOTSTRAP_SERVERS=kafka:9092`。 |
| `curl http://localhost:8000/v1/healthz` | 通過，回 `{"status":"ok"}`。 |
| `curl http://localhost:8000/v1/readyz` | 通過，回 `{"status":"ready"}`。 |

### Dashboard 驗證

- 驗證網址：不適用，本次是設定與 migration 執行路徑修正，未改 Dashboard 使用者介面。
- 實際結果：API health/readyz 正常，代表 Dashboard 所需的 API 與 Postgres 連線仍可用。
- 未執行原因：本次沒有修改 Dashboard route、template 或互動行為；前一筆紀錄已完成最新 Docker stack 的 Dashboard 點擊驗證。

### Docker 驗證

- 驗證命令：`docker compose up -d`、`docker compose ps`、`docker compose exec -T api alembic upgrade head`、container env check、API health/readyz。
- 實際結果：Docker container 端明確覆蓋為 Docker network hostnames；主機端 `.env` 改為 `localhost` 後不會影響 container 內連線。
- 未執行原因：不適用，本次已執行 Docker 驗證。

### 限制與已知問題

- `.env` 是本機未追蹤檔案；本次已同步改成本機可用的 `localhost` 設定，版本控制中則以 `.env.example` 記錄主機端預設。
- Kafka advertised listener 仍以 container-to-container demo 為主；若要從 host 直接跑完整 worker 並連 Kafka，仍需另行調整 Kafka advertised listeners。

## 2026-05-24 - v0.5 最新 Docker stack 直接重測

- 版本：v0.5.0
- 變更範圍：依使用者要求，直接使用目前已啟動並重建完成的預設 Docker stack `rewardhacking-*` 重跑測試，不另開獨立 compose project，也不修改設定。
- 測試環境：本機 PowerShell、Python 3.11、Docker Desktop、Codex in-app Browser；Docker services 使用預設 ports：API `8000`、Postgres `5432`、Redis `6379`、Kafka `9092`。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `docker compose ps` | 通過，`rewardhacking-api-1`、`rewardhacking-worker-1`、`rewardhacking-postgres-1`、`rewardhacking-redis-1`、`rewardhacking-kafka-1` 皆 Up；Postgres/Redis/Kafka healthy。 |
| `python -m pytest -q` | 通過，20 passed。 |
| `python -m ruff check app tests` | 通過，All checks passed。 |
| `alembic upgrade head` | 第一次使用 `.env` 內的容器 hostname `postgres`，主機端無法解析而失敗；未改設定，改用臨時環境變數 `POSTGRES_DSN=postgresql+asyncpg://app:app@localhost:5432/app` 後通過。 |
| `curl http://localhost:8000/v1/healthz` | 通過，回 `{"status":"ok"}`。 |
| `curl http://localhost:8000/v1/readyz` | 通過，回 `{"status":"ready"}`。 |
| `curl http://localhost:8000/dashboard` | 通過，HTML 內已包含 `Worker` 與 `Observability` 側邊欄連結。 |
| `POST http://localhost:8000/v1/agent/submit` | 通過，建立 run `dac58d70-f49f-4f36-b730-8815ed25ed36`；worker 處理後狀態為 `completed`，worker attempts=1，tool executions=2。 |

### Dashboard 驗證

- 驗證網址：`http://localhost:8000/dashboard/runs`、`/dashboard/runs/dac58d70-f49f-4f36-b730-8815ed25ed36`、`/dashboard/worker`、`/dashboard/observability`
- 實際結果：用 Codex in-app Browser 點擊最新 Docker Dashboard。Runs 頁可找到 `dac58d70` 並點入 detail；run detail 顯示 `completed`，且有 `Worker Attempts` 與 `Tool Executions` 區塊。側邊欄已顯示 `Worker` 與 `Observability`；點擊 `Worker` 進入 `/dashboard/worker`，頁面顯示 `Dead Letter Queue`、`Recent Attempts` 與該 run 的 completed attempt；點擊 `Observability` 進入 `/dashboard/observability`，頁面顯示 Worker Completed=1、Worker DLQ=0、Eval Pass=1 與 SLO。瀏覽器 console error/warn 為空。
- 截圖：已由 Browser 驗證流程擷取 Run detail、Worker、Observability 三張截圖。
- 未執行原因：不適用，本次已執行 Dashboard 點擊驗證。

### Docker 驗證

- 驗證命令：直接使用目前的預設 `rewardhacking-*` Docker stack，未建立獨立 container/project。
- 實際結果：最新 Docker image 已在 `localhost:8000` 生效；Dashboard 側邊欄包含 Worker/Observability；API health/readyz、worker processing、audit 查詢與 Dashboard 點擊皆通過。
- 未執行原因：不適用，本次已執行 Docker 驗證。

### 限制與已知問題

- 主機端直接執行 `alembic upgrade head` 時，`.env` 的 `postgres` 是 Docker network 內 hostname；主機端不需改設定，只要在命令層臨時指定 `localhost:5432` DSN 即可。
- Browser 表單文字輸入在本環境受虛擬剪貼簿限制影響，因此本次用 API 建立測試 run，再用 Dashboard 實際點擊驗證 run detail、Worker 與 Observability。

## 2026-05-24 - v0.5 完整重測與 Dashboard 點擊驗證

- 版本：v0.5.0
- 變更範圍：依使用者要求重跑完整測試，包含 Dashboard 實際瀏覽器點擊流程；本次未修改程式碼。
- 測試環境：本機 PowerShell、Python 3.11、Docker Desktop、Codex in-app Browser；隔離 Docker Compose project `rewardhacking-v05-clicktest`。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `python -m pytest -q` | 通過，20 passed。 |
| `python -m ruff check app tests` | 通過，All checks passed。 |
| `POSTGRES_DSN=postgresql+asyncpg://app:app@localhost:15432/app alembic upgrade head` | 通過，Alembic 顯示 PostgreSQL migration context，無錯誤。 |
| `docker compose -p rewardhacking-v05-clicktest -f docker-compose.yml -f - up -d --build` | 通過，使用 ports：API `18080`、Postgres `15432`、Redis `16379`、Kafka `19092`；api、worker、postgres、redis、kafka 皆 Up，postgres/redis/kafka healthy。 |
| `curl http://localhost:18080/v1/healthz` | 通過，回 `{"status":"ok"}`。 |
| `curl http://localhost:18080/v1/readyz` | 通過，回 `{"status":"ready"}`。 |
| `curl http://localhost:18080/v1/observability/summary` | 通過，Dashboard 點擊測試後回 worker completed=1、evaluator pass=1、DLQ=0。 |

### Dashboard 驗證

- 驗證網址：`http://localhost:18080/dashboard`、`/dashboard/runs`、`/dashboard/approvals`、`/dashboard/worker`、`/dashboard/observability`、`/dashboard/playground`、`/dashboard/runs/{run_id}`
- 實際結果：用 Codex in-app Browser 點擊側邊欄 nav，Overview/Runs/Approvals/Worker/Observability/Playground 皆可開啟且 title 與主要 heading 正確。Playground 表單填入 `code_fix`、`read_repo, run_tests` 後 submit，頁面顯示 `Result` 與 `queued`；點擊 `Open run detail` 後等待 worker，run detail 顯示 `completed`，且有 `Worker Attempts` 與 `Tool Executions` 區塊。再點 `/dashboard/worker` 顯示該 run 的 completed attempt；點 `/dashboard/observability` 顯示 Worker Completed=1、Eval Pass=1。瀏覽器 console error/warn 為空。
- 截圖：已由 Browser 驗證流程擷取 Playground 初始頁、Playground submit 結果、Run detail、Worker、Observability 五張截圖。
- 未執行原因：不適用，本次已執行 Dashboard 點擊驗證。

### Docker 驗證

- 驗證命令：隔離 project `rewardhacking-v05-clicktest`，使用 compose override 避免占用既有 `rewardhacking-*` 容器 ports。
- 實際結果：Docker stack 啟動成功；`docker compose -p rewardhacking-v05-clicktest ps` 顯示 api、worker、postgres、redis、kafka 皆 Up；API 與 Dashboard 點擊測試皆通過。
- 未執行原因：不適用，本次已執行 Docker 驗證。

### 限制與已知問題

- 因既有 `rewardhacking-*` stack 已占用預設 ports，本次使用替代 host ports 驗證。
- `/v1/metrics` 的 API process 可看到 metric family；worker process metrics 不會出現在 API process 的 `/v1/metrics` 中，Dashboard observability 以 Postgres audit tables 驗證。

## 2026-05-24 - v0.5 Execution Hardening 與 Observability

- 版本：v0.5.0
- 變更範圍：實作 worker retry/DLQ audit、approval TTL/event、tool execution audit、task-specific evaluator result、observability summary、Dashboard worker/observability 頁面，並同步 Roadmap、README 與相關文件。
- 測試環境：本機 PowerShell、Python 3.11、Docker Desktop；worktree 分支 `codex/v0.4-v0.5-hardening-observability`。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `python -m pytest -q` | 通過，20 passed。 |
| `python -m ruff check app tests` | 通過，All checks passed。 |
| `alembic upgrade head` | 第一次使用預設 `localhost:5432` 失敗，原因是該 Postgres 未提供本 worktree migration 連線；改用 `POSTGRES_DSN=postgresql+asyncpg://app:app@localhost:15432/app` 後通過。 |
| `docker compose up -d --build` | 第一次因既有 `rewardhacking-*` 容器占用 `8000/5432/6379/9092` 失敗；改用 compose override ports `18080/15432/16379/19092` 後通過。 |

### Dashboard 驗證

- 驗證網址：`http://localhost:18080/dashboard/worker`、`http://localhost:18080/dashboard/observability`、`http://localhost:18080/dashboard/runs/{run_id}`、`http://localhost:18080/dashboard/approvals`
- 實際結果：`/dashboard/worker` HTTP 200 且顯示 `Worker Operations`；`/dashboard/observability` HTTP 200 且顯示 `Observability Trends`；低風險 code_fix run 從 `queued` 變成 `completed`，`worker-attempts=1`、`tool-executions=2`、`evaluations=1`；destructive workflow run 進入 `awaiting_approval`，`expired=true` 可查，`request_changes` 會記錄 reviewer 與 reason。
- 未執行原因：不適用，本次已執行 Dashboard 驗證。

### Docker 驗證

- 驗證命令：使用 compose override 啟動：API `18080`、Postgres `15432`、Redis `16379`、Kafka `19092`。
- 實際結果：`docker compose ps` 顯示本 worktree 的 `api`、`worker`、`postgres`、`redis`、`kafka` 皆 Up；`postgres`、`redis`、`kafka` healthy；`GET /v1/healthz` 回 `{"status":"ok"}`；`GET /v1/readyz` 回 `{"status":"ready"}`；`GET /v1/observability/summary` 回傳 worker completed、approval、incident、evaluator 與 SLO summary。
- 未執行原因：不適用，本次已執行 Docker 驗證。

### 限制與已知問題

- 因本機已有原專案 Docker stack 占用預設 ports，本次使用替代 host ports 驗證，容器內部服務名稱與 ports 維持原設計。
- `BoundedToolExecutor` 目前仍不呼叫真實外部工具；sandbox、timeout、resource/cost limit 是可審計 policy contract，不是完整 OS sandbox。
- LLM-as-judge 目前是 deterministic fake judge signal，預設 `used_for_hard_deny=false`。
- Worker process 的 Prometheus metrics 位於 worker process 內；API `/v1/metrics` 可查 API-side metrics，Dashboard observability 使用 Postgres audit tables。

## 2026-05-24 - docs 文件總覽檔名調整

- 版本：v0.3.0
- 變更範圍：將 docs 目錄內原本與根目錄同名的文件總覽更名為 `docs/OVERVIEW.md`，避免混淆；同步更新根目錄 README 與測試紀錄中的相關引用。
- 測試環境：本機 PowerShell，未啟動新的 Docker 驗證。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `rg -n "docs/READ[M]E\\.md" README.md AGENTS.md docs app tests pyproject.toml docker-compose.yml` | 通過，無輸出；`rg` exit code 為 1，代表沒有殘留舊路徑。 |
| `rg -n "docs/OVERVIEW\\.md|OVERVIEW\\.md|專案文件總覽" README.md docs/OVERVIEW.md docs/test_report.md` | 通過，確認根目錄 README 已連到 `docs/OVERVIEW.md`，且新檔內容仍保留 `# 專案文件總覽 v0.3`。 |
| `Get-Content -Encoding UTF8 docs/OVERVIEW.md` | 通過，可正常讀取 UTF-8 內容。 |

### Dashboard 驗證

- 驗證網址：未執行。
- 實際結果：未執行。
- 未執行原因：本次只調整文件檔名與文件連結，未影響 Dashboard route、template、API 行為或 Docker 設定。

### Docker 驗證

- 驗證命令：未執行。
- 實際結果：未執行。
- 未執行原因：本次只修改文件檔名與文件連結，沒有改動程式碼、依賴、Dockerfile、Docker Compose 或 migration。

### 限制與已知問題

- 本次未執行 `pytest`、`ruff` 或 Docker，因為變更範圍是文件重新命名與引用更新。

## 2026-05-24 - v0.3 踩坑紀錄文件化

- 版本：v0.3.0
- 變更範圍：將 Playground form parser、Docker Compose、Kafka image/topic init、migration owner 等 v0.3 開發踩坑整理到 `docs/runbook.md`、`docs/development.md`、`docs/deployment.md`，保留 `docs/test_report.md` 作為歷史測試紀錄。
- 測試環境：本機 PowerShell，未啟動新的 Docker 驗證。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `rg -n "python-multipart|request\\.form|internal_server_error|kafka-topics|bitnamilegacy|migration" docs` | 通過，確認 `docs/runbook.md`、`docs/development.md`、`docs/deployment.md` 可搜尋到新增的踩坑關鍵字與操作提醒。 |
| `Get-Content -Encoding UTF8 docs/runbook.md` | 通過，可正常讀取 UTF-8 內容，並確認新增 `Dashboard Playground Submit 回 internal_server_error` 章節。 |
| `Get-Content -Encoding UTF8 docs/development.md` | 通過，可正常讀取 UTF-8 內容，並確認新增 Dashboard form 開發注意事項。 |
| `Get-Content -Encoding UTF8 docs/deployment.md` | 通過，可正常讀取 UTF-8 內容，並確認新增 Docker Compose 常見陷阱。 |

### Dashboard 驗證

- 驗證網址：未執行。
- 實際結果：未執行。
- 未執行原因：本次只調整文件，沒有修改 Dashboard route、template、API 行為或 Docker 設定；Playground submit 的實際驗證已在上一筆 `Playground Submit Run 500 修正` 紀錄中完成。

### Docker 驗證

- 驗證命令：未執行。
- 實際結果：未執行。
- 未執行原因：本次只修改文件，沒有改動程式碼、依賴、Dockerfile、Docker Compose 或 migration。

### 限制與已知問題

- 本次未執行 `pytest`、`ruff` 或 Docker，因為變更範圍是文件化既有踩坑與操作知識。

## 2026-05-24 - Playground Submit Run 500 修正

- 版本：v0.3.0
- 變更範圍：修正 Dashboard Playground 表單送出時因 Docker runtime 缺少 `python-multipart`，導致 `request.form()` 解析失敗並回 `internal_server_error` 的問題；新增 Playground POST 回歸測試。
- 測試環境：本機 PowerShell、Python test env、Docker Desktop、Docker Compose。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `pytest tests/test_api.py::test_dashboard_playground_submit_queues_run -q` | 通過，1 passed。 |
| `ruff check app tests` | 通過，All checks passed。 |
| `pytest -q` | 通過，16 passed。 |
| `docker compose up -d --build` | 通過，API/worker image 重新 build；build log 確認安裝 `python-multipart-0.0.29`；Postgres、Redis、Kafka、API、worker 啟動完成。 |
| `curl.exe -s http://localhost:8000/v1/healthz` | 通過，回傳 `{"status":"ok"}`。 |
| `curl.exe -s http://localhost:8000/v1/readyz` | 通過，回傳 `{"status":"ready"}`。 |
| `POST http://localhost:8000/dashboard/playground`，使用 Playground 畫面相同欄位資料 | 通過，HTTP 200；回傳內容包含 `queued`，且不包含 `internal_server_error`。 |

### Dashboard 驗證

- 驗證網址：`http://localhost:8000/dashboard/playground`
- 實際結果：使用 `demo-user`、`code_fix`、`read_repo, run_tests`、`fix bug1699`、`修正會引起連鎖反應的 bug` 與 `No external network` 的表單資料送出後，頁面不再回 JSON 500 error，run 會進入 `queued`。
- 未執行原因：不適用，本次已用實際 Dashboard form endpoint 驗證。瀏覽器 plugin 本輪未暴露可用的 localhost 操作工具，因此未補截圖驗證。

### Docker 驗證

- 驗證命令：`docker compose up -d --build`、`curl.exe -s http://localhost:8000/v1/healthz`、`curl.exe -s http://localhost:8000/v1/readyz`、`POST http://localhost:8000/dashboard/playground`
- 實際結果：通過。容器重新 build 後已包含 `python-multipart`，API health/readyz 正常，Dashboard Playground submit 回 200。
- 未執行原因：不適用，本次已執行 Docker 驗證。

### 限制與已知問題

- 本次修正的是表單解析 dependency 缺漏；沒有變更 policy、worker、approval 或 executor 行為。

## 2026-05-24 - 測試指南檔名調整

- 版本：v0.3.0
- 變更範圍：將舊測試指南檔名更名為 `docs/testing_guide.md`，並更新 docs 文件總覽中的測試指南連結；文件總覽目前位於 `docs/OVERVIEW.md`。
- 測試環境：本機 PowerShell，未啟動新的 Docker 驗證。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| legacy testing guide path search | 通過，無輸出；`rg` exit code 為 1，代表沒有殘留舊路徑。 |
| `rg -n "testing_guide\\.md|測試指南" docs/OVERVIEW.md docs/testing_guide.md` | 通過，確認新連結與 `# 測試指南 v0.3` 標題存在。 |
| `Get-Content -Encoding UTF8 docs/testing_guide.md` | 通過，可正常讀取 UTF-8 內容。 |

### Dashboard 驗證

- 驗證網址：未執行。
- 實際結果：未執行。
- 未執行原因：本次只調整文件檔名與文件連結，未影響 Dashboard 路由、畫面或後端資料流。

### Docker 驗證

- 驗證命令：未執行。
- 實際結果：未執行。
- 未執行原因：本次只調整文件檔名與文件連結，未改動程式碼、資料庫 migration、Docker Compose 或服務設定。

### 限制與已知問題

- 本次未執行 `pytest` 或 `ruff`，因為沒有修改程式碼或測試邏輯。

## 2026-05-24 - v0.3.0 Guarded Async Execution MVP

- 版本：v0.3.0
- 變更範圍：重整 Roadmap，實作 queued submit、Kafka request publish、worker consumer、Redis run state/idempotency/breaker、approval API、bounded executor transcript、Dashboard approval queue，並同步 README 與 docs。
- 測試環境：本機 PowerShell、Python test env、Docker Desktop、Docker Compose。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `pytest -q` | 通過，15 passed。 |
| `ruff check app tests` | 通過，All checks passed。 |
| `docker manifest inspect bitnami/kafka:3.7.0` | 失敗，Docker Hub 無此 manifest。 |
| `docker manifest inspect bitnamilegacy/kafka:3.7.0` | 通過，確認 amd64/arm64 manifest 存在。 |
| `docker manifest inspect apache/kafka:3.7.0` | 通過，確認官方 Apache Kafka 3.7.0 manifest 存在；本專案因沿用 Bitnami KRaft env，採 `bitnamilegacy/kafka:3.7.0`。 |
| `docker compose up -d --build` | 初次失敗：`.env` 不存在；建立 `.env` 後第二次失敗：`bitnami/kafka:3.7` tag 不存在；改為 `bitnamilegacy/kafka:3.7.0` 後第三次失敗：`kafka-topics.sh` 不在 PATH；修為 `/opt/bitnami/kafka/bin/kafka-topics.sh` 後第四次失敗：worker 與 API 同時跑 migration 造成 Alembic version table 競態；移除 worker migration 後通過，API/worker/Postgres/Redis/Kafka 均啟動。 |
| `docker compose exec -T api alembic upgrade head` | 通過，Alembic 使用 PostgresqlImpl，無 pending migration。 |
| `curl.exe -s http://localhost:8000/v1/healthz` | 通過，回傳 `{"status":"ok"}`。 |
| `curl.exe -s http://localhost:8000/v1/readyz` | 通過，回傳 `{"status":"ready"}`。 |
| `POST http://localhost:8000/v1/agent/submit` 低風險 request | 通過，API 先回 `queued`；約 6 秒後 `GET /v1/runs/{run_id}` 顯示 `completed`，`requires_human_review=false`，且有 execution output、event timeline、tool transcript。 |
| `POST http://localhost:8000/v1/agent/submit-with-details` destructive request | 通過，回 `awaiting_approval`，policy flag 為 `destructive_without_approval`，`GET /v1/approvals` 可查。 |
| `GET http://localhost:8000/dashboard/approvals` | 通過，HTTP 200，頁面包含 `Approval Queue`。 |
| Redis 寫入 `breaker:read_data` 後送低風險 request | 通過，API 回 `awaiting_approval`，policy flag 為 `tool_circuit_breaker_open:read_data`。 |
| `curl.exe -s http://localhost:8000/v1/dashboard/summary` | 通過，回傳 total_runs=3、completed=1、awaiting_approval=2、failed=0。 |

### Dashboard 驗證

- 驗證網址：`http://localhost:8000/dashboard`、`http://localhost:8000/dashboard/runs/{run_id}`、`http://localhost:8000/dashboard/approvals`
- 實際結果：低風險 run 可由 `queued` 更新為 `completed`，且不會被誤標為需要人審；run detail 可查 execution output、event timeline、tool transcript；destructive 與 breaker 情境會進 approval queue；approval queue 頁面 HTTP 200 且顯示 `Approval Queue`。
- 未執行原因：不適用，本次已執行 Dashboard 驗證。

### Docker 驗證

- 驗證命令：`docker compose up -d --build`、`docker compose ps`、`docker compose exec -T api alembic upgrade head`、`curl.exe -s http://localhost:8000/v1/healthz`、`curl.exe -s http://localhost:8000/v1/readyz`
- 實際結果：最終通過。`docker compose ps` 顯示 `api`、`worker`、`postgres`、`redis`、`kafka` 皆 Up，`kafka` 與 Postgres/Redis healthy；`kafka-init` Exited 0。
- 未執行原因：不適用，本次已執行 Docker 驗證。

### 限制與已知問題

- Docker 驗證需要先建立被 git ignore 的 `.env`，本次已由 `.env.example` 複製。
- `v0.3.0` 的 `BoundedToolExecutor` 只記錄受控 transcript，不是完整 OS sandbox。
- Worker 已能消費 queued run，但 retry/backoff、DLQ、consumer lag alert 尚未實作。
- Approval workflow 是最小版，完整 TTL、reviewer 報表與 request changes 後續流程留到後續版本。

## 2026-05-24 - AGENTS.md 工作規範修正

- 版本：v0.2.0
- 變更範圍：修正 `AGENTS.md` 的工作規範文字，新增 `docs/test_report.md` 測試紀錄模板。
- 測試環境：本機 PowerShell，未啟動 Docker。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `Get-Content -Raw -Encoding UTF8 AGENTS.md` | 通過，可正常讀取 UTF-8 內容。 |
| `Get-Content -Raw -Encoding UTF8 docs/test_report.md` | 通過，可正常讀取 UTF-8 內容。 |
| `git status --short` | 通過，顯示 `AGENTS.md` 與 `docs/test_report.md` 為未追蹤檔案。 |

### Dashboard 驗證

- 驗證網址：未執行。
- 實際結果：未執行。
- 未執行原因：本次只修改工作規範與文件模板，未影響 Dashboard 路由、畫面或後端資料流。

### Docker 驗證

- 驗證命令：未執行。
- 實際結果：未執行。
- 未執行原因：本次只修改文件，沒有改動需要容器啟動驗證的程式碼、資料庫 migration 或服務設定。

### 限制與已知問題

- 本次未執行 `pytest` 或 `ruff`，因為沒有修改程式碼或測試邏輯。

## 紀錄方式

- 每次新增紀錄時，請放在本檔案最上方。
- 日期請使用 `YYYY-MM-DD`。
- 版本請填目前專案版本，例如 `v0.2.0`。
- 命令要填實際執行的完整指令。
- 結果要填實際輸出重點，例如通過數量、失敗原因或未執行原因。

## 紀錄模板

```markdown
## YYYY-MM-DD - 變更摘要

- 版本：
- 變更範圍：
- 測試環境：

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `pytest -q` |  |
| `ruff check app tests` |  |

### Dashboard 驗證

- 驗證網址：
- 實際結果：
- 未執行原因：

### Docker 驗證

- 驗證命令：
- 實際結果：
- 未執行原因：

### 限制與已知問題

-
```
# 測試紀錄

## 2026-05-24 - Dashboard 全頁面雙語標題與中文標註

- 版本：`v0.5.0`
- 變更範圍：Dashboard server-rendered templates 新增英文主標＋繁體中文標註，涵蓋 `/dashboard`、`/dashboard/runs`、`/dashboard/runs/{run_id}`、`/dashboard/approvals`、`/dashboard/worker`、`/dashboard/observability`、`/dashboard/playground`；同步修正 `docs/dashboard.md` 亂碼與頁面說明。
- 測試環境：Windows PowerShell、Python 3.11、Docker Desktop、Codex in-app Browser。

### 測試命令

| 命令 | 實際結果 |
| --- | --- |
| `pytest tests/test_api.py::test_dashboard_pages_render` | 先失敗，錯誤原因為 `/dashboard` 缺少 `防護總覽`，確認測試可抓到缺中文標註的問題。 |
| `pytest tests/test_api.py::test_dashboard_pages_render` | 修改 templates 後通過，1 passed。 |
| `pytest` | 通過，20 passed。 |
| `ruff check app tests` | 通過，All checks passed。 |
| `docker info` | sandbox 內因 Docker API 權限被拒；使用核准後的 Docker 權限重跑成功，Docker Desktop 可用。 |
| `docker compose ps` | 通過，api、worker、postgres、redis、kafka 皆為 Up，postgres/redis/kafka healthy。 |
| `docker compose up -d --build` | 通過，api 與 worker image 重新 build，api/worker container 重新建立並啟動。 |
| `Invoke-WebRequest http://localhost:8000/dashboard...` | 通過，Overview、Runs、Approvals、Worker、Observability、Playground 皆回 HTTP 200，且各頁包含對應中文標註。 |

### Dashboard 驗證

- 驗證網址：`http://localhost:8000/dashboard`、`/dashboard/runs`、`/dashboard/runs/{run_id}`、`/dashboard/approvals`、`/dashboard/worker`、`/dashboard/observability`、`/dashboard/playground`。
- 實際結果：用 Codex in-app Browser 載入每個 Dashboard 頁面，英文主標與繁體中文標註皆可見。Run detail 測試 run 為 `0335ab43-c891-4f91-b99a-42d4fe9ae98d`。`Observability Trends` 保留英文，並顯示 `可觀測性趨勢`；`SLO` 保留英文，並顯示 `服務水準目標`。頁面 heading 與中文標註的垂直間距檢查通過，沒有發現標題重疊；browser console error/warn 為空。
- 截圖：已由 Browser 驗證流程擷取 `/dashboard/observability` 畫面，可看到 `Observability Trends / 可觀測性趨勢` 與 `SLO / 服務水準目標`。
- 未執行原因：不適用，本次已執行 Docker 版 Dashboard 點擊/載入驗證。

### Docker 驗證

- 驗證命令：`docker info`、`docker compose ps`、`docker compose up -d --build`。
- 實際結果：Docker Desktop 可用；重新 build 後 `rewardhacking-api-1` 與 `rewardhacking-worker-1` 已重建並啟動，Postgres、Redis、Kafka 仍維持 healthy。
- 限制或已知問題：一般 sandbox 權限無法直接連到 Docker API，需要核准後的 Docker 權限執行 Docker 驗證。
