# NK APL 文件索引

## 交接閱讀順序

1. [專案目前規格](../README_APL_ZH.md)
2. [完整交接手冊](HANDOVER_ZH.md)
3. [QTI 相機與 MediaMTX 故障排除](QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md)
4. [工號與掃碼槍操作](SCANNER_OPERATOR_FLOW_ZH.md)
5. [相機停止、釋放與重新啟動修正計畫](CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md)（第二階段本機實作與 271 項 PC 回歸見第 12.10 節；實機待驗收）

## 文件狀態

| 文件 | 用途 | 是否代表目前規格 |
|---|---|---|
| `../README_APL_ZH.md` | 操作流程、判定規則、啟動與限制 | 是 |
| `HANDOVER_ZH.md` | 架構、部署、設定、測試與交接清單 | 是 |
| `QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md` | 已驗證的相機與串流故障排除 | 是 |
| `SCANNER_OPERATOR_FLOW_ZH.md` | 工號前置、掃碼槍搜尋與切換 | 是 |
| `CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md` | Ctrl+C、worker 回收、全業務階段／Web／錄影停止計畫與驗證紀錄 | 兩階段本機實作有 PC 紀錄，實機待驗收 |
| `archive/` | 上游 VisionEdge、舊四／五步流程與歷史修正 | 否 |

遇到內容不一致時，以目前程式、`config/edge.ini`、
`config/apl_workflow.json` 及根目錄 `README_APL_ZH.md` 為準。
