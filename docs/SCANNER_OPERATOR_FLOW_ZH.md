# 工號與掃碼槍操作（單張判定分支）

適用來源：`NK_APL/APL_cowork/nk_apl_v5`（2026-10-01 合併自 `NK_APL/APL_test_3/nk_apl_v5-feature-sfis-bypass-switch`）。這份說明只描述本機原始碼，不代表 SmartCam 已更新或現場驗收通過。

## 操作順序

Serial Number 區是五步引導流程，同一時間只有一個輸入框可用並自動取得焦點，已完成的步驟顯示實際掃到的值：

1. **Scanner ready**：顯示掃碼槍連線狀態。未連線只是警告，不會擋住後續手動輸入。
2. **Employee number**：輸入員工工號，按 `Sign In`。
3. **Product serial number**：掃產品 SN，必須 `K2V5` 開頭。此時**還不會**呼叫 SFIS。
4. **MCIO card serial number**：掃 MCIO 卡 SN，必須 `APL` 開頭。收到後才送 SFIS Step1。
5. **Capture & inspect**：拍照判定，OK 後送 Step2。

未簽入時兩個掃碼欄位都停用；SmartCam 掃碼槍傳來的完整條碼會被站台拒絕，不會建立週期或呼叫 SFIS。掃錯標籤（在產品步驟掃到 `APL` 開頭、或在 MCIO 步驟掃到 `K2V5` 開頭）會顯示紅字並附上掃到的字串，狀態不變；前綴規則寫在 `config/apl_workflow.json` 的 `sn_prefix` 與 `mcio_sn_prefix`，比對不分大小寫。

掃碼槍是後端獨占讀取，無法靠焦點決定這一槍要填哪一格，因此由**站台狀態**決定：`WAITING_SN` 收產品 SN、`WAITING_MCIO_SN` 收 MCIO 卡 SN。瀏覽器兩個欄位也送同一支 `/api/apl/scan`，並多帶一個 `expect` 欄位，避免畫面比站台慢一秒時填錯格。

`Terminate Inspection` 固定放在面板底部，在等待 MCIO 卡序號的階段也可以按；按下後**兩個序號都會清空**並回到步驟 3。已簽入的工號會留在目前站台，後續 SN 可以連續使用。換人時須先完成或終止目前檢測，再按 `Switch operator`。

此順序同樣套用於 `--demo` 和 Browser Video Test。測試模式仍不送 SFIS；正式模式的 Step1／Step2 呼叫與 payload 沒有因本次修改而變更。切換到隔離的影片測試站台後，若畫面顯示未簽入，須在該測試站台再輸入工號。

## 查找與更換掃碼槍

Serial Number 區保留連線狀態摘要；`Find or change scanner` 預設收合。第一次使用新掃碼槍若未自動選上，請展開並按 `Find Devices`，從候選裝置確認名稱、VID/PID 與 event 路徑，再按 `Save Selection`。程式必須實際開啟該 HID 裝置成功，才會將它加入 `runtime_data/barcode/scanner_selection.json` 的已連接清單。舊版單台選擇會在成功接回後自動轉成新版格式，不需刪除舊檔。

已連接過的掃碼槍重啟、拔插或兩台交替使用時，會由背景偵測自動接回，不需再次按 `Find Devices`。上次使用的裝置仍在時優先維持；它不在且僅有另一台已知裝置時才自動切換。檢測進行中不切換掃碼槍，以免更動目前 SN 與檢測流程。只有新裝置、同時有多台且無法唯一辨識，或需要人工指定時才展開設定區。

永久身分不使用易變的 `/dev/input/eventN`。有 USB 序號時以序號辨識；沒有序號時先比對實體連接路徑。若只有一台該型號已知裝置及一台候選裝置，可在換 USB 孔後重新綁定；若兩台無序號且型號相同、無法區分，禁止猜測，必須手動選擇。

新版儲存檔使用 `version: 2`、`known` 清單與 `active` 欄位。部署前請備份原有 `scanner_selection.json`；若回退到只支援舊版單台格式的程式，也需恢復舊檔，不能直接讓舊版讀取新版檔案。這是本機掃碼槍偏好設定，不影響 SFIS request 或檢測結果資料。

沒有已存選擇時，程式先找 `config/apl_workflow.json` 中 VID/PID/名稱吻合的唯一候選；否則，若全部 USB 鍵盤候選只有一台，就自動嘗試連接，開啟成功後才記住。多台候選時必須手選。候選來自 Linux USB HID 鍵盤且支援 Enter；列在清單中不代表已確認為掃碼槍，選擇前請核對實物。SmartCam 實體掃碼槍以獨占 HID 讀取，掃碼不會直接打進瀏覽器文字框。

同一個收合介面在正式與測試畫面都可使用；Linux SmartCam 的測試模式也會啟動已選實體掃碼槍。Windows PC 沒有 SmartCam 的 `/sys/class/input`，因此沒有可選的板端 HID 裝置；仍可在簽入後使用網頁 SN 欄位測試。

若搜尋不到裝置，先在 SmartCam 查 `lsusb`、`cat /proc/bus/input/devices`、Type-C host 狀態及 powered hub。介面連線狀態與 `GET /api/apl/scanner` 可顯示目前選擇和候選清單。部署新版時務必連同新檔 `scanner_selection.py` 一起複製，不能只更新 `visionedge_server.py`。

## 驗證界線

PC 已驗證工號前置、候選／持久化選擇及 API 邏輯；實體 USB 重插、同型多台、SmartCam HID 獨占、掃碼速度及現場 Step1 行為仍需在指定板上驗收。不要用 PC 測試結果推論產線機已部署。
