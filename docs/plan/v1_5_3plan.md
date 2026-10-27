# RewardHacking `v1.5.3` 版本開發與功能規劃書 (`v1.5.3Plan.md`)

- **專案名稱**：RewardHacking — Autonomous Agent Guardrail & Governance Control Plane
- **當前基線版本**：`v1.5.2`（Alembic Head：`202606010004`）
- **目標規劃版本**：`v1.5.3`
- **版本定位**：**Active FinOps Budget Enforcement, Context-Aware Pricing Engine & FinOps Governance Portal**（主動式 FinOps 預算攔截、階層式/租戶定價計費引擎與預算違規治理中心）
- **文件規範依據**：嚴格遵循專案根目錄 `AGENTS.md` 第 1 至第 6 節之規劃、UI/Dashboard 驗證、自動化測試與文件同步規範（僅規劃，尚未執行實作）。

## 1. 背景分析與現況缺口盤點 (Current Baseline & Gap Analysis)

在 `v1.5.0` 至 `v1.5.2` 版本中，專案已建立 Agent FinOps Core 的資料表結構（`pricing_catalog`、`usage_events`、`cost_events`、`budget_policies`、`budget_violations`、`pricing_import_batches`、`pricing_change_logs`、`pricing_source_documents`）、官方模型定價 YAML Seed 匯入器，並完成全英文開源文件國際化與安全掃描。

然而，深入檢視現有程式碼（`app/services/finops.py`、`app/services/pricing_import.py`、`app/api/routes.py`、`app/ui/routes.py`）與 `docs/test_report.md` 的實測限制紀錄，在進入 `v1.6.0`（Safe MCP Gateway & Dynamic Token Broker）之前，`v1.5.x` 產品線仍存在以下 **5 大核心功能缺口**，亟需於 `v1.5.3` 收斂完成：

| 缺口編號 | 模組領域 | 現有程式碼與測試報告記載之具體缺口 | 影響範圍 |
| :--- | :--- | :--- | :--- |
| **Gap 1** | **主動式預算攔截與多維度預算核算** | `docs/test_report.md` 明確記載：「V1.5 只做成本帳本資料層；budget violation 只記錄與查詢，不會阻擋 run，也不會改變 model/tool routing。」且 `app/services/finops.py` 的 `_check_budgets()` 目前僅檢查 `policy.scope == "run"` 與 `hard_limit_amount`，完全忽略資料表已定義的 `tenant`、`workflow`、`project`、`model`、`tool` 範圍，以及 `soft_limit_amount`、`period`（`daily`/`weekly`/`monthly`）、`actions_on_soft_limit`、`actions_on_hard_limit`、`fallback_model_tier`。 | `app/services/finops.py`<br>`app/services/control_plane.py`<br>`app/api/routes.py`<br>`app/ui/routes.py` |
| **Gap 2** | **階層式定價查找、租戶合約價與節約核算** | `_lookup_pricing()` 未過濾 `effective_from` / `effective_to` 有效期間、未優先匹配租戶覆寫價（`scope_type="tenant_override"`）、未根據 `usage.input_tokens` 比對 `input_threshold_tokens_min` / `max`（導致如 `gemini-3.1-pro-preview` 200k 長文本階層與 `minimax-m3` 512k 階層無法正確匹配）；此外 `_record_cost_for_usage()` 未扣除 `free_quota_quantity`，且 `TenantCostSummary` 的 `cache_savings` 與 `batch_savings` 永遠為 `0`。 | `app/services/finops.py`<br>`app/schemas/finops.py` |
| **Gap 3** | **預算政策管理 UI 與違規處置工作流** | `docs/test_report.md` 記載：「目前 Costs Dashboard 是查詢型頁面，尚未提供建立 budget policy 的表單。」同時 `budget_violations` 資料表雖已具備 `acknowledged_by`、`resolved_by`、`resolution_status`（`open`/`acknowledged`/`resolved`/`ignored`）欄位，但完全沒有對應的 API 端點或 Dashboard 處置按鈕。 | `app/api/routes.py`<br>`app/ui/routes.py`<br>`app/ui/templates/costs.html`<br>`app/ui/templates/admin_finops_budgets.html` |
| **Gap 4** | **定價手動修訂稽核、匯入過期預警與精度顯示** | 手動呼叫 `PATCH /v1/admin/finops/pricing/{id}`、`/dashboard/admin/finops/pricing/{id}/edit` 或 `disable` 時，未寫入 `PricingChangeLog`，違反 `docs/pricing/model_pricing_seed.md` 不可變稽核原則；`validate_pricing_seed()` 未實作 `stale_sources` 與促銷無到期日 `warnings` 檢查；且 Pricing Detail 頁面將極小 Decimal（如 `0.000000001`）顯示為科學記號 `0E-8`。 | `app/services/finops.py`<br>`app/services/pricing_import.py`<br>`app/ui/routes.py`<br>`app/ui/templates/admin_finops_pricing_detail.html` |
| **Gap 5** | **FinOps 可觀測性指標與 Pydantic V2 警告清理** | `pytest -q` 執行時產生 85 個 Pydantic V2 `json_encoders` deprecation warnings（位於 `app/schemas/finops.py`）；`/v1/metrics` 缺少 FinOps 成本與預算違規 Prometheus 指標；`/dashboard/operations` 缺少 FinOps 準備度檢查項。 | `app/schemas/finops.py`<br>`app/observability/metrics.py`<br>`app/services/operations.py` |

## 2. 功能目標與具體驗收標準 (Functional Goals & Acceptance Criteria)

### 功能一：主動式多維度預算執行與控制面攔截 (Active Multi-Scope Budget Enforcement & Control-Plane Guardrails)

- **功能目標**：
  1. **多維度與時間週期預算評估 (`evaluate_budget_policies`)**：擴充預算檢查引擎，支援 `scope` 為 `run`、`tenant`、`workflow`、`project`、`model`、`tool`，並根據 `period`（`per_run`、`daily`、`weekly`、`monthly`、`quarterly`、`yearly`）與 `budget_basis`（`estimated`、`actual`、`net`、`gross`、`base_currency`）計算週期內累計花費與執行前剩餘預算 `remaining_budget_before`。
  2. **篩選條件支援**：精確套用 `BudgetPolicy` 上的 `include_failed_runs`、`include_replay_runs`、`include_dev_environment`、`provider`、`model_name`、`model_tier`、`tool_id`、`workflow_id` 與有效期間（`effective_from` 至 `effective_to`）。
  3. **軟硬門檻雙重判定 (`soft_limit_amount` vs `hard_limit_amount`)**：
     - 當累計成本超過 `soft_limit_amount` 但未達 `hard_limit_amount` 時，建立 `threshold_type="soft"` 之 `BudgetViolation`，執行 `actions_on_soft_limit`（如 `warn`、`notify`、`degrade_model`）。
     - 當累計成本超過 `hard_limit_amount`（或 `limit_amount`）時，建立 `threshold_type="hard"` 之 `BudgetViolation`，執行 `actions_on_hard_limit`（如 `fail_closed`、`require_approval`、`dry_run_only`）。
  4. **提交階段與控制面主動攔截 (`POST /v1/agent/submit*` 與 `/dashboard/playground`)**：
     - **`fail_closed` 動作**：在提交階段估算成本後，若觸發 `fail_closed`，立即將 `AgentRun.status` 轉為 `denied`，附加 `policy_flags=["finops_budget_hard_limit_exceeded"]`，寫入 `Incident`（`severity="high"`, `code="finops_budget_exceeded"`），且**不發送** `agent.requested` 事件至 Kafka。
     - **`require_approval` 動作**（或 `approval_required=True`）：將 `AgentRun.status` 轉為 `awaiting_approval`，於 `ExecutionPlan.approval_gates` 新增 `{"gate_type": "finops_budget_gate", "status": "pending", "budget_id": ...}`，並將 `ApprovalEvent` 關聯至 `BudgetViolation.approval_id`。
     - **`degrade_model` 動作**：若設定 `fallback_model_tier`（例如從 `premium_reasoning` 降級為 `standard` 或 `cheap`），由 `ControlPlanePlanner` 將 `ExecutionPlan.model_route` 更新為降級後的模型與層級，並記錄 `routing_reason` 與 `policy_flags=["finops_model_degraded"]`。
- **具體驗收標準 (Acceptance Criteria)**：
  - 設定 `scope="tenant"`, `period="monthly"`, `hard_limit_amount=0.0001`, `actions_on_hard_limit=["fail_closed"]` 後提交 Run，API 回傳 `status="denied"`、`policy_flags` 包含 `"finops_budget_hard_limit_exceeded"`，且 `GET /v1/incidents` 可查得 `finops_budget_exceeded` 事件。
  - 設定 `actions_on_hard_limit=["require_approval"]` 後提交 Run，API 回傳 `status="awaiting_approval"`，且出現在 `/dashboard/approvals` 佇列中；人工核准後可正常排入 `queued`。
  - 設定 `soft_limit_amount=0.00001`, `actions_on_soft_limit=["degrade_model"]`, `fallback_model_tier="cheap"` 後提交 Run，`GET /v1/runs/{run_id}/execution-plan` 的 `model_route` 顯示已降級至 `cheap` tier 且 `budget_violations` 記錄 `action_taken="degrade_model"`。

### 功能二：階層式定價查找、租戶專屬合約價、免費額度與節約金額核算 (Context-Tiered Pricing, Tenant Overrides, Free Quotas & Savings Accounting)

- **功能目標**：
  1. **智慧定價匹配演算法 (`_lookup_pricing` 升級)**：
     - **時間有效性過濾**：僅匹配 `effective_from <= event_time` 且 `(effective_to IS NULL OR effective_to >= event_time)` 的定價列，自動排除過期促銷規則。
     - **Scope 優先序解析**：依序優先匹配 `tenant_override`（且 `scope_id == usage.tenant_id`）> `contract`（`scope_id == usage.org_id` 或 `usage.tenant_id`）> `system_default` > `manual`。
     - **Token 門檻階層匹配**：當 `usage.input_tokens > 0` 時，優先匹配符合 `input_threshold_tokens_min <= usage.input_tokens <= input_threshold_tokens_max` 的定價列（完美支援 `gemini-3.1-pro-preview` 200k 門檻與 `minimax-m3` 512k 門檻）。
     - **計費模式匹配**：支援從 `usage.metadata` 或請求上下文讀取 `pricing_mode`（`standard`、`batch`、`priority`、`promotion`）與 `service_tier`。
  2. **免費額度扣抵與最低計費單位 (`_billable_quantity` & `_record_cost_for_usage`)**：
     - 若定價列設定了 `free_quota_quantity > 0` 與 `free_quota_period`（如 `monthly`），查詢該租戶於當期同 `pricing_id` 已累計的 `UsageEvent` 數量，僅對超出免費額度的部分計費，並在 `CostEvent.metadata_payload` 記錄 `free_quota_applied_quantity`。
     - 若計費數量大於 0 且小於 `minimum_billable_quantity`，則以 `minimum_billable_quantity` 計費。
  3. **快取與批次節約金額核算 (`cache_savings` 與 `batch_savings`)**：
     - 在 `tenant_cost_summary()` 與 `run_cost_summary()` 中，針對 `token_type="cached_input"` 的使用事件，比對同模型 `standard` `input` 單價與實際 `cached_input` 單價之差額，累加為 `cache_savings`。
     - 針對 `pricing_mode="batch"` 的成本事件，將 `gross_cost - net_cost`（或與同模型 `standard` 單價之差額）累加為 `batch_savings`。
- **具體驗收標準 (Acceptance Criteria)**：
  - 匯入 `app/seeds/model_pricing_seed.yaml` 後，送入兩筆 `gemini-3.1-pro-preview` 的 `UsageEvent`（`input_tokens=100,000` 與 `input_tokens=250,000`），產生的 `CostEvent.unit_price` 分別為 `2.00` USD 與 `4.00` USD。
  - 為特定租戶新增 `scope_type="tenant_override"`, `scope_id="vip-tenant"` 的定價列後，該租戶的 `UsageEvent` 優先採用覆寫單價，其餘租戶仍採用 `system_default` 單價。
  - 送入含 `cached_input_tokens` 與 `batch` 模式的 `UsageEvent` 後，`GET /v1/cost/tenants/{tenant_id}` 回傳的 `cache_savings > 0` 且 `batch_savings > 0`。

### 功能三：預算政策管理與預算違規處置工作流 (Budget Policy & Violation Lifecycle Portal — API + Dashboard UI)

- **功能目標**：
  1. **完整預算政策與違規處置 REST API**：
     - `GET /v1/budgets/{budget_id}`：取得單筆預算政策。
     - `POST /v1/budgets/{budget_id}/disable`：停用預算政策（設為 `is_active=False`, `effective_to=now()`）。
     - `GET /v1/budget-violations/{violation_id}`：取得單筆預算違規紀錄（支援附加 `resolution_status` 過濾於 `GET /v1/budget-violations`）。
     - `POST /v1/budget-violations/{violation_id}/acknowledge`：由 `operator`、`reviewer`、`finops_admin` 或 `admin` 確認違規，更新 `resolution_status="acknowledged"`、`acknowledged_by`、`acknowledged_at`。
     - `POST /v1/budget-violations/{violation_id}/resolve`：由 `finops_admin` 或 `admin` 結案或忽略違規，支援 `resolution_status` 為 `"resolved"` 或 `"ignored"`，更新 `resolved_by`、`resolved_at` 與 `details["resolution_note"]`。
  2. **新增 Admin FinOps Budgets 管理頁面 (`/dashboard/admin/finops/budgets`)**：
     - 提供視覺化表單建立與更新 `BudgetPolicy`（涵蓋 `tenant_id`、`scope`、`scope_id`、`policy_name`、`period`、`soft_limit_amount`、`hard_limit_amount`、`budget_basis`、`actions_on_soft_limit`、`actions_on_hard_limit`、`fallback_model_tier`、`approval_required`）。
     - 列表顯示所有預算政策及其當前使用率、狀態與一鍵 `Disable` 按鈕。
  3. **升級 Costs Dashboard (`/dashboard/costs`)**：
     - 新增 `Cache Savings` 與 `Batch Savings` 統計卡片。
     - 在 `Budget Violations` 區塊新增 `Resolution Status` 欄位與內嵌處置操作表單（`Acknowledge`、`Resolve`、`Ignore`），並提供前往 `/dashboard/admin/finops/budgets` 的快捷按鈕。
- **具體驗收標準 (Acceptance Criteria)**：
  - 可透過瀏覽器或 TestClient 在 `/dashboard/admin/finops/budgets` 表單直接建立一筆預算政策，無須手動呼叫 JSON API。
  - 當產生 `BudgetViolation` 後，操作員可在 `/dashboard/costs` 點擊 `Acknowledge` 與 `Resolve`，頁面與 `GET /v1/budget-violations/{id}` 狀態即時更新為 `acknowledged` / `resolved`，且記錄操作者與時間戳記。

### 功能四：定價目錄不可變修訂稽核、Seed 匯入過期預警與 Decimal 顯示精度修正 (Immutable Pricing Revision Audit, Seed Staleness Warnings & Decimal Formatting)

- **功能目標**：
  1. **手動修訂與停用自動寫入 `PricingChangeLog` 並支援 Supersede 模式**：
     - 修改 `update_pricing_item()` 與 `disable_pricing_item()`：每次透過 API (`PATCH /v1/pricing/{id}`, `PATCH /v1/admin/finops/pricing/{id}`, `POST /v1/admin/finops/pricing/{id}/disable`) 或 Dashboard (`/dashboard/admin/finops/pricing/{id}/edit`, `/disable`) 變更定價時，必須在同一個資料庫 Transaction 中寫入 `PricingChangeLog`（記錄 `change_type="update"` / `"disable"` / `"supersede"`、`old_unit_price`、`new_unit_price`、`change_reason`、`changed_by`）。
     - 在 `PricingCatalogUpdateRequest` 與 Pricing Detail 編輯表單新增 `supersede_existing: bool = False` 選項：勾選時不直接覆寫原 `pricing_id` 的 `unit_price`，而是將舊列停用（`is_active=False`, `effective_to=now()`）並產生新 `pricing_id`（設定 `supersedes_pricing_id` 指向舊列），完全對齊 `docs/pricing/model_pricing_seed.md` 規範。
  2. **Seed 驗證器擴充 `stale_sources` 與 `warnings` (`validate_pricing_seed`)**：
     - 檢查 `source_documents` 與 `pricing_items` 的 `source_checked_at`：若距今超過 `PRICING_SOURCE_STALE_DAYS`（預設 30 天），將該筆來源加入 `report.stale_sources` 並附加 warning。
     - 檢查 `pricing_mode == "promotion"` 但未設定 `effective_to`，或 `token_type == "cache_storage"` 且 `unit_price == 0`（限時免費）之項目，加入 `report.warnings`。
     - 若 `PricingImportRequest(strict=True, allow_warnings=False)` 且存在 `warnings` 或 `stale_sources` 時，拒絕套用並回傳 `status="invalid"`。
  3. **消除科學記號顯示 (`decimal_plain` Jinja2 Filter)**：
     - 在 `app/ui/routes.py` 註冊 `decimal_plain` filter（將 `Decimal` 格式化為去除尾端多餘零的純十進位字串，例如 `Decimal("0.000000001")` 顯示為 `"0.000000001"` 而非 `"0E-8"` 或 `"1E-9"`），套用至 `admin_finops_pricing.html`、`admin_finops_pricing_detail.html`、`costs.html` 與 `run_detail.html`。
- **具體驗收標準 (Acceptance Criteria)**：
  - 在 `/dashboard/admin/finops/pricing/{pricing_id}` 修改 `unit_price` 為 `0.000000001` 並儲存後，頁面 `Unit Price` 顯示為 `0.000000001`（不出現 `0E-8`），且下方 `Change Log` 表格立即新增一筆 `change_type="update"`（或 `"supersede"`）的稽核紀錄。
  - 對過期 `source_checked_at` 或無 `effective_to` 的促銷項目執行 `dry_run_pricing_seed()`，回傳報告中 `stale_sources` 與 `warnings` 非空，且在 `/dashboard/admin/finops/pricing/import` 頁面清楚顯示預警清單。

### 功能五：FinOps 可觀測性指標、Operations Gate 成本健康檢查與 Pydantic V2 零警告 (FinOps Observability, Operations Gate & Zero Warnings)

- **功能目標**：
  1. **Pydantic V2 序列化重構（零警告）**：
     - 在 `app/schemas/finops.py` 移除已棄用的 `ConfigDict(..., json_encoders={Decimal: str})`，改於 `FinOpsBaseModel` 或 `Decimal` 型別別名（`SerializedDecimal = Annotated[Decimal, PlainSerializer(lambda v: format(v, "f"), return_type=str, when_used="json")]`）統一處理 JSON 序列化，使 `pytest -q` 達到 **0 warnings**。
  2. **新增 FinOps Prometheus Metrics (`app/observability/metrics.py`)**：
     - `FINOPS_USAGE_EVENTS_TOTAL` (`agent_finops_usage_events_total{provider,resource_type,phase}`)
     - `FINOPS_COST_USD_TOTAL` (`agent_finops_cost_usd_total{tenant_id,cost_type,provider}`)
     - `FINOPS_BUDGET_VIOLATIONS_TOTAL` (`agent_finops_budget_violations_total{tenant_id,scope,threshold_type,decision}`)
     - `FINOPS_PRICING_IMPORTS_TOTAL` (`agent_finops_pricing_imports_total{mode,status}`)
     - 同步擴充 `observability_summary()` 與 `/dashboard/observability`，顯示總成本事件數、開啟中預算違規數與預算攔截率。
  3. **Operations Release Gate 整合 FinOps 檢查 (`app/services/operations.py`)**：
     - 新增檢查項 `finops_open_critical_violations`：若存在 `resolution_status == "open"` 且 `severity in {"high", "critical"}` 的預算違規，回傳 `warn`（`APP_ENV=prod` 時回傳 `fail`）。
     - 新增檢查項 `finops_pricing_catalog_ready`：確認資料庫中至少存在有效定價紀錄，否則回傳 `warn`。
     - 將系統版本、OpenAPI 版本與 `/dashboard/operations` 預設 Release Version 推進至 `v1.5.3`。
- **具體驗收標準 (Acceptance Criteria)**：
  - 執行 `pytest -q` 全數通過且輸出 **0 warnings**（徹底消除原有的 85 個 deprecation warnings）。
  - 呼叫 `GET /v1/metrics` 可檢索到 `agent_finops_usage_events_total`、`agent_finops_budget_violations_total` 等新指標。
  - 開啟 `/dashboard/operations` 可看到 `finops_open_critical_violations` 與 `finops_pricing_catalog_ready` 檢查項，且 Release Gate 表單預設版本為 `v1.5.3`。

## 3. UI / Dashboard 驗證指引 (UI / Dashboard Verification Instructions)

依據 `AGENTS.md` 第 1 節規範，所有新增與修改之使用者可見行為皆須透過 Dashboard 進行端到端操作驗證：

| 驗證頁面與路由 | 導覽方式 | 輸入與操作步驟 | 預期視覺狀態與 DOM 驗證結果 |
| :--- | :--- | :--- | :--- |
| **Admin FinOps — Budgets**<br>`/dashboard/admin/finops/budgets` | 點擊側邊欄 `Admin FinOps` -> 點擊頁面頂部導覽按鈕 `Budgets`（或由 `/dashboard/costs` 點擊 `Manage Budgets`） | 1. 在 `Create Budget Policy` 表單填入：`tenant_id="qa-v153"`, `scope="tenant"`, `period="monthly"`, `soft_limit_amount="0.00010000"`, `hard_limit_amount="0.00050000"`, `actions_on_soft_limit="degrade_model"`, `fallback_model_tier="cheap"`, `actions_on_hard_limit="fail_closed"`。<br>2. 點擊 `Create Policy`。 | 頁面狀態碼 `HTTP 200`；`Active Budget Policies` 表格出現新建立的政策列，完整顯示 Soft/Hard Limits、Actions 與 `Disable` 按鈕。 |
| **Interactive Playground**<br>`/dashboard/playground` | 點擊側邊欄 `Playground` | 1. 填入 `tenant_id="qa-v153"`, `task_type="analysis"`, `allowed_tools="read_data, summarize"`，送出第 1 筆任務（觸發 Soft Limit）。<br>2. 再連續送出任務使累計預估成本超過 Hard Limit (`0.00050000`)。 | 第 1 筆任務顯示 `queued` 且附帶 `finops_model_degraded` flag；超過 Hard Limit 的任務立即在結果面板顯示狀態 `denied`、原因包含 `Budget policy exceeded`，且顯示 `finops_budget_hard_limit_exceeded` 標籤。 |
| **Costs & Violations Portal**<br>`/dashboard/costs` | 點擊側邊欄 `Costs`，於篩選框輸入 `tenant_id="qa-v153"` 並點擊 `Filter` | 1. 檢視頂部統計卡片。<br>2. 在 `Budget Violations` 表格找到剛觸發的違規列（狀態為 `open`）。<br>3. 點擊該列的 `Acknowledge` 按鈕，再填入處置備註點擊 `Resolve`。 | 頂部統計區顯示 `Cache Savings` 與 `Batch Savings` 金額（8 位小數固定格式）；違規列狀態由 `open` 依序變為 `acknowledged` 與 `resolved`，並顯示處理人員與時間。 |
| **Admin FinOps — Pricing Detail**<br>`/dashboard/admin/finops/pricing/{pricing_id}` | 點擊側邊欄 `Admin FinOps` -> 點擊任一模型連結進入詳情頁 | 1. 在 `Simple Edit` 表單將 `Unit Price` 改為 `0.00000001`、`Change Reason` 填入 `"v1.5.3 precision and audit test"`，勾選 `Supersede existing row`。<br>2. 點擊 `Save`。 | 頁面重新導向至新產生的 `pricing_id` 詳情頁；`Unit Price` 顯示為 `0.00000001 USD`（無 `0E-8` 科學記號）；`Supersedes` 欄位顯示舊 `pricing_id`；下方 `Change Log` 表格出現新紀錄。 |
| **Admin FinOps — Pricing Import**<br>`/dashboard/admin/finops/pricing/import` | 點擊 `Admin FinOps` -> `Import` | 輸入 `app/seeds/model_pricing_seed.yaml` 並點擊 `Dry Run`。 | 報告區塊除了顯示 `Valid Rows: 149` 外，新增顯示 `Warnings`（標示限時促銷/無 `effective_to` 項目）與 `Stale Sources` 檢查結果。 |
| **Operations Gate & Observability**<br>`/dashboard/operations`<br>`/dashboard/observability` | 點擊側邊欄 `Operations` 與 `Observability` | 1. 檢視 `/dashboard/operations` 檢查清單與 Release Gate 版本輸入框。<br>2. 檢視 `/dashboard/observability` 指標面板。 | `/dashboard/operations` 包含 `FinOps pricing catalog ready` 與 `No open critical budget violations` 兩項檢查，`release_version` 預設值為 `v1.5.3`；`/dashboard/observability` 顯示 FinOps 成本與違規統計摘要。 |

## 4. 實作設計與受影響模組清單 (Implementation Design & Affected Modules)

本版本**不需要新增資料庫 Migration 也能運作核心邏輯**（因為 `202606010004_expand_v151_finops_core.py` 已預先建立所有需要的資料表與欄位！），所有變更皆為將既有 Schema 欄位徹底接通至服務層、控制面、API 與 UI。

### 4.1 受影響程式碼檔案清單

| 模組分類 | 檔案路徑 | 預計變更內容與設計說明 |
| :--- | :--- | :--- |
| **設定與版本** | `pyproject.toml`<br>`app/main.py`<br>`app/core/config.py` | - 將版本號由 `1.5.2` 更新為 `1.5.3`。<br>- 在 `Settings` (`app/core/config.py`) 新增 `pricing_source_stale_days: int = 30` 與 `finops_budget_enforcement_enabled: bool = True`。 |
| **Pydantic Schemas** | `app/schemas/finops.py` | - 移除 `json_encoders={Decimal: str}`，改用 Pydantic V2 `@field_serializer` / `Annotated[Decimal, PlainSerializer(...)]` 消除 85 個 deprecation warnings。<br>- 新增 `BudgetViolationAcknowledgeRequest`、`BudgetViolationResolveRequest`。<br>- 在 `PricingCatalogUpdateRequest` 新增 `supersede_existing: bool = False`。 |
| **FinOps 服務層** | `app/services/finops.py` | - **定價查找 (`_lookup_pricing`)**：加入 `effective_from` / `effective_to` 時間過濾、`scope_type` 優先序（`tenant_override` > `contract` > `system_default` > `manual`）、以及 `input_threshold_tokens_min` / `max` 區間匹配。<br>- **計費與免費額度 (`_billable_quantity`, `_record_cost_for_usage`)**：實作 `free_quota_quantity` 期內扣抵與 `minimum_billable_quantity` 保底計費。<br>- **節約核算 (`tenant_cost_summary`, `run_cost_summary`)**：計算 `cache_savings` 與 `batch_savings`。<br>- **多維度預算評估 (`evaluate_budget_policies`, `_check_budgets`)**：支援 `run`、`tenant`、`workflow`、`project`、`model`、`tool` scopes 與 `per_run`/`daily`/`weekly`/`monthly` periods；支援 `soft_limit_amount` 與 `hard_limit_amount` 雙門檻；回傳 `BudgetEnforcementOutcome`（含 `decision`, `triggered_actions`, `fallback_model_tier`, `violations`）。<br>- **違規處置 (`acknowledge_budget_violation`, `resolve_budget_violation`, `disable_budget_policy`)**：實作狀態轉換與時間戳記寫入。<br>- **手動定價變更稽核 (`update_pricing_item`, `disable_pricing_item`)**：自動寫入 `PricingChangeLog`，並支援 `supersede_existing=True` 產生新定價列。 |
| **Seed 匯入服務** | `app/services/pricing_import.py` | - 在 `validate_pricing_seed()` 中實作 `stale_sources`（檢查 `source_checked_at` 是否超過 `stale_days`）與 `warnings`（檢查 `pricing_mode == "promotion"` 缺少 `effective_to` 等情境）。<br>- 在 `dry_run_pricing_seed()` 與 `apply_pricing_seed()` 支援 `strict` 與 `allow_warnings` 參數控制。 |
| **控制面與編排** | `app/services/control_plane.py`<br>`app/services/orchestrator.py` | - `ControlPlanePlanner.plan()` 支援接收 `budget_outcome`：當觸發 `degrade_model` 時自動套用 `fallback_model_tier`；當觸發 `require_approval` 時加入 `finops_budget_gate` 至 `approval_gates`；並將預設 `routing_reason` 更新為 `V1.5.3`。 |
| **可觀測性與營運** | `app/observability/metrics.py`<br>`app/services/audit_log.py`<br>`app/services/operations.py` | - 新增 4 組 FinOps Prometheus Counter/Histogram 指標。<br>- `observability_summary()` 新增 `finops` 統計字典。<br>- `compute_operations_readiness()` 新增 `finops_pricing_catalog_ready` 與 `finops_open_critical_violations` 兩項檢查。 |
| **REST API 路由** | `app/api/routes.py` | - 在 `/v1/agent/submit` 與 `/v1/agent/submit-with-details` 串接主動式預算攔截（支援 `fail_closed` 轉 `denied` + 寫入 `Incident`、`require_approval` 轉 `awaiting_approval`、`degrade_model` 模型降級）。<br>- 新增 `GET /v1/budgets/{budget_id}`、`POST /v1/budgets/{budget_id}/disable`、`GET /v1/budget-violations/{violation_id}`、`POST /v1/budget-violations/{violation_id}/acknowledge`、`POST /v1/budget-violations/{violation_id}/resolve`。 |
| **Dashboard UI 路由與模板** | `app/ui/routes.py`<br>`app/ui/templates/costs.html`<br>`app/ui/templates/admin_finops_pricing.html`<br>`app/ui/templates/admin_finops_pricing_detail.html`<br>`app/ui/templates/admin_finops_import.html`<br>`app/ui/templates/admin_finops_budgets.html` (新增)<br>`app/ui/templates/observability.html`<br>`app/ui/templates/operations.html` | - 註冊 `decimal_plain` Jinja2 filter 解決 `0E-8` 顯示問題。<br>- 新增 `/dashboard/admin/finops/budgets` GET/POST 路由與模板，以及 `/dashboard/costs` 的違規 Acknowledge/Resolve POST 路由。<br>- 更新 `/dashboard/operations` 預設版本為 `v1.5.3`。 |

### 4.2 核心流程架構圖 (`v1.5.3` 主動式 FinOps 控制面閉環)

```
[Client Submit: POST /v1/agent/submit* or /dashboard/playground]
       │
       ▼
1. Auth & Redis Tenant Quota Check
       │
       ▼
2. Prompt Sanitizer (Classical Chinese Guard) & Multi-Factor Risk Scoring
       │
       ▼
3. Pre-Execution Cost Estimation & Multi-Scope Budget Evaluation (NEW in v1.5.3)
   ├─ Lookup Pricing: Effective Date -> Tenant Override -> Input Token Tier (<=200k / >200k)
   ├─ Apply Free Quota & Minimum Billable Quantity
   └─ Check Active Budgets (run / tenant / workflow / project / model / tool):
       ├─ [Within Budget] ─────────► Normal Control Plane Plan
       ├─ [Soft Limit: degrade_model] ► Downgrade ExecutionPlan.model_route to fallback_model_tier
       ├─ [Hard Limit: require_approval] ► Add finops_budget_gate -> Status = awaiting_approval
       └─ [Hard Limit: fail_closed] ────► Status = denied + Log Incident (Skip Kafka Queue)
```

## 5. 驗證測試套件與可執行指令 (Verification Test Suite & Executable Commands)

實作完成後，工程師必須新增專屬測試檔 `tests/test_v153_finops_governance.py`，並依序執行以下完整測試套件：

### 5.1 新增單元與整合測試案例 (`tests/test_v153_finops_governance.py`)

1. `test_v153_zero_pydantic_deprecation_warnings`：驗證載入所有 Schemas 與執行 API 序列化時無任何 `PydanticDeprecatedSince20` 警告。
2. `test_v153_context_tiered_and_tenant_override_pricing_lookup`：驗證長文本階層定價（`<=200,000` vs `>=200,001` tokens）、租戶專屬覆寫價（`tenant_override` 優先於 `system_default`）、以及過期 `effective_to` 自動排除。
3. `test_v153_free_quota_deduction_and_cache_batch_savings`：驗證 `free_quota_quantity` 扣抵後計費正確，且 `GET /v1/cost/tenants/{tenant_id}` 正確回傳 `cache_savings` 與 `batch_savings`。
4. `test_v153_active_budget_enforcement_fail_closed_and_approval_gate`：驗證跨 Run 的 `tenant` / `workflow` 預算超額時，`fail_closed` 會將新提交的 Run 阻擋為 `denied` 並寫入 `Incident`；`require_approval` 會將 Run 轉為 `awaiting_approval` 並附加 `finops_budget_gate`。
5. `test_v153_budget_soft_limit_model_degradation`：驗證觸發 `soft_limit_amount` 且設定 `degrade_model` + `fallback_model_tier="cheap"` 時，`ExecutionPlan.model_route` 自動降級。
6. `test_v153_budget_violation_acknowledge_and_resolve_workflow`：驗證 `POST /v1/budget-violations/{id}/acknowledge` 與 `/resolve` 的 RBAC 權限與狀態轉換，以及 `/dashboard/costs` 與 `/dashboard/admin/finops/budgets` 頁面互動。
7. `test_v153_manual_pricing_edit_writes_changelog_and_decimal_plain_render`：驗證手動編輯與停用定價會寫入 `PricingChangeLog`、支援 `supersede_existing`，且頁面渲染 `0.00000001` 不會出現 `0E-8`。
8. `test_v153_seed_validation_stale_sources_and_promotion_warnings`：驗證 `validate_pricing_seed()` 對過期來源與無到期日促銷項目回傳 `stale_sources` 與 `warnings`，以及 `strict=True` 阻擋行為。
9. `test_v153_finops_prometheus_metrics_and_operations_readiness`：驗證 `/v1/metrics` 輸出 `agent_finops_*` 指標，且 `/v1/operations/readiness` 包含 FinOps 健康檢查項。

### 5.2 確切可執行驗證指令 (Exact Executable Shell Commands)

```bash
# 1. 靜態安全掃描與程式碼風格檢查
python scripts/security_check.py
ruff check app tests scripts
git diff --check

# 2. 執行 v1.5.3 專屬測試與 FinOps 迴歸測試（確認 0 warnings）
pytest tests/test_v153_finops_governance.py tests/test_v151_finops_core.py tests/test_v15_finops_core.py -q -W error::DeprecationWarning

# 3. 執行全量自動化測試套件
pytest -q

# 4. 驗證官方模型定價 Seed Dry-Run 檢查
python scripts/import_pricing_catalog.py --dry-run --file app/seeds/model_pricing_seed.yaml --created-by "v1.5.3-verify"

# 5. Docker Compose 完整環境啟動與 Migration 驗證（執行前請確認 Docker Desktop 已啟動）
docker compose up -d --build
docker compose ps
docker compose exec -T api alembic upgrade head
docker compose exec -T api alembic current

# 6. 端點健康檢查與 Dashboard 路由驗證
curl -f http://localhost:8000/v1/healthz
curl -f http://localhost:8000/v1/readyz
curl -s http://localhost:8000/v1/operations/readiness
curl -s http://localhost:8000/dashboard/admin/finops/budgets
curl -s http://localhost:8000/dashboard/costs
curl -s http://localhost:8000/dashboard/operations
```

## 6. 實作後需同步更新之文件清單 (Documentation Synchronization Checklist)

依據 `AGENTS.md` 第 4 節「Documentation Synchronization Requirements」，實作並通過所有測試後，必須同步更新以下文件（保留歷史章節，不刪除既有脈絡）：

1. `README.md`：將 `Current Release` 更新為 `v1.5.3`，新增 `v1.5.3` 功能摘要，並將 `v1.5.2` 移至 `Release History & Evolutionary Context`。
2. `docs/OVERVIEW.md`：更新 `Current Version: v1.5.3`、`Previous Version: v1.5.2`，並補充 `v1.5.3` 演進紀錄。
3. `docs/roadmap.md`：更新 `Roadmap Baseline: v1.5.3`，於里程碑表格新增 `v1.5.3`（Active FinOps Budget Enforcement & Governance Portal）。
4. `docs/v1.5.1_finops_core.md`：擴充說明 `v1.5.3` 新增的多維度主動式預算攔截、長文本階層定價匹配、免費額度扣抵、`/dashboard/admin/finops/budgets` 介面與違規處置 API。
5. `docs/api.md`：更新 `API Version: v1.5.3`，補齊 `/v1/budgets/{budget_id}`、`POST /v1/budgets/{budget_id}/disable`、`GET /v1/budget-violations/{violation_id}`、`POST /v1/budget-violations/{violation_id}/acknowledge`、`POST /v1/budget-violations/{violation_id}/resolve` 規格。
6. `docs/dashboard.md`：更新 `Dashboard Version: v1.5.3`，新增 `/dashboard/admin/finops/budgets` 導覽說明與 `/dashboard/costs` 違規處置操作流程。
7. `docs/data_model.md`：更新 `Data Model Version: v1.5.3`，補充 `BudgetPolicy` 與 `BudgetViolation` 主動攔截與處置狀態機說明。
8. `docs/observability.md`：更新 `Observability Baseline: v1.5.3`，新增 4 項 `agent_finops_*` Prometheus 指標說明。
9. `docs/configuration.md`：新增 `PRICING_SOURCE_STALE_DAYS` 與 `FINOPS_BUDGET_ENFORCEMENT_ENABLED` 環境變數說明。
10. `docs/runbook.md` 與 `docs/testing_guide.md`：補充預算硬門檻攔截（`finops_budget_hard_limit_exceeded`）之故障排除與測試指引。
11. `docs/test_report.md`：於檔案最上方新增 `v1.5.3` 實測報告區塊。

## 7. 需記錄至 `docs/test_report.md` 之測試數據與指標 (Required Test Report Metrics)

實作完成後，必須將實際執行結果依 `docs/test_report.md` 頂部格式寫入，並包含以下具體量化數據與證據：

- **測試總數與警告數**：記錄 `pytest -q` 通過總數（預計由 `74 passed` 增加至 `83+ passed`）以及 `0 warnings`（驗證 85 個 Pydantic V2 warnings 已完全清零）。
- **靜態檢查與安全掃描結果**：記錄 `python scripts/security_check.py`、`ruff check app tests scripts`、`git diff --check` 之實際輸出。
- **定價匯入與預警統計**：記錄 `import_pricing_catalog.py --dry-run` 之 `valid_rows`、`unchanged_rows`、`warnings` 數量與 `stale_sources` 數量。
- **主動式預算攔截與處置實測 ID**：
  - 測試用 Tenant ID（例如 `qa-v153-finops`）與建立之 `budget_id`。
  - 觸發 `degrade_model`、`require_approval`、`fail_closed` 的實際 `run_id` 與對應之 `violation_id`。
  - 執行 `acknowledge` 與 `resolve` 後的 `resolution_status`、`acknowledged_at`、`resolved_at` 實際回傳值。
  - 手動編輯定價後產生的 `change_id` (`pcl_...`) 與新舊 `pricing_id` 對照。
- **Dashboard 與 Docker 驗證狀態**：記錄各容器 (`api`, `worker`, `postgres`, `redis`, `kafka`) 健康狀態、Alembic Head (`202606010004`)、瀏覽器或 HTTP 驗證之所有路由清單與 Console `0 error / 0 warn` 驗證結果。
