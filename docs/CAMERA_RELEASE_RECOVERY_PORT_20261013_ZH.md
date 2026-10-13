# APL_fix 相機釋放與重開恢復移植紀錄

日期：2026-10-13。目標為 `APL_fix` 的 `feature/apl-fix-camera-release-test` 分支，
移植前基準提交為 `a0b4167`。參考來源是
`NK_APL/APL_cowork_2/nk_apl_v5` 的相機提交 `31584e5`；後續的
MediaMTX 提交 `960dd59` 不屬於本次移植。

## 移植範圍

| 檔案 | 相機相關改動 |
|---|---|
| `camera_recovery.py`（新增） | 判斷釋放失敗是否具備重開資格；核對本機與 `/run` guard、boot、session、worker 與 cam-server 身分。 |
| `camera_worker.py` | 收集停止證據與新幀數；受監督地等待服務替換、啟動 worker、驗證新幀，合格後封存舊 guard。 |
| `camera_lifecycle.py`、`edge_runtime.py` | 僅在 `reopen_eligible` 成立時允許明確的再次啟動，並向狀態 API 回報恢復資格與進度。 |
| `static/edge_dashboard.html` | 顯示 Recover Camera 與等待／失敗訊息；恢復完成前維持拍照和錄影禁用。 |
| `tests/test_camera_lifecycle.py`、`tests/test_camera_recovery.py`、`tests/test_camera_shutdown.py`、`tests/test_ui_static.py` | 覆蓋 guard、資格判斷、取消、worker 停止與 UI 狀態。 |

上述九個程式及測試檔移植後的 Git blob hash 均與來源提交 `31584e5` 相同。
原有的 callback drain、獨立錄影 EOS／NULL、主相機 EOS／NULL、ACK、worker
回收和失敗 guard 流程仍在基準版本中；本次新增的是**有證據限制的重開路徑**。
未改動 DMZ／SFIS 程式、站台流程、設備設定或 MediaMTX 檔案。

## 停止與重新開啟的實際語義

正常停止仍須完成相機及錄影資源收尾。若來源停在 READY→NULL 且失敗，
只有 worker 已確認並回收、未遭強制結束、主 EOS 與 callback 已完成、
其他相機元素到 NULL、錄影收尾沒有失敗、先前確實收到新幀等條件同時成立，
才標記 `reopen_eligible`。停止結果仍維持 `safe=false`、`RELEASE_FAILED`，
不得把恢復資格解讀成硬體已正確釋放。

使用者再次按 Start／Recover 時，程式核對同一開機的兩份 guard 與舊 worker
身分，等待 **不同 PID／啟動時間**的 cam-server 穩定 3 秒（上限 60 秒），
再建立新的 worker。worker 啟動上限為 70 秒，需收到至少兩個新的、時間遞增
的影像才視為啟動；確認 guard 未變動後封存舊 guard。程式不主動重啟
cam-server，也不會在未知或強制結束的釋放失敗後跳過 guard。

先前開發板的約 40 秒等待主要發生在 cam-server 重啟後的相機初始化；
這裡延長的是啟動期限，沒有加入固定 40 秒睡眠。實際耗時以板端觀察為準。
此方法讓已符合條件的失敗能再次取得畫面，**不是修復 BSP／HAL 的第二次
StopCamera 釋放缺陷**。

## 本機驗證與限制

使用 `amr_pol_ocr` Python 環境，在 `APL_fix` 執行：

```powershell
python -X utf8 -m unittest discover -s tests -p 'test_camera_*.py'
python -X utf8 -m unittest discover -s tests -p 'test_ui_static.py'
python -X utf8 -m unittest discover -s tests -q
```

相機專項 **46 項通過**；UI 專項 **16 項通過**。完整回歸執行
**337 項，4 項跳過、1 項失敗、1 項錯誤**：`test_matching` 的樣本張數
假設與目前 Golden Sample 數量不符；`test_video_temperature` 的
`after['dmz']['bypass']` 出現 `KeyError`。兩個失敗的測試及其業務程式碼
未在本次移植中修改，不把完整回歸記為通過。

PC 測試可驗證邏輯與 UI 分支，不能證明 Qualcomm HAL 已釋放，也不能證明
產線影像長時間穩定。尚須在指定開發板記錄至少多輪 Start→新幀持續更新→
Stop→再次 Start；逐輪保存 `safe`、`reopen_eligible`、worker/cam-server PID
及啟動時間、guard、首幀耗時、EOS／NULL、錄影收尾與 MediaMTX 影像結果。
任一輪缺少重開資格時應保持 guard 並停止重試，交由 BSP 問題追查。

本文件描述本機來源變更；尚未在 SmartCam 部署或驗收。
