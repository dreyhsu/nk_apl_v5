# NK APL 文件索引

## 交接閱讀順序

1. [專案目前規格](../README_APL_ZH.md)
2. [完整交接手冊](HANDOVER_ZH.md)
3. [QTI 相機與 MediaMTX 故障排除](QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md)
4. [工號與掃碼槍操作](SCANNER_OPERATOR_FLOW_ZH.md)
5. [相機停止、釋放與重新啟動修正計畫](CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md)（第 13 節：開發機 cam-server ABRT 定位、主 EOS／NULL 修正、280 項 PC 回歸；修正尚未部署驗收）
6. [APL_fix 相機釋放與重開恢復移植紀錄](CAMERA_RELEASE_RECOVERY_PORT_20261013_ZH.md)（本機移植與待實機驗證）
7. [正式版自動比對與串流整合](PRODUCTION_STREAM_AND_MATCHING_INTEGRATION_20261015_ZH.md)（本機來源、正式 SFIS 設定與板端待驗證項目）
8. [站點狀態輪詢與黃色提示優化紀錄](STATION_STATUS_POLLING_OPTIMIZATION_20261015_ZH.md)（已實作快取狀態顯示、連續失敗警告與節流 log；後端鎖範圍優化待辦）

## 文件狀態

| 文件 | 用途 | 是否代表目前規格 |
|---|---|---|
| `../README_APL_ZH.md` | 操作流程、判定規則、啟動與限制 | 是 |
| `HANDOVER_ZH.md` | 架構、部署、設定、測試與交接清單 | 是 |
| `QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md` | 已驗證的相機與串流故障排除 | 是 |
| `SCANNER_OPERATOR_FLOW_ZH.md` | 工號前置、掃碼槍搜尋與切換 | 是 |
| `CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md` | Ctrl+C、worker 回收、全業務階段／Web／錄影停止計畫與驗證紀錄 | 有實機失敗證據，主 EOS 修正僅 PC 驗證，尚未實機通過 |
| `CAMERA_RELEASE_RECOVERY_PORT_20261013_ZH.md` | APL_fix 分支的相機重開恢復移植、PC 測試與板端待驗證項目 | 本機程式狀態；尚未實機驗收 |
| `PRODUCTION_STREAM_AND_MATCHING_INTEGRATION_20261015_ZH.md` | 3 FPS 非同步探測、WebRTC、RTP 緩衝、正式 DMZ／SFIS 配置與部署核對 | 本機來源；尚未正式機驗收 |
| `STATION_STATUS_POLLING_OPTIMIZATION_20261015_ZH.md` | 偶發黃色 Connecting 提示的現況、鎖競爭原因、UI／log 修正與後續鎖優化 | UI 與診斷已通過 PC 測試；SmartCam 實機待驗證 |
| `archive/` | 上游 VisionEdge、舊四／五步流程與歷史修正 | 否 |

遇到內容不一致時，以目前程式、`config/edge.ini`、
`config/apl_workflow.json` 及根目錄 `README_APL_ZH.md` 為準。
