# APL 英文介面、本機 SN 與影片測試更新

日期：2026-09-08。應用目錄：`NK_APL/VisionEdge_APL`。

本文件描述 PC 開發版。唯一最新版來源為
`D:\work_project\smartcam\NK_APL\VisionEdge_APL`；部署、實機測試及 `.app`
打包均須由使用者另行明確指示。

## 原因與修正

- 舊 SmartCam 圖層和 PC 圖層各自繪製，待辦也使用綠色，造成狀態誤讀。
  改用共用 `workflow_view.py`，保留 Stable/Raw、搜尋 ROI 和有效匹配框。
- 網頁使用 `current_step_id` 標示目前步驟，但後端未提供；已补齊。
- 掃碼後原本需要 DMZ mock 接受才啟動，現在配置為本機 SN 模式；四步依序
  完成後保持結果，操作員 Confirm Completion 才保存 PASS，不傳送 DMZ。
- Live SOP Flow 移到 System Status 上方；溫度是實際感測器讀值，不是假資料。
- 網頁新增影片上傳與獨立測試紀錄，切換來源前確認舊相機已停止；失敗不開第二個 owner。
- 未完成週期遇到影片循環回頭會中止，避免從不同輪次湊齊四步。

## 變更檔案

| 檔案 | 內容 |
| --- | --- |
| `workflow_view.py`（新增） | 共用 Workflow 標籤、色彩和繪圖命令 |
| `apl_matching.py` | PC 使用共用圖層 |
| `overlay_renderer.py` | SmartCam Cairo 使用共用圖層 |
| `apl_station.py` | 本機 SN/PASS、測試標記、目前步驟 ID |
| `config/apl_workflow.json` | 預設 station_mode=local |
| `edge_runtime.py` | 回放進度、循環偵測、restart 停止檢查 |
| `video_test.py`（新增） | 驗證上傳、來源互斥、測試 SQLite 隔離及還原 |
| `system_metrics.py`（新增） | 真實溫度讀取與快取 |
| `visionedge_server.py` | 影片 API、序列化來源/工站操作、溫度狀態 |
| `static/apl_ui.js` | 英文 SN 操作、影片控制及溫度顯示 |
| `static/edge_dashboard.html` | 英文介面、SOP 順序及版面 |
| `static/flow_studio.html` | 英文介面；不變更 APL 正式判定設定 |
| `tests/test_local_workflow.py`（新增） | 本機流程、圖層狀態與 test1.mp4 辨識測試 |
| `tests/test_video_temperature.py`（新增） | 來源切換、上傳及溫度測試 |
| `README_APL_ZH.md` | 更新操作方式與限制 |
| `docs/UI_LOCAL_SN_UPDATE_ZH.md`（新增） | 本文件 |

## 不變的範圍

Golden Sample、ROI `751,254,431,588`、比對門檻、四步順序、QTI 相機管線、
MediaMTX 服務、HTTPS 憑證與正式歷史紀錄不修改。40 系及插入深度仍未驗收。

## 驗證與上線注意

- 52 項單元／整合測試通過，含目前 `test1.mp4` 的真實 Template Matching。
- 瀏覽器已確認英文版面、SOP 位置、上傳及 SN 確認前不推進。
- 影片測試使用相同演算法，但不是 SmartCam 實體工件、掃碼槍或 HDMI 驗收。
- 板端原始程式備份：`/var/rootdirs/opt/visionedge_apl_backups/pre_ui_20260908_1700.tar.gz`。
- 實機驗收仍需現場擺回固定視角，測試四步、遮擋、30 系誤放、拔插掃碼槍，
  並確認長時間溫度與節拍。實際裝置驗證結果會追加於本文件。
