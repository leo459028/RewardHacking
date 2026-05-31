# V1.1 架構圖資（目前歷史圖資）

本目錄保留舊版 `*-v1.png` 圖資，並新增 `*-v11.png` 作為 V1.1 文件與簡報使用的新版架構圖。V1.2 目前新增的是 Control Plane 文字文件、API 與 Dashboard，尚未新增 `*-v12.png` 圖資；因此 `*-v11.png` 保留為歷史圖資與簡報輔助素材。V1.1 圖資使用 GPT-Image2 / 內建影像生成重新製作為中英雙語標註版本，強調 Classical Chinese Prompt Guard、`evaluation_summary.prompt_guard`、`agent_prompt_guard_flags_total{flag}`、Dashboard Prompt Guard panel，以及 V1.0 原有 production readiness baseline 的延續。

## V1.1 圖資清單

| 圖檔 | 用途 |
| --- | --- |
| `architecture-ad-marketing-v11.png` | 專案總覽架構與產品定位 |
| `api-request-flow-v11.png` | API request 從 submit 到 queued / awaiting approval / denied 的流程 |
| `audit-trail-data-model-v11.png` | Postgres audit trail、Prompt Guard summary 與主要資料表 |
| `dashboard-navigation-map-v11.png` | Dashboard 頁面導覽與 Run Detail Prompt Guard panel |
| `frontend-backend-worker-boundary-v11.png` | UI、FastAPI、worker 與 infra 邊界 |
| `observability-metrics-v11.png` | Prometheus metrics、Prompt Guard metric 與 Dashboard data source |
| `operations-gate-v11.png` | Operations evidence、release readiness 與 release gate snapshot |
| `policy-decision-matrix-v11.png` | allow / escalate / deny 決策矩陣與 V1.1 classical flags |
| `production-readiness-roadmap-v11.png` | V1.1 baseline 與後續 production hardening roadmap |
| `project-structure-map-v11.png` | repo 目錄與 V1.1 主要程式位置 |
| `reward-hacking-threat-model-v11.png` | reward hacking threat vectors 與古文繞語攻擊防護 |
| `tenant-governance-v11.png` | tenant、API key、RBAC-lite、quota 與 V1.1 安全證據 |

## 使用建議

- 需要圖片時可引用 `*-v11.png`，但 V1.2 Control Plane 能力應以文字文件為準。
- 舊的 `*-v1.png` 保留作為歷史圖資，不再作為目前版本主圖。
- `*-v11.png` 採中英雙語標註；中文用於閱讀脈絡，英文保留 route、metric、table、flag 與程式碼欄位對照。
- 若 V1.2 修改資料流、schema、metrics 或 Dashboard 頁面，請新增下一組版本化檔名，不要覆蓋既有圖資。
