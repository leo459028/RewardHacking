# 測試紀錄

這份文件用來記錄本專案每次完成實作或文件同步後，實際執行過的測試與驗證結果。

請不要只填預期結果；如果某項驗證沒有執行，要清楚寫明原因。例如：純文件修改未啟動 Docker、Docker Desktop 尚未開啟、或本次沒有影響 Dashboard。

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
