# NK APL 文件索引

## APL_1109 版本現況

最新本機修改：[Capture 與持續取像分離：實作及驗證紀錄](APL_CAPTURE_STREAM_ISOLATION_IMPLEMENTATION_20261109_ZH.md)。背景自動比對、手動拍照身分固定、存證交易與停止保護已實作；370 項 PC 回歸中 366 通過、4 項影片缺失跳過。尚未部署／驗收 SmartCam。

修改前基準見 [APL_1109 新增功能與版本差異整理（2026-11-09）](APL_1109_VERSION_REVIEW_20261109_ZH.md)。本文依 `main@199972c` 的實際程式、設定與 PC 測試整理 PATLITE 三色燈、19／12 碼 SN 檢查、自動檢驗執行方式，以及與先前 10/15 串流優化分支的差異。

[Capture 期間持續取像與直播的修改計畫](plan/APL_CAPTURE_STREAM_ISOLATION_PLAN_20261109_ZH.md) 整理手動比對與存證的阻塞重現、預覽與自動檢驗的分工，以及 PC／SmartCam 的驗收方式。

下列既有 README／交接文件包含尚未更新的 bypass、Step4、前綴與來源版本敘述；本版現況請搭配上述整理、實際設定及環境覆寫核對。

## 交接閱讀順序

1. [專案目前規格](../README_APL_ZH.md)
2. [完整交接手冊](HANDOVER_ZH.md)
3. [QTI 相機與 MediaMTX 故障排除](QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md)
4. [工號與掃碼槍操作](SCANNER_OPERATOR_FLOW_ZH.md)
5. [相機停止、釋放與重新啟動修正計畫](CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md)（第 13 節：開發機 cam-server ABRT 定位、主 EOS／NULL 修正、280 項 PC 回歸；修正尚未部署驗收）
6. [APL_fix 相機釋放與重開恢復移植紀錄](CAMERA_RELEASE_RECOVERY_PORT_20261013_ZH.md)（本機移植與待實機驗證）

## 文件狀態

| 文件 | 用途 | 是否代表目前規格 |
|---|---|---|
| `../README_APL_ZH.md` | 操作流程、判定規則、啟動與限制 | 是 |
| `HANDOVER_ZH.md` | 架構、部署、設定、測試與交接清單 | 是 |
| `QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md` | 已驗證的相機與串流故障排除 | 是 |
| `SCANNER_OPERATOR_FLOW_ZH.md` | 工號前置、掃碼槍搜尋與切換 | 是 |
| `CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md` | Ctrl+C、worker 回收、全業務階段／Web／錄影停止計畫與驗證紀錄 | 有實機失敗證據，主 EOS 修正僅 PC 驗證，尚未實機通過 |
| `CAMERA_RELEASE_RECOVERY_PORT_20261013_ZH.md` | APL_fix 分支的相機重開恢復移植、PC 測試與板端待驗證項目 | 本機程式狀態；尚未實機驗收 |
| `archive/` | 上游 VisionEdge、舊四／五步流程與歷史修正 | 否 |

遇到內容不一致時，以目前程式、`config/edge.ini`、
`config/apl_workflow.json` 及根目錄 `README_APL_ZH.md` 為準。
