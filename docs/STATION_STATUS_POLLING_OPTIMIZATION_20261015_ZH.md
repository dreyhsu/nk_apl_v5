# 站點狀態輪詢與黃色提示優化紀錄（2026-10-15）

範圍：本機來源 `D:\work_project\smartcam\APL_fail\nk_apl_v5`。本文記錄目前觀察、程式碼原因及後續優化方案；除本文件與文件索引外，本次沒有修改程式碼、部署 SmartCam、重啟服務或變更 SFIS／MediaMTX 設定。

## 目前狀態

2026-10-15 使用者觀察到：

- 即時相機畫面存在且持續更新。
- 自動模板判定可正確完成。
- UI 偶爾短暫顯示黃色 `Connecting to station...`，之後會自行消失。
- 目前沒有證據顯示黃色提示發生時，相機、RTP、MediaMTX 或 WebRTC 同時中斷。

目前來源已包含正式版整合的 3 FPS 單一背景自動探測、避免在相機取像迴圈同步執行完整模板比對，以及依 WebRTC 實際解碼狀態維持畫面的前端邏輯。完整來源狀態與驗證邊界見 `PRODUCTION_STREAM_AND_MATCHING_INTEGRATION_20261015_ZH.md`。

因此，這次黃色提示應先視為「站點狀態的新鮮度短暫不足」，不能直接解讀成相機或 MediaMTX 斷線。

## 現行判定方式

前端約每 800 ms 呼叫一次 `/api/edge/status`，單次請求逾時為 4.5 秒。`stationFresh()` 另要求：

1. 至少成功取得過一次狀態；
2. 最後成功時間距今少於 5 秒；
3. 沒有狀態 API 錯誤；
4. `station.status_stale` 不為 `true`。

只要本次 station snapshot 被標成 stale，即使 HTTP API 成功、畫面仍在解碼、自動判定仍能運作，`renderStation()` 也會把提示文字設為 `Connecting to station...`。這使「初次尚未連線」與「已連線但短暫使用快取狀態」共用同一個文案，造成誤判。

後端 `responsive_station_status()` 以非阻塞方式嘗試取得兩層鎖：

- `visionedge_server.py` 的 `_station_lock`；
- `APLStation.lock`。

任一層當下無法取得，就立即回傳最後一次 `_station_snapshot` 並設定 `status_stale=true`。因此一次非常短的鎖競爭也足以讓黃色提示閃現。

## 已找到的鎖競爭來源

### 1. 站點命令與 SFIS／DMZ

`station_command` 目前使用 `_station_lock` 包住完整路由操作。掃描產品 SN 後，命令可能一路進入 SFIS Step1。

`APLStation._confirm()` 已正確在送出網路請求前釋放 `APLStation.lock`，避免 SFIS 等待時間鎖住 station 內部狀態；但是外層 `station_command` 的 `_station_lock` 仍可能保持到整個路由結束。這段期間 `/api/edge/status` 無法取得 `_station_lock`，只能回傳 stale snapshot。

因此 SFIS 回應較慢時，UI 可能短暫顯示 `Connecting to station...`，即使相機與 MediaMTX 完全正常。

### 2. 自動判定提交與證據儲存

自動模板探測已移到單一背景 thread，不再阻塞相機主取像迴圈；但探測命中或 20 秒到期後，`inspect_snapshot()` 仍需提交判定結果、繪製結果影像、儲存原圖／結果圖、更新 SQLite 與 journal。

其中部分工作位於 `APLStation.lock` 範圍內。若圖片編碼、磁碟寫入或 SQLite 同步花費較久，狀態 API 在同一時間查詢就會拿到 `status_stale=true`。Golden Samples 數量主要影響背景比對時間；結果圖與證據寫入則影響提交階段的鎖持有時間，兩者應分開量測。

## 建議優化

第一階段 UI 修正與基本狀態可觀測性已於本機來源實作；縮小後端鎖範圍及把證據寫入移出臨界區仍是待實作方案。上述變更尚未部署或通過 SmartCam 驗證。

### 第一階段：修正 UI 語意與去除單次閃爍

- 前端增加 `hasConnectedOnce`，只有從未成功取得 station 狀態時才顯示 `Connecting to station...`。
- 已成功連線後，單次 `status_stale=true` 應保留最後一次有效畫面與站點狀態，不退回 Connecting。
- 若連續取得快取狀態，可顯示較準確的 `Station status updating...`，並記錄快取持續時間；不把它稱為斷線。
- 只有 HTTP 狀態 API 連續失敗或逾時，才顯示 `Connection lost. Reconnecting...`。
- 狀態過舊時仍可暫停掃碼／提交按鈕，避免用舊狀態接受操作；顯示文字與操作安全需分開處理。

本機來源已實作上述行為：首次取得有效狀態前仍顯示 Connecting；成功連線後的快取 snapshot 會隱藏提示，但 `freshStation=false` 仍會暫停依賴即時狀態的操作；真正 API 錯誤仍顯示 `Connection lost. Reconnecting...`。前端檔案查詢版本已更新，避免瀏覽器沿用舊 JavaScript。

不建議只把 5 秒門檻改成更大的數字。延長門檻只能掩蓋問題，無法區分初次連線、快取狀態和真正 API 失聯。

### 第二階段：縮小後端全域鎖範圍

- `_station_lock` 僅用於建立、替換或關閉全域 station 物件，不應包住完整 SFIS／DMZ 網路等待。
- `responsive_station_status()` 在 station 已建立後，應能讀取最後一次原子快照，不因外層命令鎖忙碌就宣告連線狀態失效。
- 若掃碼器與 Web 掃碼需要序列化，使用用途明確的 command／scan lock；不要讓狀態讀取和外部網路等待共用同一把全域鎖。
- 保留 cycle、request token、late response 與狀態轉移檢查，不能為了減少黃色提示而允許兩次 Step1／Step4 並行。

### 第三階段：縮短 station lock 內的重工作業

- 在鎖內只驗證 cycle／generation、預留 request token 並提交必要的狀態轉移。
- 使用不可變 snapshot 在鎖外繪製結果圖、JPEG 編碼及準備證據資料。
- 磁碟與 SQLite 寫入若移到鎖外，必須以 attempt token／cycle generation 做二次確認，避免終止、重置或晚到結果污染下一個 cycle。
- 寫入失敗仍必須回到 `RECOVERY_REQUIRED`，並保留現有證據一致性與錯誤紀錄；不能只為縮短延遲而犧牲交易完整性。

### 第四階段：補足可觀測性

建議在 `/api/edge/status` 或診斷事件加入：

- `station_snapshot_source`: `live` 或 `cache`；
- `station_snapshot_age_ms`；
- `station_status_busy_reason`: `global_lock`、`station_lock` 或空值；
- 前端連續 stale 次數及持續時間；
- 自動探測、判定提交、結果繪製、影像寫入、SQLite／journal 各階段耗時。

這些欄位不得包含 SN、工號、SFIS payload、憑證或其他生產敏感資料。

本機來源目前已加入 `status_stale_reason`、`status_snapshot_age_ms`、stale episode 次數與恢復時間。一般服務 log 在 stale episode 開始時發出節流 warning，恢復時記錄 info；啟用 `APL_DIAGNOSTICS=1` 時另寫入 `STATION_STATUS_STALE`／`STATION_STATUS_RECOVERED` 及瀏覽器採樣欄位。標準 warning 最多每 10 秒一次，避免約 800 ms 的狀態輪詢洗滿 journal。

## 建議驗收條件

1. 相機持續有畫面、自動判定正確時，單次或極短的 stale snapshot 不再顯示 `Connecting to station...`。
2. SFIS Step1／Step4 等待期間，畫面與最後有效站點狀態維持，UI 可顯示忙碌但不能誤稱正在初次連線。
3. 自動判定提交與證據儲存期間，`station_snapshot_age_ms` 可觀察，但不影響相機取像與 WebRTC 解碼。
4. 真正停止 8765 API 或造成連續請求逾時時，前端仍會明確顯示 `Connection lost. Reconnecting...`。
5. 掃碼、Step1、Step4、終止、重試與 late response 測試均通過，沒有因縮小鎖範圍造成重複請求或跨 cycle 寫入。
6. PC 測試只能驗證狀態機、鎖行為與 UI；Qualcomm 相機、MediaMTX、WebRTC、實際 SFIS 延遲仍需在目標 SmartCam 驗證。

## 本機驗證

- `node --check static/apl_ui.js`：通過。
- UI、診斷與相機生命週期針對性測試：通過。
- `amr_pol_ocr` 完整測試：359 項通過、4 項因缺測試影片跳過。
- 測試覆蓋首次連線、已連線後短暫 stale、真正 API 錯誤、stale reason 與 snapshot age 診斷欄位。
- 尚未部署到 SmartCam，因此板端瀏覽器顯示、systemd journal 與真實 SFIS 延遲仍待驗證。

## 結論

目前最合理的判斷是：黃色提示是狀態快照鎖競爭與前端提示語意過度敏感共同造成的暫時性 UI 狀態。現有觀察不支持把它直接歸因於相機、MediaMTX 或模板判定失敗。優化應先修正快照發布與 UI 狀態模型，再縮短全域鎖和證據寫入的臨界區；各階段需保留 SFIS 交易與 cycle 一致性。
