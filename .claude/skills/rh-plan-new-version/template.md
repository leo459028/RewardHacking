# RewardHacking `vX.Y.Z` 版本開發與功能規劃書 (`vX_Y_Zplan.md`)

- **專案名稱**：RewardHacking — Autonomous Agent Guardrail & Governance Control Plane
- **當前基線版本**：`vA.B.C`（`main` @ `<commit>`；Alembic Head：`<head>`；測試基線：`<N> passed`；瀏覽器 E2E `<M>` 個主要視圖）
- **目標規劃版本**：`vX.Y.Z`
- **系列定位**：（引用 `docs/roadmap.md` 的系列定位，一句話）
- **本版定位**：（一句話）
- **文件規範依據**：`AGENTS.md` 第 0–6 節；2.3 評估不變量。
- **分支**：從 `main` @ `<commit>` 建立本地分支 `vX.Y.Z`。

---

## 1. 為什麼這樣規劃？

### 1.1 背景（使用者討論結論，YYYY-MM-DD）
1. （使用者已確認的決定，逐條列出）

### 1.2 原始碼盤點出的具體缺口（已核實）

| 缺口 | 問題 | 證據（`file:line`） | 影響 |
| :--- | :--- | :--- | :--- |
| Gap 1 | | | |

### 1.3 版本邊界
- **`vX.Y.Z`（本版本）**：
  1.
- **不在本版範圍**：
  -

### 1.4 設計決議（實作時以本節為準）

| 編號 | 主題 | 決議 |
| :--- | :--- | :--- |
| D1 | | |

---

## 2. 功能目標與驗收標準

### 功能一：
- （可驗證的具體行為；含錯誤情況與單向安全不變量）

---

## 3. UI / Dashboard 驗證指引
1. （路徑、輸入、預期畫面）
- E2E Scenario：（`scripts/browser_manual_test.py` 新增內容；不得破壞 live 資料）

---

## 4. 實作設計與受影響模組

| 模組 | 變更 |
| :--- | :--- |
| | |

---

## 5. 驗證測試套件與指令

### 5.1 新增測試檔 `tests/test_vXYZ_<feature>.py`（規劃約 N 個）
-

### 5.2 指令
```bash
python scripts/phase_check.py tests/test_vXYZ_<feature>.py
python scripts/release_gate.py --out <scratchpad>/release_gate.md
```

---

## 6. 實作後需同步更新的文件
README、`docs/OVERVIEW.md`、`docs/roadmap.md`、`docs/manual_test.md`、`docs/testing_guide.md`、`docs/test_report.md`，以及受影響的 `docs/*.md`、`AGENTS.md`（Quick Map、Alembic head）。

---

## 7. 需記錄至 `docs/test_report.md` 的測試數據
- Stage 2 實際輸出（`release_gate.py` 摘要）、全量 pytest 通過數、Docker `alembic current`、E2E 視圖數與 live ID。

---

## 8. 已知邊界（需寫入文件）
-

---

## 9. 實作進度紀錄（Implementation Progress Log）

> 依 `AGENTS.md` 第 1 節第 7 項維護。接手時先讀本節，再執行 `git status`、`git diff` 與 Stage 1 測試。
> Commit：每階段 Stage 1 通過並更新本表後 `wip(vX.Y.Z): P<n> ...`（`python scripts/phase_check.py ... --commit`）；Stage 2 全數通過並寫入 test report 後 `feat(vX.Y.Z): ...`。不 push。
> 分支：`vX.Y.Z`（本地，從 `main` @ `<commit>` 建立）。

| 階段 | 內容 | 狀態 | 變更檔案 | Stage 1 結果 | 備註／下一步 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| P1 | | 未開始 | | | |
