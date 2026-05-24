# 測試紀錄

這份文件用來記錄本專案每次完成實作或文件同步後，實際執行過的測試與驗證結果。

請不要只填預期結果；如果某項驗證沒有執行，要清楚寫明原因。例如：純文件修改未啟動 Docker、Docker Desktop 尚未開啟、或本次沒有影響 Dashboard。

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
