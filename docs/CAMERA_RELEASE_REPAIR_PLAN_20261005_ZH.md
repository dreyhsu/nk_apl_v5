# APL 相機停止、硬體釋放與重新啟動修正計畫

建立日期：2026-10-05；更新日期：2026-10-06。

討論摘要與下一階段執行順序見：[相機第二次釋放失敗：問題統整與修正計畫](CAMERA_SECOND_RELEASE_ISSUE_AND_PLAN_20261006_ZH.md)。本文件保留歷史修正與詳細證據。
狀態：停止流程及診斷已提交至 `dfde633`；唯讀 SSH 核對開發機兩個核心檔案相同，第二次停止仍在相機 HAL close 失敗。硬體問題尚未解決，不以 PC 測試取代實機驗收。

閱讀順序：第 14 節為最新實機對照與修正分工；第 15 節整理之前的問題與防回歸要求。第 11 至 13 節保留各階段當時的實作／部署狀態，不代表後續現況。

## 1. 目的與適用版本

要解決的操作問題：手動執行程式後按 Ctrl+C，重新執行時無法再次取得相機，或留下 camera worker，使後續操作被 recovery guard 阻擋。

正常停止的完成條件不是「網頁關閉」或「主程序消失」，而是停止新增工作、完成必要錄影收尾、確認相機 pipeline 進入 NULL、收到對應 worker 的釋放結果，以及回收該 worker。正常路徑應能在同一次開機內再次啟動，不以 reboot 作為日常停止方式。

| 項目 | 本次範圍 |
|---|---|
| 修改目標 | `D:\work_project\smartcam\NK_APL\APL_cowork_2\nk_apl_v5` |
| 檢查分支 | `feature/sfis-bypass-switch` |
| 實作基準 | 原基準 `34d0833`，commit 時間 `2026-10-05 17:19:51 +08:00`；第一／第二階段後為 `3edc07f`，主 EOS／NULL 診斷修正為 `dfde633` |
| 目前模式 | `single_snapshot_cover_card`，不是 Go Live 自動收集流程 |
| 主要比較來源 | `firmware_update/smartcam_ai_inference_SDK_HPH_3`，只讀參考 |
| 操作方式 | 支援直接執行 `visionedge_server.py`，不要求改用 systemd service |
| 設備狀態 | 2026-10-06 唯讀 SSH 查詢開發機 `056325`；只核對當時部署內容及已有故障，沒有部署、啟停服務或 reboot；不是產線 `746708` 驗收 |

部署資料夾即使叫 `VisionEdge_APL_Go_live`，也不能據此判定裡面是 Go Live 版本。驗收必須記錄實際程式雜湊、設定、啟動命令及 PID。

保持現有員工工號、掃碼槍、SN、單張判定及 SFIS 契約。本分支設定使用 Step1／Step4，不套用舊文件的 Step2 流程。不修改 `dmz_client.py` 的傳送內容、SFIS 開關或產線站別設定。

## 2. 修正前已確認的程式缺口

以下保留修正前的本機來源證據，不足以斷言每次產線失敗都由同一原因造成。行號屬於修正前版本，現行程式以函式名稱定位；已實作內容見第 11 節。

| 位置 | 目前行為 | 需要修正的部分 |
|---|---|---|
| `camera_worker.py:175`，`WorkerCameraBackend.start()` | 啟動 worker 未建立獨立 session | 避免前景 Ctrl+C 同時打斷父子程序的清理順序；由父程序監督 worker |
| `camera_worker.py:286`，`WorkerCameraBackend.stop()` | 等待 18 秒逾時後寫 recovery marker 並拋例外 | 此分支沒有完整 TERM、KILL、wait/reap、socket 回收；不能留下仍存活 worker 就結束清理 |
| `camera_worker.py`，`worker_main()` | GLib signal handler 已有，但初始化發生在主迴圈運行前；清理使用 Python Timer | 處理初始化卡住、主迴圈尚未運行及 native call 阻塞；不能只依賴 worker 自己的 timer |
| `edge_runtime.py:1309`，`QtiGstCameraBackend.stop()` | overlay/sample/H.264 callback 等待失敗便拋例外；錄影停止例外被忽略 | 區分不安全的 pipeline 釋放與仍可完成的獨立清理；完整保留錄影及相機結果 |
| `edge_runtime.py:817`，`GstH264Mp4Recorder.stop()` | 有 EOS 等待與 NULL 動作，但狀態驗證及上層結果傳遞不足 | EOS、mux finalize、recorder NULL 都必須有明確結果，不能把失敗當完成 |
| `edge_runtime.py:1575`，`_make_backend()` | 父程序仍先呼叫 `QtiGstCameraBackend.available()` | QTI plugin 探測可能在 worker 建立前阻塞；需移入受監督程序 |
| `edge_runtime.py:1643`，`EdgeRuntime.stop()` | runtime thread 結束就回傳 success 並設為 STOPPED | thread 結束不等於 backend 釋放成功；不可覆蓋 `_run()` 留下的清理失敗 |
| `visionedge_server.py:651`，`_shutdown()` | 忽略 `EDGE.stop()` 回傳與例外；同時由 finally、atexit 呼叫 | 統一且可重複呼叫的關閉入口，避免漏報、重複清理及並行停止 |

前一輪本機故障注入已重現三種來源層行為：callback 逾時後未執行 NULL、worker wait 逾時未進入完整終止回收、runtime 結束後仍可能覆寫成停止成功。這些不是 Qualcomm 實機重現結果。

其中第一項不能直接理解為「把 NULL 放進 finally 就能修好」。callback 若仍持有影像或 cairo 資源，強制釋放 pipeline 可能造成存取已釋放記憶體。問題是目前缺少完整的失敗收尾與父程序監督契約，而不是所有路徑都應無條件 NULL。

## 3. HPH 參考方式

參考 `smartcam_webserver.py` 的獨立程序、停止期間禁止重新啟動、逾時升級終止及 wait/reap；參考 `smartcam_inference.py` 的 GLib signal handler、callback 排空、EOS、NULL 與退出保護。

不整份覆蓋 APL，原因如下：

- HPH 和 APL 的判定、錄影、資料保存及 SFIS 契約不同。
- HPH 有 callback 排空失敗時不再正常釋放 pipeline、改採程序退出的保護，必須保留這個安全意圖。
- HPH 仍可見 `get_state(Gst.CLOCK_TIME_NONE)` 等無限等待，不直接搬入新停止流程。
- 保留這個平台避免 `Gst.deinit()` 的既有約束，不能套一般 GStreamer 範例補上。
- HPH 的固定 settle delay 是參考值，不是相機已經釋放的證據。

## 4. 修改檔案與責任

以下列出計畫範圍；本次實際修改及未修改的檔案見第 11 節。

| 檔案 | 預計修改 |
|---|---|
| `camera_worker.py` | worker session、啟動取消、分階段停止、父程序 deadline、終止回收、結構化 ACK、recovery marker |
| `edge_runtime.py` | QTI 探測隔離、callback／recorder／pipeline 清理順序、保留釋放失敗、啟停互斥及部分啟動失敗收尾 |
| `visionedge_server.py` | 提早建立關閉處理、單次 shutdown 協調、停止新請求、回報關閉結果與失敗 exit code |
| `overlay_renderer.py` | 明確分離禁止新 callback、等待既有 callback、解除連接；僅在現有介面不足時調整 |
| `tests/test_camera_recovery.py` | 保留原測試作回歸；新 guard／ACK 案例放在 `test_camera_shutdown.py` |
| `tests/test_camera_shutdown.py`（新增） | worker 停止升級、啟動途中取消、重複 stop、IPC 中斷、程序回收測試 |
| `tests/test_runtime_shutdown.py`（新增） | callback、EOS、NULL、runtime 結果及 server shutdown 故障注入 |
| `docs/HANDOVER_ZH.md`、`docs/README.md`、本文件 | 實作後補充停止方式、版本、測試結果、限制及部署驗收紀錄 |

若新增診斷腳本，限定讀取狀態與整理日誌；不得隱含 kill、重啟平台服務、刪除 recovery marker 或送出 SFIS。硬體團隊使用的測試工具須獨立，不 import HPH 私有應用程式。

## 5. 正常停止流程

```text
Ctrl+C / SIGTERM / Stop Camera
    ↓
進入 STOPPING，禁止新的 Start / Capture / Record
    ↓
取消尚未完成的啟動與重連工作
    ↓
父程序送出一次 stop；worker 停止新增 callback 工作
    ↓
等待已進入的 overlay / sample / H.264 callback 排空
    ↓
停止錄影新增資料 → recorder EOS → mux finalize → recorder NULL
    ↓
停止常駐 bus reader → 主 pipeline EOS，有界等待 EOS / ERROR
    ↓
相機 pipeline NULL，驗證實際狀態
    ↓
停止 bus / sender 等背景工作，回報釋放結果
    ↓
父程序 wait/reap worker、關閉 IPC、整理資源
    ↓
確認安全釋放 → STOPPED；完整程式退出或允許下次 Start
```

### 5.1 同一個停止交易

- 每次 camera session 有唯一識別；啟動、停止命令與 ACK 必須對應同一 session。
- `_shutdown()`、網頁 Stop、Ctrl+C、atexit 共用同一筆停止結果。重複呼叫只能等待或讀取結果，不再重跑清理。
- 第二次 Ctrl+C 不得直接跳過收尾或誤殺父程序所在程序群組。
- Start 與 Stop 互斥；STOPPING 期間的 Start 直接回報暫不可用，不排入稍後自動啟動。
- scanner 等其他元件停止失敗，仍需嘗試相機安全收尾；最後彙整錯誤，不吞掉第一個失敗。
- 尚有資料生產者時不得提前關閉它正在使用的資料庫／輸入物件。

### 5.2 Callback 與錄影

- 先設停止旗標，再等待已進入 callback 退出；停止旗標和鎖的使用需避免新的 callback 穿過檢查。
- callback 不得持有鎖等待 shutdown，而 shutdown 又等待同一 callback，造成互等。
- recorder 完成處理已接收的資料後才 EOS；EOS 成功不等於 NULL 成功，兩者分開記錄。
- 沒在錄影屬於正常的 `not applicable`，不是停止失敗。
- 錄影收尾失敗要標示未完成，不能登記為正常保存；仍須嘗試安全釋放相機。
- callback 排空失敗時，不在仍被使用的物件上強制執行正常 teardown；回報階段與未釋放項目，轉入受監督退出。
- bus 保留到所需 EOS／ERROR／狀態結果處理完成；同一 bus 不由多個等待者互搶訊息。

### 5.3 Pipeline 與狀態

- 檢查 `set_state(NULL)` 結果，再用有上限的 `get_state()` 確認實際 state；只呼叫 set_state 不算完成。
- 啟動失敗、部分元件建立完成、正常停止都使用同一套階段清理，依已建立資源判斷需要哪些步驟。
- 清理函式收集各階段錯誤，不讓一個可恢復錯誤跳過其他安全且必要的清理。
- 有界 `get_state()` 不能保證 native `set_state()` 本身不會卡住，因此仍需父程序外部監督。

## 6. 卡住時的停止與恢復規則

### 6.1 父程序監督

Linux worker 使用獨立 session，例如 `start_new_session=True`，由父程序管理。只處理本次建立、身分確認過的 worker／程序群組，不以名稱廣泛 kill 所有 Python、cam-server 或 MediaMTX。

初始時間預算如下，實作後以板上量測調整，不是固定每次等待這麼久：

| 階段 | 初始預算 | 到期動作 |
|---|---|---|
| worker 正常清理 | 總共最多 20 秒 | 回報未完成階段；不讓每個子步驟重新獲得完整預算 |
| 父程序 graceful 等待 | 約 22 秒，包含 ACK 傳送餘裕 | 轉入 SIGTERM |
| SIGTERM 等待 | 最多 3 秒 | 仍未退出才 SIGKILL |
| SIGKILL 後回收 | 最多 2 秒 | 未退出則明確回報殘留 PID／狀態，不無限等待 |

所有等待共用 monotonic deadline，避免 NTP 校時改變等待長度。正常情況各步驟完成即返回。IPC 送出、鎖等待、thread join 也需納入預算，不能只限制 `process.wait()`。

SIGKILL 不是安全釋放證明；程序處於 kernel 不可中斷等待時，也不保證立即消失。計畫保證有界等待及明確回報，不宣稱使用 kill 一定能恢復驅動。

### 6.2 啟動途中按 Ctrl+C

- 父程序在相機初始化前就必須能接收停止要求，保存 pending worker 參照。
- 明確選用 QTI 時，plugin 探測、Gst 初始化和相機取得都放在受監督 worker。
- `auto` 模式也不能留下父程序內無界 QTI 探測；PC/OpenCV 選擇需維持既有相容性。
- 有 GLib 主迴圈時用 `GLib.unix_signal_add()`；主迴圈尚未啟動時仍靠父程序取消與超時保護。
- 停止後遲到的 ready／frame 不能使 runtime 又回到 RUNNING；使用 session／啟動世代辨識。

### 6.3 停止結果與 recovery guard

停止結果至少區分：`callbacks_drained`、`recorder_finalized`、`recorder_null`、`camera_null`、`worker_reaped`、`forced_exit`、`errors`。不存在的資源使用明確的不適用值。

| 結果 | 對外狀態與後續動作 |
|---|---|
| 正常 ACK、必要 NULL 已確認、worker 已回收 | STOPPED，可再次啟動 |
| 相機已安全釋放，但錄影 finalize 失敗 | 顯示錄影保存失敗；相機釋放與保存結果分開，不捏造錄影成功 |
| ACK 缺失、NULL 未確認或採用強制退出 | RELEASE_FAILED，保存同一次開機的 recovery guard，禁止直接再次取得相機 |
| worker 仍存活 | RELEASE_FAILED，保留 PID／階段，不能回報停止成功 |

ACK 缺失不一定代表硬體真的未釋放，但目前無法證明安全，因此採保守處理。不得只看 return code 為 0 就推論釋放完成。

recovery marker 使用原子替換，包含 boot ID、session、PID、失敗階段與原因；寫檔失敗本身也要上報，同一程序內仍保持禁止重新取得的狀態。沿用有效 boot ID 確認重開機後才封存舊 marker 的原則，不把刪 marker 當成修復。

若加入跨程序 camera lock，應依 camera ID 使用固定位置，不依部署資料夾名稱；鎖由實際持有相機的 worker 保持到退出。它只約束遵守同一鎖的程式，不能宣稱能阻擋所有第三方相機應用。

## 7. 日誌與診斷資訊

每個階段記錄開始、完成／失敗、耗時、camera ID、session、worker PID、停止來源與程序 exit code。階段至少包含：

`STOP_REQUESTED` → `CALLBACK_DRAIN` → `RECORDER_FINALIZE` → `PIPELINE_EOS` → `PIPELINE_NULL` → `PIPELINE_NULL_RESULT` → `WORKER_ACK` → `WORKER_REAP` → `STOP_COMPLETE`／`RELEASE_FAILED`。不存在的資源／不需要的 EOS 階段明示不適用，不假造成功。

保留原始錯誤，不再用單一「Camera may already be in use」覆蓋 callback timeout、NULL timeout、IPC lost 等不同原因。正常未錄影不寫成錯誤。記錄來源版本但不輸出密碼、憑證私鑰或 SFIS 敏感資料。

網頁與 API 應顯示 STOPPING、STOPPED 或釋放失敗的真實結果；若增加狀態欄位，需一併檢查現有 UI 消費端，必要時只調整狀態呈現，不改判定或 SFIS 流程。

## 8. 測試與驗收

### 8.1 PC 自動測試

先完成下列 mock／subprocess 測試。`ml_env` 可跑 worker 測試，但缺 Flask；本次完整回歸使用既有 `amr_pol_ocr` 環境，未安裝或升級套件。Windows 測試不能替代 Linux process-group 或 QTI 硬體行為。

- 正常停止：callback、EOS、NULL、ACK、reap 順序正確。
- 重複 stop、Ctrl+C 與 atexit 重入：只清理一次。
- STARTING 收到 stop、遲到 ready：不再進入 RUNNING。
- overlay/sample/H.264 callback 卡住：不在不安全狀態強行釋放物件。
- EOS timeout、錄影錯誤、NULL failure：分別保留錯誤，不能回報整體正常成功。
- IPC 斷線、ACK 遺失、worker 提前退出：同一 session 的結果判定正確。
- worker 忽略停止：依序升級並回收；所有等待有上限。
- marker 損壞、寫入失敗、相同／不同 boot ID：guard 行為正確。
- runtime thread 結束但 backend 釋放失敗：不覆寫成 STOPPED／success。
- Stop 與 Start 並行、跨資料夾啟動：不產生第二個合作式 camera owner。
- 既有工號、掃碼槍、單張判定及 Step1／Step4 測試回歸，SFIS 使用 mock，不呼叫真實產線 API。

### 8.2 SmartCam 開發機驗收

先記錄 hostname、MAC、boot ID、韌體、部署雜湊、有效設定和命令，確認測的是本次版本。先開發機，通過後才安排產線維護窗口；本文件不執行部署。

| 情境 | 建議次數 | 通過條件 |
|---|---:|---|
| 預覽正常後 Ctrl+C，再手動啟動 | 30 輪 | 同一次 boot 全部正常取得新畫面，沒有殘留自有 worker、沒有不必要 recovery marker |
| 啟動途中 Ctrl+C | 10 輪 | 啟動可取消，停止不無界卡住，不晚到自動開相機 |
| 網頁 Stop／Start | 20 輪 | 狀態與真實程序一致；無重複 worker |
| 有錄影時 Ctrl+C（若此版本啟用錄影） | 10 輪 | MP4 可解析／播放；失敗必須明確標示，不產生假的完成紀錄 |
| 模擬 worker／IPC 故障 | 依各故障案例 | 有界回報與安全鎖生效，不自動搶回相機 |
| 重複／快速 Ctrl+C | 5 輪 | 不重入 teardown，不跳過正常收尾 |

每輪驗證新 frame 的時間／序號，不能只看網頁仍有舊畫面。檢查 QMMF、Venus、GStreamer 與應用日誌；比較 PID、FD、記憶體趨勢，不以單一 warning 或一次成功判定驗收通過。

正常停止後需能再次取得相機，這才是釋放正確的實機證據。強制終止案例的通過條件是「誠實回報、安全阻擋」，不是必須不 reboot 就恢復。

## 9. 實作順序與交付

1. 補故障注入測試，固定目前錯誤的重現方式；保留來源基準。
2. 修正 worker 監督、啟動取消與 IPC 清理，建立統一停止結果。
3. 修正 callback、錄影及 pipeline 分階段收尾，處理部分啟動失敗。
4. 修正 runtime／server 的啟停互斥、shutdown 重入及失敗狀態傳遞。
5. 加入階段日誌，跑 PC 回歸測試，檢查 SFIS 契約沒有改動。
6. 開發機執行重複停止／啟動與故障驗收，記錄未通過項目；未驗證前不標示硬體問題已解決。
7. 更新交接文件、SmartCam 架構參考與索引；依工作區 `docs/01_system/SMARTCAM_SKILL_MAINTENANCE_ZH.md` 同步 skill。區分本機實作、PC 測試及實機驗收。

保留舊程式版本作回退，勿覆蓋設備的憑證、資料庫、掃碼槍記憶檔與錄影。回退前也必須停止新程序並確認相機釋放；若已進入 recovery guard，回退程式本身不會解除底層硬體狀態。

## 10. 本次交付狀態

- [x] 核對目標分支、設定及相機停止相關來源。
- [x] 比較 HPH 的程序監督與 callback 清理模式。
- [x] 建立此修改計畫與文件索引入口。
- [x] 第一階段 worker／runtime／程序 shutdown 修正及網頁釋放失敗狀態。
- [x] 新增並通過 PC 故障注入與回歸測試（249 項執行，245 通過、4 跳過）。
- [x] 更新交接手冊、架構與 lineage reference、總索引。
- [x] 第 12 節本機實作：Web 停止入口、站台鎖隔離、業務階段停止與錄影資源回收；板端矩陣待驗收。
- [ ] SmartCam 開發機驗收。
- [ ] 產線部署及驗收。

本次修改本機 Python、狀態顯示與文件；沒有修改設備、SFIS 對接或業務設定。Ctrl+C 後能否穩定重新取得 QTI 相機，仍須實機驗收。

## 11. 2026-10-05 實作與驗證紀錄

### 11.1 已實作

| 檔案 | 已實作內容 |
|---|---|
| `camera_worker.py` | Linux 獨立 process session；session-tagged IPC；STARTING 可取消，包括認證前取消；stop 結果快取；grace／TERM／KILL／reap；socket 回收；結構化 ACK；原子 recovery marker |
| `edge_runtime.py` | 顯式 QTI 不在父程序探測；auto 用受監督短程序探測；callback 雙重停止檢查；先 drain 再 EOS／NULL；驗證實際 NULL；錄影／相機結果分開；runtime 不覆寫清理失敗 |
| `overlay_renderer.py` | `prevent_new_draws()` 先封鎖新 draw，再等待現有 draw 退出 |
| `visionedge_server.py` | 相機啟動前註冊 signal；shutdown 一次執行；拒絕新寫入；scanner 失敗仍停止相機；runtime 未退出不關資料庫；HTTP 建立途中停止；失敗退出碼 70 |
| `static/edge_dashboard.html` | `RELEASE_FAILED` 紅色狀態與恢復提示；阻擋相機 Start／快照／錄影；停止及設定重啟 HTTP 等待 45 秒，避免原 15 秒先逾時 |
| `tests/test_camera_shutdown.py`、`tests/test_runtime_shutdown.py` | stop 升級、未退出 PID、ACK／session、disk-write failure、跨目錄 guard、callback／EOS／NULL 故障、啟動取消、重入、HTTP shutdown；另有真實子程序 IPC 三輪重新取得與認證前取消 |
| `tests/test_ui_static.py` | 修正基準已有的過期 MCIO 按鈕斷言；新增狀態檢查，透過 Node 執行實際 `renderStatus`，不開瀏覽器 |
| `tests/test_mcio_scan.py` | 修正 Windows fixture 關閉順序：station／SQLite 先關閉，再刪測試暫存目錄；不改掃碼流程 |
| 本文件、`HANDOVER_ZH.md`、`docs/README.md` | 更新實作狀態、停止操作、部署邊界及未驗收項目 |

`apl_station.py`、`dmz_client.py`、`scanner_selection.py`、`static/apl_ui.js`、兩個業務設定檔均未修改。`scripts/image_match_test.py` 的下午新增內容保留；HPH_3 僅作比較來源。

### 11.2 停止監督及安全判定

- QTI worker 內清理 watchdog 為 20 秒，強制退出前嘗試寫入 recovery guard；父程序 graceful wait 22 秒，TERM 3 秒，KILL／wait 2 秒，另有 ACK 排空及 IPC receiver join。runtime 使用 32 秒的停止協調 deadline。這些是故障上限，不是每次都要等滿。
- `set_state(NULL)` 本身可能卡在 native code；僅 bounded `get_state()` 不足以保護，仍需父程序 deadline。auto discovery probe 在取消後 kill／wait；若仍未退出也保留失敗及 recovery guard。Windows 未驗證 Linux `killpg` 或驅動行為。
- callback 排空失敗時不釋放仍被使用的 native 物件，改回報不安全並讓 worker 受監督退出。不可把 kill 成功當作硬體釋放成功。
- `safe=true` 表示 drain／NULL／ACK／reap 已符合安全條件；`success=false` 仍可能表示錄影 EOS 或其他收尾失敗。空錄影沒有 encoded frame 時不再回報保存成功。
- 同一 boot 的不確定釋放寫入原 `runtime_data/edge/camera_recovery_required.json`，Linux 另寫 `/run/visionedge-camera-0.recovery.json`；依實際 camera ID 命名。`/run/visionedge-camera-0.lock` 為跨資料夾 flock，持有到 worker 退出。
- flock 只約束採用此協定的程式；不能阻擋第三方 app。marker 寫入失敗會回報錯誤並保持本程序安全鎖，不能保證不可寫檔案系統下跨程序持久化成功。依現行部署以 root 執行。
- 本地 marker 只在兩個有效 boot ID 不同時封存；`/run` 在 reboot 後重建。不要刪 marker、反覆 kill `cam-server` 或使用全機 `pkill python3` 作正常釋放。

### 11.3 本機驗證

環境：Windows、既有 `amr_pol_ocr` Python、Node.js；全套 `tests/test_*.py` 共 249 項，245 通過、4 項缺參考影片而跳過，0 failure、0 error。SFIS 僅 mock，沒有真實過帳；未開啟瀏覽器或相機。

```powershell
conda run -n amr_pol_ocr python -X utf8 -B -m unittest discover -s tests -p "test_*.py" -v
git diff --check
```

Windows 匯入顯示 `/tmp/infer_runner.log` 不可寫警告，未視為本機相機驗收，也未修改裝置 log 路徑。測試中的 SFIS probe 拒絕 bypass 輸出是預期安全測試，不是對正式 API 的呼叫。

### 11.4 更新與待驗收

更新時將四個核心 Python（worker／runtime／overlay／server）與 `static/edge_dashboard.html` 同批部署；IPC 協定已變更，不可混用舊 worker。保留裝置自己的 `config/`、憑證、資料庫、掃碼槍記憶及錄影。沒有新套件需求、沒有必須安裝的新 service。

本次未改 `rotation=180`、`auto_start=true` 或 WHEP port，實際方向與埠仍依選定設備設定。不要把本計畫當成部署設定已自動更正。

剩餘驗收：同 boot Ctrl+C／啟動 30 輪、STARTING 途中取消 10 輪、網頁 Stop／Start 20 輪、實際 H.264 MP4 EOS／播放及 Linux process-group 故障回收。需記錄新 frame、PID、boot ID、ACK 和下一次成功取得；目前未執行。

## 12. 第二階段：所有業務階段都能停止與回收

補充日期：2026-10-06。第 12.1 至 12.9 節保留規劃與驗收標準，實際交付見第 12.10 節。第 11 節的 249 項測試是前一階段紀錄，不涵蓋本節所有案例；未打勾的板端項目仍未驗收。

### 12.1 需求與停止操作的定義

目標：無論有沒有工號、產品 SN 或 MCIO SN 是否正確、SFIS 是否拒絕或逾時，使用者都必須能要求停止相機；不得先要求成功登入、掃碼、終止工單或成功寫入資料庫，才能回收硬體。

「可停止」不等於「任意時候必定正常釋放」。驅動卡死時，程式應在期限內嘗試清理並誠實回報不安全；不能用 kill 成功或 UI 顯示停止冒充已取得硬體釋放證據。

| 操作 | 必須收尾的資源 | 完成後的相機狀態 |
|---|---|---|
| Ctrl+C／SIGTERM／程序退出 | 錄影、camera pipeline、自有 worker、IPC；安全處理仍使用中的站台資源 | 安全 STOPPED 後退出；不安全則回報失敗與 recovery guard |
| UI Stop Camera | 先完成錄影收尾，再回收 camera pipeline／worker／IPC；Web server 保留 | 安全 STOPPED，可由使用者再次啟動；不能自動重開 |
| UI Stop Recording | 結束該錄影 session，EOS／mux finalize／recorder NULL，釋放錄影檔案與資源 | 正常時保留相機預覽；不宣稱整個相機已釋放 |
| 錄影期間 UI Stop Camera | 同一筆停止交易先封檔，再停止相機，不要求使用者先成功按 Stop Recording | 同 Stop Camera |
| UI 取消啟動 | 取消 discovery／pending worker／部分建立的 pipeline | 未取得相機則記錄不適用；已取得則完成釋放或進入 RELEASE_FAILED |

本計畫保留 Stop Recording 的既有「停止錄影、繼續預覽」語義。若使用者要在按停止錄影後連相機也關閉，介面需明確命名為「停止錄影並關閉相機」，導向完整停止交易；不得默默改變既有按鈕或把 recorder NULL 等同 camera NULL。實作前確認此操作選擇，核心安全修正不依賴此選擇。

工號或 SN 格式錯誤本身不自動關閉相機，以免一般輸入錯誤造成反覆取放硬體；但不得阻擋任何停止操作。只有明確停止、程序退出或致命相機／資源故障才進入相機停止流程。

### 12.2 來源證據與缺口分類

行號以本次工作樹為準，後續以函式名稱定位。

| 分類 | 來源 | 證據與結論 |
|---|---|---|
| 已重現的入口缺陷 | `visionedge_server.py:454 edge_stop()`；`apl_station.py:187 _save()` | 先 `station().camera_fault()` 再 `EDGE.stop()`；站台記錄拋錯會跳過停止。前次 Flask 故障注入得到 HTTP 500、EDGE.stop 呼叫 0 次 |
| 已確認的鎖依賴 | `station_command()`、`scanner_scan()`、`edge_status()` | Stop 持有全域站台鎖等待硬體，status 也透過 station() 等該鎖；站台操作可延後 Stop，Stop 又可阻塞狀態查詢。實際等待長度仍需並行測試 |
| 已確認的 UI 限制 | `static/edge_dashboard.html toggleCamera()/renderStatus()` | STARTING 或錄影中停用相機按鈕；底層即使支援取消，網頁也無法使用 |
| 已確認的設定順序 | `edge_runtime.py update_config()` | 先改寫／儲存 cfg 再 restart；停止失敗時新設定與仍在執行的舊 pipeline 可能不一致 |
| 已確認的錄影語義 | `visionedge_server.py edge_record_stop()`、`EdgeRuntime.stop_recording()` | 只停止 recorder，不停止 camera；這不是相機洩漏的單獨證據 |
| 需補測的錄影失敗路徑 | `QtiGstCameraBackend.stop_recording()` | 清除 `_recorder` 後才收尾；需驗證 NULL 未成功時，未釋放物件的所有權、保留參照與後續全相機停止，不能僅靠 recording=false 判定安全 |
| 需補測的程序退出順序 | `visionedge_server.py _shutdown()` | scanner.stop() 在 EDGE.stop() 前；已有 exception 保護，但仍要注入阻塞，證明掃碼器卡住不能延誤相機停止 |

HPH_3 只參考 process ownership、GLib signal handler、callback drain、EOS／NULL 與受監督退出；不搬入其無界 `get_state(Gst.CLOCK_TIME_NONE)`，不改其來源。以上缺口並非全部已在產線重現。

### 12.3 統一停止協調與鎖規則

1. 建立 per-camera lifecycle coordinator。收到停止先用短時間生命週期鎖登記 `session_id`、`operation_id`、停止原因、monotonic deadline，設停止旗標與 generation，再排程清理。不先呼叫 station()、SQLite、SFIS 或 scanner.stop()。
2. 生命週期狀態與業務狀態分開。工號登入、SN 校驗及業務狀態不能成為 Stop 的前置條件；Stop 保留原有授權檢查，但不要求有有效工單。
3. 同一 session 的重複 Stop、Ctrl+C、SIGTERM、Restart 前置停止，共用一筆清理工作與結果。不得重送 EOS、兩次銷毀 pipeline 或讓舊停止結果污染新 session。
4. 不持有全域站台鎖或 runtime 狀態鎖等待 IPC、EOS、NULL、thread join、worker.wait 或網路。鎖僅保護短時間的狀態發布；各類鎖等待納入同一 deadline。避免「站台等硬體、硬體又回呼站台」互等。
5. 硬體停止與站台中斷紀錄分成兩項工作；站台記錄 best-effort 且有界。記錄失敗保留業務恢復錯誤，但仍執行完整硬體停止。不能只在持有站台鎖的原函式外面包 try/finally。
6. camera status 必須不依賴取得站台鎖。提供獨立 lifecycle status 或已發布的不可變快照；站台暫時忙碌時回報 station 狀態過期／不可用，不以舊資料假裝即時成功。UI 能持續顯示停止階段。
7. Web 請求斷線或 45 秒等待到期，不取消背景清理、不自動重試 Start。使用 operation_id 查詢同一筆結果，不能出現 HTTP 已超時而無人監督 worker。
8. Start／Capture／Record Start 在 STOPPING 或 RELEASE_FAILED 時拒絕；來源或設定重啟也共用入口。先驗證新設定，暫存 pending config，確認舊 session 已安全釋放才套用；區分 requested／effective config，新啟動失敗不偷偷回到舊硬體流程。
9. 全程序退出先發出相機停止，再以有界方式停掃碼器及其他元件；不能只捕捉 exception 而允許 scanner join 無限阻塞。第二次 Ctrl+C 只查閱／催辦既有停止，不跳過清理。

回覆欄位採相容性擴充：保留現有 success/error，新增 operation/session、phase、elapsed、camera_release、recording_finalize、station_update 等分項。未錄影、尚未取得 camera 用 not_applicable，不用假 true。camera 安全但業務日誌失敗時，各自顯示結果，不顯示整體一切正常。

### 12.4 工號、SN、判定與 SFIS 各階段

| 階段／錯誤 | 相機停止要求 | 業務資料處理 |
|---|---|---|
| 未簽入、工號空白／格式錯、Step1 NO EMP | 一律可停止，不先要求重新登入 | 保持原工號卡控；不新增或偷偷接受 SN |
| WAITING_SN、錯誤產品前綴／長度、重複 SN | 一律可停止 | 保持原拒絕原因，不靠清空資料庫解除 |
| WAITING_MCIO_SN、MCIO 格式錯／缺漏 | 一律可停止 | 已存在的 cycle 按現有中斷／終止契約記錄 |
| SN_CONFIRM／Step1 請求進行中、FAIL、timeout | 不等待 SFIS 回覆才釋放相機 | 回覆與 cycle/request token 核對；遲到接受不能重開相機或啟動拍照 |
| WAITING_CAPTURE、NOT_MATCHED | 一律可停止 | 保留原 SN／已保存證據，不自動宣告完成 |
| JUDGING／證據寫入中 | 立即禁止新拍照，執行有界停止 | 已開始的工作持有自己的安全 frame copy；遲到結果不得復活已取消的 session。未結束的 writer 不提前 close DB |
| PASS_CONFIRM | 一律可停止 | 視覺 OK 證據保留；相機停止不等於 Step4 成功或自動終止工單 |
| Step4 傳送／等待回覆 | 相機可以停止，不自動取消或補送 SFIS | 已發出的請求按現有 outbox／request ID／確認規則收尾；未知結果不可改成未送出或失敗後盲目重送 |
| RECOVERY_REQUIRED／SQLite busy、disk full、readonly | 硬體清理仍執行 | 記錄不可寫錯誤與 stderr 診斷；不得以成功写 DB 作為回收條件 |
| 完成、終止、測試／bypass | 同樣適用 Stop | 保持測試隔離及不送正式 API 的約束 |

只讓 camera generation 失效，不粗暴清除全部 SFIS request token。相機來源生命週期與過帳請求生命週期要分開，避免為了停止相機遺失已成功送出的交易。需要調整 `apl_station.py` 時只調整中斷／遲到結果協作，不更改 Step1／Step4 schema、工號／SN 格式規則或判定門檻。

### 12.5 錄影資源的特殊處理

- 每次錄影有獨立 session/result；Stop Recording 與 Stop Camera 同時到達時只能有一個 recorder finalize owner，其餘等待同一結果。
- 停止接收新 buffer，排空已接收佇列，送一次 EOS，等待 mux 完成，recorder NULL 並確認狀態，再關閉檔案參照。保留原 timestamp／IDR 與共用 encoder 設計，不讓整條相機 pipeline 接收到只供錄影的 EOS。
- 正常停止錄影後相機仍輸出新 frame／WebRTC；camera owner PID 不應因正常封檔而更換。不可報 camera_released=true。
- recorder finalize 失敗但 recorder NULL 已確認：回報保存失敗，不能將損壞或零 frame MP4 列為完成；相機可持續預覽。再次錄影是否允許由 recorder 安全結果決定，不只看 active=false。
- recorder NULL／callback 無法確認：保留待清理物件與失敗結果，禁止新的錄影，升級為完整相機停止；沿用受監督 worker deadline 與 guard，不在未知狀態繼續重用 native recorder。
- record_stop IPC 等待超時不代表 worker 完成停止。父程序要取得最終結果或升級全相機停止，不能只回 HTTP error 後放任 worker 卡在 mux。
- UI 錄影狀態區分 RECORDING／FINALIZING／IDLE／FAILED；只有封檔成功才顯示保存完成。Stop Camera 不因 recording=true 被禁用。

### 12.6 時間預算、錯誤與保護

沿用第一階段 worker 20 秒 watchdog、父程序 graceful 22 秒、TERM 3 秒、KILL/reap 2 秒、runtime 32 秒協調預算；這些不是可逐層重新起算的額外等待。第二階段需證明在進入站台鎖以前即已啟動期限與監督。錄影正常封檔目標沿用 8 秒、IPC 9 秒，超時按第 12.5 節處理。

PC 並行測試可用可控 Event／較短注入 deadline，避免測試本身耗時或偶發失敗。UI status 在硬體 Stop 卡住時仍應於 1 秒內回覆，作開發驗收目標而非目前已實現的性能保證。

stage error、cleanup error、業務保存 error 分開記錄。日誌含 boot/session/operation/PID、入口、階段、耗時、ACK／exit 結果；不記工號／完整 SN／私鑰／SFIS 憑證。不可寫 DB 時仍嘗試 stderr 與 recovery guard；guard 也無法寫入時保留本程序禁啟動狀態並明確告警，不能承諾跨程序保護。

### 12.7 預計實作檔案與順序

| 順序 | 檔案 | 修改內容 |
|---|---|---|
| P0-1 | `tests/test_runtime_shutdown.py`、`tests/test_api.py` | 先補 DB 拋錯仍停止、站台鎖占用、停止中 status 可用、未簽入／錯誤 SN 等失敗測試 |
| P0-2 | `visionedge_server.py`、`edge_runtime.py` | lifecycle coordinator、停止入口與站台／scanner 解耦、背景結果、deadline、Stop／Start／Restart 競態 |
| P0-3 | `apl_station.py`、必要的站台測試 | 業務中斷協作、generation、遲到判定與 SFIS 回覆處理；對外 SFIS 契約不動 |
| P1-1 | `edge_runtime.py`、`camera_worker.py`、`tests/test_camera_shutdown.py` | recorder failure ownership、Stop Recording 與全 Stop 競態、IPC 超時監督 |
| P1-2 | `static/edge_dashboard.html`、必要時 `static/apl_ui.js`、`tests/test_ui_static.py` | 啟動可取消、錄影中可 Stop Camera、獨立相機／錄影／業務錯誤顯示、不被工號卡控禁用停止 |
| P1-3 | `edge_runtime.py`、設定相關測試 | pending/effective config，安全停止後套用與失敗狀態 |
| P2 | 本文件、`HANDOVER_ZH.md`、文件索引 | 填入實際改動、測試輸出、部署指紋與板端逐輪驗收，不以本計畫代替測試紀錄 |

不修改 `dmz_client.py` 的 API 契約、不動正式設定、Golden Samples、既有證據、HPH_3 或另一份 APL_cowork。若改動共用架構，實作當次依 skill maintenance 更新 architecture／APL reference 與索引；本次僅補計畫，不把待實作行為寫成 skill 的已完成事實。

### 12.8 驗收矩陣

PC 先做參數化測試：第 12.4 節每種業務狀態，分別注入 Ctrl+C 關閉請求、UI Stop Camera；有錄影者再加入 Stop Recording。確認控制流與結果，而不是只斷言文字或按鈕存在。

必測項目：

- [ ] camera_fault 拋 SQLite OperationalError：相機 Stop 確實執行且只一次；業務錯誤仍被保留。
- [ ] 站台鎖由 Step1／Step4／儲存工作持有：Stop 開始與 deadline 不受該鎖阻擋；status 可查。
- [ ] scanner.stop() 拋錯或卡住：相機仍被獨立停止；全程序關閉不無界等待。
- [ ] 未簽入、無效工號、NO EMP、錯誤產品／MCIO SN：UI 和直接 API 皆能停止；仍禁止未授權業務操作。
- [ ] STARTING 取消、Stop/Start 同時、Stop/Restart 同時、Ctrl+C 和 UI Stop 同時：只有一個 owner／停止交易。
- [ ] 判定與證據寫入中 Stop：不使用已釋放 native frame、不提前關閉 DB，不讓遲到結果恢復相機。
- [ ] SFIS 已送出後 Stop：不新增 API 呼叫、不改 request body／ID／既有確認語義；晚到回覆不啟動硬體。
- [ ] Stop Recording 與全相機 Stop 競態：只送一次 EOS、回傳同一 finalize 結果；無雙重釋放。
- [ ] EOS timeout、recorder NULL failure、record_stop IPC timeout：各自報錯，未釋放 recorder 不遺失參照，必要時升級全 Stop。
- [ ] 重複停止已停止的 camera／recorder：不回頭操作舊 session，不重新開機或重送過帳。
- [ ] 設定重啟失敗：pending/effective 分明；新設定不假裝已套用，不自動重新取相機。
- [ ] HTTP timeout／瀏覽器離線：後端清理繼續，重連能查同一 operation 結果。
- [ ] UI 真實函式／DOM 互動替代測試：未登入仍能 Stop、STARTING 能取消、FINALIZING 可見；保留使用者先前不做瀏覽器測試的限制。
- [ ] 既有 scanner／operator／SFIS／snapshot 回歸；使用 mock，不對正式 SFIS 過帳。

板端驗收在明確授權後於開發機進行，不能由 Windows mock 代替：

| 測試 | 次數／範圍 | 通過條件 |
|---|---|---|
| Ctrl+C 後同 boot 重新啟動 | 30 輪，涵蓋等待工號、錯誤工號／SN、判定、待確認等階段 | 自有 worker 回收、必要 NULL／ACK 正確，下一輪確實取得新 frame |
| UI Stop Camera／Start | 20 輪，至少包含未登入與錯誤 SN | 停止不被業務狀態阻擋；停止中 status 可查；無第二個相機 owner |
| STARTING 取消 | 10 輪，包含 IPC 尚未建立 | 期限內有結果，遲到 ready 不復活 |
| Stop Recording、預覽保持、再錄影 | 10 輪 | MP4 可解析、時間軸合理、新 frame 持續、無殘留 mux／FD 增長趨勢 |
| 錄影中 Ctrl+C／Stop Camera | 各 5 輪 | 先 finalize 再 release；可重啟；保存失敗明確而非假成功 |
| SFIS 慢回覆／拒絕期間 Stop | 僅隔離 mock 或核准測試端點 | 相機可停止，交易資料按既有規則保留，沒有重複過帳 |
| callback／IPC／NULL 故障注入 | 各類至少 1 次 | 有界回報，不冒稱硬體已釋放；不安全時 guard 生效 |

每次記錄 hostname/MAC、boot ID、版本雜湊、effective config、操作入口、業務階段、operation/session、舊新 PID、phase 耗時、封檔結果與重新取得影像證據。正常路徑不需 reboot；故障路徑允許安全鎖與人工恢復，不要求用 kill 強行達成重啟。

### 12.9 本次文件交付狀態

- [x] 核對目前分支與來源、現有錄影／相機停止入口及站台錯誤關係。
- [x] 區分錄影資源釋放與整個相機來源釋放，列出全業務階段的預期行為。
- [x] 整理上述修改順序、相依檔案、故障注入與板端驗收矩陣。
- [x] 第二階段本機程式實作與 PC 測試，範圍及限制見下節。
- [ ] 開發機部署及硬體驗收。
- [ ] 產線維護窗口驗收。

上次計畫交付僅修改文件。後續使用者授權實作後的變更記錄如下；仍未部署設備或呼叫正式 SFIS。

### 12.10 第二階段實作紀錄（2026-10-06）

來源仍為 `APL_cowork_2/nk_apl_v5`、`feature/sfis-bypass-switch`，基準 `34d0833` 加本機未提交修改。

| 檔案 | 本次交付 |
|---|---|
| `camera_lifecycle.py`（新增） | 獨立相機停止工作與 operation ID；同一 camera generation 共用停止結果；不等待站台鎖才停止；站台通知獨立執行；HTTP 等待逾時仍保留清理工作；Stop 可取消未完成 Restart |
| `visionedge_server.py` | Stop Camera 不經 station_command；Ctrl+C 共用 coordinator；先停止相機再有界停止 scanner；站台 close 也有界；新增 GET `/api/edge/lifecycle`；edge status 使用非阻塞站台快照，過期明示 status_stale；重啟驗證與掃碼隔離 |
| `edge_runtime.py` | requested/pending 與 effective config 分離；停止失敗不寫入新設定；停止旗標阻擋 Capture／Record；相機判定取消 token；錄影 FINALIZING／FAILED 結果；保留未成功 NULL 的 recorder；software recorder 重複停止與封檔結果 |
| `camera_worker.py` | record_stop IPC 未確認 recorder NULL 時回傳 requires_camera_stop，交上層停止監督；不把錄影 IPC 逾時視為封檔成功 |
| `apl_station.py` | inspect_snapshot 增加可選 cancelled callback，停止相機後的遲到判定不再變成 PASS；不修改 SFIS send／validate／request ID 或 Step1／Step4 payload |
| `static/edge_dashboard.html`、`static/apl_ui.js` | STARTING 可取消；錄影中與工單中可 Stop Camera；站台過期禁止業務操作但不禁止停止；FINALIZING／FAILED 呈現；停止錄影等待最多 50 秒，包含必要的全相機停止升級 |
| 測試 | 新增 `test_camera_lifecycle.py`；擴充 runtime、snapshot、UI 測試；保留前一階段 worker／IPC 測試 |

停止相機：最多等待 coordinator 35 秒；若仍未完成，回覆 pending 與 operation_id，背景清理不取消。站台通知僅等待 1 秒，逾時另報 station_update pending；不能宣告業務紀錄完成。站台通知尚未返回時不允許新啟動／新站台命令，防止延遲中斷影響新 cycle；狀態可查，無需持續重按停止。

停止錄影：同一錄影操作共用 finalize 結果，等待 11 秒；逾時或 recorder NULL 不明時升級完整停止相機。正常完成保留預覽；EOS 失敗但 NULL 成功會報保存失敗，不假裝 camera 也失敗。完整 Stop 的 worker watchdog／TERM／KILL／guard 沿用第一階段。軟體 native VideoWriter 卡住不能由 Python 強行安全銷毀，會回報未釋放／禁用重啟，不宣稱已完成。

不會因工號或 SN 輸入錯誤自動關相機。錯誤不再成為停止的前置條件；既有 Step4 在途 token 與回覆處理保留。已送出交易不因停止相機自動重送或偽造完成。已進入證據保存的寫入仍需安全結束；資料庫 writer 未退出時不提前關閉其連線。

PC 驗證：Windows、既有 `amr_pol_ocr`、Node.js；完整 unittest **271 項，267 通過、4 缺參考影片跳過，0 failure／error**。新增案例包括 DB 拋錯、站台鎖占用、全部主要站台狀態的 Stop API、HTTP 等待逾時、同時 Stop/Restart、掃碼器阻塞、record finalize 超時、停止後遲到判定丟棄、Step4 回覆仍正常完成，以及設定原子替換失敗仍保留舊檔。UI 使用 Node 執行實際 renderStatus，沒有瀏覽器測試；另做兩段 inline script 與 apl_ui.js 語法檢查。低空間自動停止錄影使用同一套 runtime 收尾結果檢查。

`git diff --check` 通過（僅 CRLF 提示）。`dmz_client.py`、`config/apl_workflow.json`、`config/edge.ini`、`scanner_selection.py`、`scripts/image_match_test.py` 與基準無差異。測試中 `/tmp/infer_runner.log` 不可寫是 Windows 環境警告；SFIS probe 的拒絕輸出為 mock／安全測試，不是正式送出。

同批部署檔案：`camera_lifecycle.py`、`camera_worker.py`、`edge_runtime.py`、`overlay_renderer.py`、`visionedge_server.py`、`apl_station.py`、`static/edge_dashboard.html`、`static/apl_ui.js`。其中 overlay 為第一階段變更，本次保留。不要只更新 Web 或漏掉新模組；保留設備自己的 config、certs、runtime_data。沒有安裝新套件、沒有新增 service、沒有 commit／push。

仍待驗收：第 12.8 節的開發機同 boot 重複取放、實際 USB／SFIS 慢回覆、Qualcomm callback/encoder/NULL 卡死、MP4 播放完整性與 Linux 程序群組回收。PC 對各 business state 的 Stop 測試使用替身，不能證明每個產線故障都已重現；不能將本次交付標示為硬體問題已完全解決。

## 13. 實機失敗定位與主 pipeline 停止修正（2026-10-06）

### 13.1 這次不是單純逾時

唯讀 SSH 查詢對象：`usi-smartcam-056325`、`192.168.50.212`，BSP 為
`v2.4-20-dev`、BUILD_TIMESTAMP `202606261504`。以下時間是設備 journal 的 UTC。
故障發生於 `2026-10-06 00:29:37 UTC`，即台灣時間 08:29:37。

已核對板端核心檔案與本機 `3edc07f` 相同：

- `edge_runtime.py` SHA-256：`c1ca884e99fc188961312256679db7a7a4b0e78628a3f3dc7cdef87e877e5875`
- `camera_worker.py` SHA-256：`09067128134934495776352126eae556cb25552b38f1ec6cad43739984b4c9a1`

故障鏈：

1. recovery JSON 顯示 `callbacks_drained=true`、`recorder_null=true`、
   `recorder_finalized=true`，只有 `camera_null=false`。
2. worker `6187` 請求 StopCamera；QMMF 刪除 video track 並進入 HAL CLOSE。
3. cam-server `597` 在 CamX session destroy 記錄 `2 devices orphaned` 與
   `Stale device handles on Close`，涉及 Sensor／CSIPHY；同時有 FastRPC unmap 錯誤。
4. systemd 確認 `cam-server.service` 主程序 `status=6/ABRT`，之後重啟為 `7317`。
5. worker 出現 `RecorderClient: connection closed`、`StopCamera failed`。
   coordinator 記錄約 0.951 秒即失敗、`worker_reaped=true`、`forced_exit=false`。
6. 重啟的 cam-server 又有 sensor power-up／CCI 錯誤，不能據此視為恢復成功。

因此，先前將任何 NULL 確認失敗都顯示成「Camera did not reach NULL before
timeout」是不準確的：這次是服務崩潰造成停止失敗，不是 worker 被父程序強殺，
也不能靠延長逾時或刪除 guard 當成修復。Sensor／CSIPHY stale handles 是實際
故障位置；BSP 內部為何 abort，尚無此次 backtrace，不能直接斷言是線材或硬體損壞。

### 13.2 程式停止流程的缺口與修正

參照 HPH_3 `smartcam_inference.py` 的 `handle_interrupt_signal`：先送主 pipeline
EOS、給 stream 有界收尾時間，再排空 callback、封檔、NULL。舊 APL 只對獨立
MP4 recorder 送 EOS，主相機直接 NULL。這是已確認的實作差異，不是已證明 BSP
abort 的唯一原因；不能宣稱補 EOS 必定解決 vendor close bug。

此次僅修改停止流程及診斷：

- callback 排空與獨立 recorder finalize 後，停止並 join bus reader，確保 EOS／
  ERROR 只有一個 bus consumer；主 pipeline 若仍 PLAYING／PAUSED，送 EOS 並最多
  等待 2 秒。EOS 拒絕、例外或超時仍繼續嘗試 NULL，保留警告。
- NULL 確認最多使用剩餘清理預算中的 8 秒；必須 wait return 為 SUCCESS 且 current
  為 NULL，才確認完成。FAILURE 明確報 state change failed；ASYNC／未達 NULL
  才報 deadline，記錄 set return、wait return、current、pending、耗時及分支狀態。
- appsink 設 `wait-on-eos=false`，因為停止時 Python consumer 已禁止新工作；全部
  顯示／RTP／appsink sink 禁用 `enable-last-sample`，避免額外持有最後一幀。
- worker 與 parent 的 lifecycle log 各使用 2 MiB、3 個備份的 rotating file。
  記錄 session／PID、PIPELINE_EOS、PIPELINE_NULL_RESULT、失敗分支，parent 不重複
  拼接相同錯誤。位置是 `runtime_data/edge/logs/camera_worker.log` 與 `camera_parent.log`。
- 沿用總 cleanup 20 秒、parent grace 22 秒／TERM 3 秒／KILL 2 秒；native 呼叫本身
  仍由 worker watchdog／parent 監督。沒有無界重試，沒有 Gst.deinit()，沒有自動
  重啟 cam-server、刪除 guard 或對仍不安全的相機重新取得。

與 HPH 的實際 EOS 放置次序並非逐字複製：APL 有獨立 recorder、callback gate
及 worker，需先隔離 Python consumer；main pipeline EOS 只用於 drain 主串流，
不替代 recorder 的 MP4 EOS。選定 BSP 是否接受此路徑仍需實機驗證。

未修改 `auto_start`、rotation、MediaMTX、掃碼槍、工號、SN、SFIS Step1／Step4
契約或 payload。此次是 `3edc07f` 之後的本機未提交修改，尚未部署。

### 13.3 驗證及現場下一步

Windows 既有 amr_pol_ocr：unittest **280 項，276 通過、4 缺影片跳過、0 failure／error**。
新增主 EOS 在 NULL 之前、EOS 超時／例外仍 NULL、bus consumer 排他、native FAILURE
不誤報 timeout、ASYNC 的 NULL 快照不冒稱完成、失敗分支診斷、log 輪替與錯誤去重。
這些是替身測試，不是 BSP／sensor 資源釋放成功證據。

板端此次只有唯讀查詢，未 stop/restart 程序、未更新任何現場檔案、未 reboot。
部署需更新本次 `edge_runtime.py` 與 `camera_worker.py`；其前提是完整的 `3edc07f`
核心／UI 已部署，不能與更早的 IPC 版本混用，且保留 config／certs／runtime_data。

當前開發機已因 cam-server abort 進入 same-boot guard。需先取得設備更新及一次
reboot 的核准，不能刪除 marker 跳過 guard。更新後按照第 12.8 節測 UI Stop/Start、
Ctrl+C、錄影封檔與再次取得新幀；各輪保留新 log 與 cam-server journal。如果 cam-server
再度 ABRT，記錄第一次失敗即停止重試，交 BSP／camera 團隊查 HAL close 資源管理。
未完成以上步驟前，不標示「硬體釋放已解決」。

## 14. 第二次停止失敗的實機對照與修正分工

### 14.1 本次觀察範圍

2026-10-06 約 02:17 至 02:21 UTC，經 SSH 唯讀查詢已有日誌；未操作相機、修改檔案、重啟服務或 reboot。

| 項目 | 採集結果 |
|---|---|
| 開發機 | `usi-smartcam-056325`，SSH `192.168.50.212` |
| MAC | `00:10:c6:05:63:25` |
| boot ID | `479acecc-a90c-4d3f-ae41-4ab71fc25eb1` |
| BSP | `v2.4-20-dev`，BUILD_TIMESTAMP `202606261504` |
| 部署目錄 | `/var/rootdirs/opt/nk_apl_v5` |
| 父程序 | `1890 /usr/bin/python3 -B -u visionedge_server.py` |
| 本機來源 | `feature/sfis-bypass-switch`，HEAD `dfde633` |

板端兩個核心檔案與該版本本機內容 SHA-256 相同：

- `edge_runtime.py`：`21e127d5943ced376750f1b9c123d159f5f8c3416daa1b9ad805c1296ea06a2e`
- `camera_worker.py`：`44bd122805cfaebe5614bd878cbe8cd0017852e3c70a5fde19f85cac51a0cefd`

這證明這次不是上述兩個舊檔沒有更新；沒有對整個部署包逐檔驗證，不能擴大成「全部檔案及設定完全一致」。

### 14.2 同 boot 第一輪與第二輪的差異

以下是既有 `camera_worker.log`、`camera_parent.log` 與 cam-server journal 的對照，不是本次重新操作的測試。

| 項目 | 第一輪 | 第二輪 |
|---|---|---|
| worker 啟動（UTC） | 02:02:53，PID `1911` | 02:06:17，PID `4627` |
| 停止開始（UTC） | 02:06:07 | 02:09:08 |
| callback drain | 成功 | 成功 |
| 主 pipeline EOS | 成功 | 成功 |
| pipeline NULL | SUCCESS，約 0.242 秒 | FAILURE，約 0.194 秒，current=READY、pending=NULL |
| 分支狀態 | 整體 NULL 確認成功；沒有逐分支快照 | encoder、frame_sink、h264_sink、stream_udp、display 都已 NULL；只有 camsrc 失敗 |
| worker 回收 | exit=0、reaped=true、forced_exit=false | exit=70、reaped=true、forced_exit=false |
| cam-server | PID `569`，StopCamera successfully closed | 同一 PID `569` 在 HAL close SIGABRT，systemd 之後啟動 `6682` |

兩輪都沒有 active recorder，`recorder_null/finalized=null` 是不適用，不是封檔失敗。第二輪已換成全新 worker；第一輪回收後約 9 秒才開始第二輪，不能把這次故障直接歸因為立即重開或沿用舊 Python worker。

第一輪 journal：

```text
02:06:07.909  SensorCache::MustRelease ... subDevice=1 bReleaseDevice=1
02:06:07.909  CSLReleaseDeviceHW ... release success ... CSLHwCSIPHY
02:06:07.910  SensorCache::MustRelease ... subDevice=0 bReleaseDevice=1
02:06:07.916  CSLReleaseDeviceHW ... release success ... CSLHwImageSensor
02:06:08.003  HalOp: End CLOSE
02:06:08.005  StopCamera: Camera(0) successfully closed!
```

第二輪 journal：

```text
02:09:08.711  HalOp: Begin CLOSE
02:09:08.722  MustRelease ... subDevice=1 bReleaseDevice=0 isFullRecoveryTriggered=0
02:09:08.780  CSLHwImageSensor / CSLHwCSIPHY ... refCount=0
02:09:08.780  RealTimeFeatureZSLPreviewRawYUV 2 devices orphaned
02:09:08.780  CSLCloseHW() Stale device handles on Close ... count=2
02:09:08.829  cam-server.service: Main process exited ... status=6/ABRT
```

以上為保留關鍵欄位的摘錄，不是完整原始行。失敗位置已確認：CamX 的 SensorCache／CSL 關閉路徑留下 Sensor／CSIPHY handle，HAL close 隨後崩潰。第二輪為何改走不釋放的分支、是否為 cache／reference count 或其他配置／呼叫時序所觸發，仍需 vendor source、對應 backtrace 或最小重現對照；不能宣稱已找出該分支的全部內部原因。

沒有找到此次有效的 core：`coredumpctl` 無紀錄，kernel core_pattern 為 `/var/coredump/%e.core`，該位置未見 `cam-server.core`。`/var/cache/camera/coredump` 裡的 9 月 1／2 日資料是舊事件，不能拿來代替本次 backtrace。

### 14.3 MediaMTX 的責任邊界

板端實際 service 為獨立程序，ExecStart 使用 `/var/rootdirs/opt/mediamtx/mediamtx.yml`。該 YAML 的 `smartcam` source 是 `udp+rtp://127.0.0.1:5004`，沒有啟動相機的 runOn hook；service 沒有與 camera worker 共用停止的 PartOf／BindsTo 關係。

```text
qtiqmmfsrc → 影像處理／H.264 → RTP udpsink → MediaMTX → WebRTC
     ↑
cam-server／CamX HAL，這次失敗在此來源的關閉路徑
```

採集時 MediaMTX 為 PID `4298`、NRestarts=0；兩次停止期間一直是同一程序。第二次停止結果中 `stream_udp=NULL`。02:09:09 WebRTC session 關閉，02:09:18 MediaMTX 才記錄 RTP i/o timeout，符合上游停止後沒有 RTP 的後果，不能反過來當成 MediaMTX 造成 HAL abort 的證據。

板端 AdditionalHosts 仍含舊位址 `169.254.130.130`，但已有 peer 使用 `192.168.50.212:8189` 成功建立；這是部署設定偏移，與本次 Sensor／CSIPHY 釋放故障分開處理，不能用修改 ICE hosts 當成 camera release 修復。

### 14.4 下一步修正順序

1. **保留目前停止契約，不再以補 EOS／拉長 timeout 宣告修復。** 這次 EOS 已成功，FAILURE 也不是逾時。保留 callback drain、必要 recorder 封檔、NULL／ACK／reap、錯誤和 same-boot guard；不修改 SFIS Step1／Step4。
2. **建立 standalone 最小相機重現工具。** 僅使用板端 GI／GStreamer，先測 `qtiqmmfsrc` 的 PREVIEW request pad、相同 caps → fakesink，多輪 open/EOS/NULL；不 import HPH／APL 私有程式、不送 SFIS。記錄 pad type readback、每輪 cam-server PID、各 element state 和新 frame。先對照至少第二輪，再按第 12.8 節做完整驗收。這是待實作及待核准板端操作，不是本次已完成。
3. **逐項加入分支，做有界對照。** 最小來源若也失敗，優先交 vendor；若成功，再加入 encoder、appsink、overlay／display，最後 RTP／MediaMTX。一次只改一項，沿用相同 caps／pad type／BSP；任一次 ABRT 即保存首次失敗並停止，不在損壞狀態反覆重新取得。
4. **BSP／camera 團隊修正重新開啟後的 HAL 關閉。** 對照 `SensorCache::MustRelease`、`ReleaseOneSubDevice`、`HALDevice::CloseCachedSensorHandles` 與 `CSLCloseHW` 的 ownership／reference／cache；確保 session 銷毀時 handle 不會留下或重複釋放。函式名稱來自 journal／已安裝 HAL symbol，不代表有可從 Python 安全呼叫的 API。需要匹配 BSP 的 vendor patch 或已驗證設定，不能自行猜一個 cache override 寫入板子。
5. **應用層只針對被對照證明的觸發條件修改。** 若分支／pad／時序差異確認會觸發問題，再修改 `edge_runtime.py` 的 builder／stop，補成功與失敗的逐 element 診斷及回歸；`camera_worker.py`、coordinator 保持 session 監督。不能在沒有證據時反覆 set NULL、提前 release request pad、Gst.deinit、kill cam-server 或忽略失敗。
6. **若需要暫時避開頻繁重開，另議長駐 camera owner。** cycle 切換／Stop Recording 不關來源；但 UI「暫停預覽」和「釋放相機」必須區分。長駐不是硬體釋放修復，Ctrl+C／真正 Stop Camera 仍會進 HAL close；不得偷換現有 Stop Camera 語義或承諾可繞過所有故障。

最小來源失敗不等於所有應用程式碼都正確，但可將 reproducer 範圍縮到 plugin／QMMF／HAL。最小來源成功也不等於已通過 APL 完整 pipeline，仍需分支對照。現在沒有經實機驗證、可以保證第二次 close 成功的 Python 小修；這是尚未結案的原因。

### 14.5 現場只讀查詢入口

先確認實際目錄；HTTPS／HTTP 依當次啟動設定選擇，不需再開一個相機程序。

```bash
cd /var/rootdirs/opt/nk_apl_v5
hostname
cat /proc/sys/kernel/random/boot_id
sha256sum edge_runtime.py camera_worker.py
pgrep -af 'visionedge_server|camera_worker|smartcam_webserver|mediamtx|cam-server'
tail -n 100 runtime_data/edge/logs/camera_worker.log
tail -n 100 runtime_data/edge/logs/camera_parent.log
cat runtime_data/edge/camera_recovery_required.json
systemctl show cam-server.service -p MainPID -p NRestarts -p Result
curl -skS --max-time 5 https://127.0.0.1:8765/api/edge/lifecycle
```

查本次兩次關閉的既有 journal（UTC）：

```bash
journalctl -b -u cam-server.service --since '2026-10-06 02:06:07' --until '2026-10-06 02:06:10' --no-pager | grep -E 'MustRelease|ReleaseDevice|StopCamera|CLOSE|orphan|Stale|ABRT'
journalctl -b -u cam-server.service --since '2026-10-06 02:09:08' --until '2026-10-06 02:09:09' --no-pager | grep -E 'MustRelease|ReleaseDevice|StopCamera|CLOSE|orphan|Stale|ABRT'
```

若 boot／時間已改，不能直接套用舊時間窗。`curl -k` 只供內網診斷，不是正式 TLS 驗證。不要在報告寫入密碼、完整產品 SN、工號或私鑰。

## 15. 既有問題總表與防回歸要求

### 15.1 不再混為同一個問題

| 問題 | 已處理／需要保持的契約 | 截至本次的限制 |
|---|---|---|
| Ctrl+C 同時打斷父子程序、留下 worker | 獨立 session；啟動可取消；受監督 grace／TERM／KILL／reap | PC 流程驗證不是 HAL 可重開證據 |
| worker 啟動或 native 清理卡死 | 有界 deadline、ACK 和 worker identity；缺 ACK 不假報成功 | kill 不保證釋放驅動；同 boot guard 必須保留 |
| callback 未退出就銷毀 native buffer | 停新 callback、排空，再清理；失敗保留不安全狀態 | 不可把 NULL 無條件塞進 finally |
| 錄影未封檔／NULL 失敗仍顯示保存 | recorder EOS、mux finalize、NULL 分項；未釋放參照保留 | Stop Recording 正常只釋放 recorder，不等於來源釋放 |
| UI 無法取消 STARTING 或錄影中不能 Stop Camera | coordinator／UI 支援取消及完整停止 | 仍需真正板端／瀏覽器驗收 |
| 工號／SN／DB／scanner 阻塞硬體停止 | hardware stop 不先等站台鎖、DB 或 scanner | 不改 SFIS 在途交易及原 schema |
| 停止後遲到 frame／判定使流程復活 | camera generation、operation/session 核對、拒絕遲到結果 | 保存工作及 SFIS 結案生命週期仍分開 |
| 設定先寫入再停止，requested／effective 不一致 | pending config，安全停止後才套用；失敗保留舊檔 | 不能用資料夾名稱推定模式或 rotation |
| NULL failure 一律說成 timeout | 保存 set／wait／current／pending、各 element 和 journal | READY + FAILURE 是失敗，不是已釋放或等不夠久 |
| 主來源沒送 EOS／sink 留最後一幀 | `dfde633` 已補主 EOS、sink 設定及輪替診斷 | 本次 EOS 成功仍 HAL abort，不能宣告根因已修復 |
| 第二次 HAL close stale Sensor／CSIPHY handles | 第 14 節已有第一／二輪實機對照；保留 guard | 未解決；需最小重現與 BSP／camera 修正 |
| WHEP 502、TLS 檔缺失、port 不符 | 檢查實際 listener／scheme／憑證與 upstream | Web／串流故障不等於 HAL 釋放失敗；分案處理 |

### 15.2 修改後必須保留的驗收門檻

- 一個實體 camera 只有一個 owner；同一 generation／session 只有一筆 stop，不讓 Start／Restart 越過 STOPPING 或 RELEASE_FAILED。
- callback、recorder EOS／NULL、主 EOS／NULL、ACK／reap 各自有結果。EOS 成功不能取代 NULL；程序消失不能取代 vendor release。
- 不靠登入、SN、SQLite 寫入成功才停止；不因停止自動補送或清除在途 SFIS 交易。
- 改 builder、encoder、callback、pad、stop／worker 或 BSP 後，重跑 PC 回歸及第 12.8 節的同 boot 板端矩陣；至少涵蓋第二次及連續多次開關，不能只測第一次。
- 每輪保留版本、boot ID、worker／cam-server PID、source state、回傳值、新 frame 及必要 MP4 完整性。正常重開期間 cam-server ABRT／非預期重啟即不通過，不能把自動 service restart 當成成功。
- 板端測試發生第一次不安全釋放就保存證據並停止該輪；恢復後開新的明確測試紀錄，不刪 guard 繼續累計成功次數。
- 部署保留 config／certs／資料庫／掃碼記憶／證據；同批 IPC 核心／UI 不混版。文件須分別標註 source、installed、running 與 acceptance。

### 15.3 結案紀錄

下面欄位未填入實際證據前，本問題保持未結案；不能以「已寫文件」「已 push」「PC 測試通過」取代。

| 項目 | 待填證據 |
|---|---|
| BSP／HAL 根因與修復 | vendor ticket、matched patch／設定、實際包版本與雜湊 |
| 應用版本及設定 | commit、部署核心檔 hash、有效 caps／pad／display／RTP 設定 |
| 實機身分與時間 | hostname、MAC、boot ID、BSP、採集 UTC |
| 最小 pipeline 與完整 APL | 第 14.4／12.8 節逐輪結果、第一次／第二次對照 |
| Ctrl+C／UI／錄影／各業務階段 | 次數、NULL／ACK／reap、新 frame、MP4 與 cam-server journal |
| 驗收責任 | 驗收人、日期、未通過項目與產線維護窗口 |

本文件的目的，是讓後續修改保留已知契約及重現證據，防止相同缺陷再次引入；不能保證未驗收的 BSP 或之後的新版本永遠不再出錯。
