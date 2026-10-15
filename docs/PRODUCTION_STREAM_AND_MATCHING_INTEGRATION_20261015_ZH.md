# 正式版自動比對與串流整合（2026-10-15）

範圍：本機來源 `APL_fail/nk_apl_v5`，以 `APL_fail/nk_apl_v5_test_2` 的已實作修正作對照。沒有部署、重啟或修改產線 SmartCam；測試板 `649274` 的運行狀態不等於正式機的安裝或運行狀態。

## 原因與本次修正

原本自動探測在相機主取像迴圈同步做完整模板比對。測試板先前 521 次比對平均約 239.65 ms、最大約 280.71 ms，超過 5 FPS 的 200 ms 週期，容易持續占用主迴圈。另單張預覽等待站台的 `frame_lock`；前端即使仍有 WebRTC 解碼畫面，也可能因狀態 API／`frame_seq` 暫停而隱藏或拆掉連線。測試板曾同時出現 MediaMTX `RTP packets lost`、`invalid FU-A packet` 與 UDP `RcvbufErrors` 增加；這些是不同層的證據，不能斷定全部由比對負載造成。

- 自動比對上限改為 3 FPS，保留啟用、連續命中兩次、20 秒及 `not_matched`；用單一背景探測避免阻塞取像與排隊，結果核對來源影格、相機 generation、cycle、attempt；停止時有限等待，未完成則禁止直接重開。
- 預覽不再等待比對的 `frame_lock`；先裁 ROI 再轉灰階，模板與門檻不變。
- 前端在狀態輪詢短暫失效時，依實際 WebRTC 解碼是否仍前進來維持畫面；相機明確停止或解碼停滯仍可隱藏／重連。
- 加入預設關閉的 `APL_DIAGNOSTICS=1` 遙測，記錄比對耗時、相機／RTP 階段、WHEP、HTTP 與瀏覽器統計；`summarize_diagnostics.py` 可彙整事件。診斷不儲存 SDP 或任意瀏覽器請求內容。
- 新增 `capture_stream_diagnostics.py`：只讀收集目前服務的 MediaMTX／cam-server／kernel journal、程序與系統 UDP 計數；預設執行 300 秒，目錄限制為 owner 可讀。它不啟動或停止相機、不修改 MediaMTX、不強制 SFIS PASS。可在板上執行 `python3 -B -u capture_stream_diagnostics.py --seconds 300 --run-id incident-01`，然後以 `python3 -B summarize_diagnostics.py runtime_data/diagnostics/incident-01` 彙整。系統 UDP 計數並非 MediaMTX 單一 socket 證據。
- `mediamtx.yml` 增加 1 MiB UDP 接收緩衝；`mediamtx.service` 是正式路徑的**範本**，啟動前設定 `net.core.rmem_max=2097152`。MediaMTX 執行檔仍位於外部 `/var/rootdirs/opt/mediamtx/mediamtx`。只複製本目錄的 YAML／service，不會自動取代板上已啟動服務或其外部 YAML。

## 正式 DMZ／SFIS 與串流設定

`config/edge.ini` 的網頁 TLS 為 `on`，對外網址是 `https://<SmartCam-IP>:8765/edge`。正式 `visionedge-apl.service` 另指定 `VISIONEDGE_TLS=on`，`run_apl.sh` 拒絕關閉 TLS 的環境覆寫。TLS 憑證／私鑰不在本機 Git 來源中，部署時必須在設定路徑提供有效檔案；缺檔會拒絕啟動，不會降級為 HTTP。WHEP 的 `127.0.0.1:8889` 是裝置內部 HTTP 代理上游，瀏覽器仍連 HTTPS `:8765`；ICE/TCP `:5000` 是另一條 WebRTC 媒體連線，不是 HTTP 網頁。

`config/apl_workflow.json` 保持 `mode=http`、`api_style=sfis`、`bypass_sfis=false`、`step4_enabled=true`。`APL_DMZ_MODE=http` 是可處理 HTTP／HTTPS URL 的 client 模式，不是 SmartCam 網頁協定。原 `visionedge-apl.service` 的 `APL_DMZ_MODE=disabled` 會蓋掉 JSON；本次已改成 `http` 並設定 `APL_DMZ_BYPASS=off`，啟動腳本拒絕停用 DMZ 或啟用 bypass。**SFIS 出站 URL 目前仍為明文** `http://10.0.28.125:8097/api/SFISAPI`，且 `allow_insecure_http=true`；在 SFIS 負責人提供有效 HTTPS URL／連接埠與憑證要求前，不能宣稱 SFIS 已使用 HTTPS，也不能任意把原網址改成 `https://`。取得端點後應改 `base_url`、設 `allow_insecure_http=false`，再驗證 `/Step1`／`/Step4`。沒有帶入測試入口 `run_diagnostics.py` 或其固定 PASS 設定。板上若有未更新的 unit／drop-in，仍須查 `systemctl cat visionedge-apl.service` 與實際進程環境。現有 `dmz.line=TEST`、`station=V5_AI-CAM`、`employee_no=M031978` 是來源中的值，須由 SFIS 負責人確認是否為正式站點資料。PC 無法驗證內網可達、憑證／防火牆、實際 Step1／Step4 契約或對方接受結果；不可把靜態設定當作現場串接成功。

串流鏈為相機 worker → `127.0.0.1:5004/UDP` RTP → MediaMTX `smartcam` → WHEP `127.0.0.1:8889` → HTTPS `:8765` 同源代理 → 瀏覽器 ICE/TCP `:5000`。MediaMTX `webrtcAdditionalHosts` 使用先前產線紀錄的 `10.6.34.183` 及直連管理位址 `169.254.130.130`；部署到不同機器或 DHCP 位址改變時必須依現場 `ip -4 addr show dev eth0` 更新，不能固定沿用。正式版 Browser Video Test 預設關閉。

若正式板上的 `mediamtx.service` 仍以 `/var/rootdirs/opt/mediamtx/mediamtx.yml` 啟動，需先對照服務內容與實際安裝目錄，再決定安裝本範本或同步外部 YAML。此步會變更 live service，不屬於本次本機原始碼修改；不可僅因本地 `mediamtx.yml` 已更新就宣告板端生效。

## 驗證邊界與上線核對

本機以既有 `amr_pol_ocr` 環境執行 `python -B -m unittest discover -s tests`：356 項通過、4 項因缺影片跳過；`node --check static/apl_ui.js` 通過。Git Bash `bash -n run_apl.sh` 通過，分別注入 `APL_DMZ_MODE=disabled` 及 `APL_DMZ_BYPASS=on`，兩次皆於啟動 Python 前拒絕。修正兩個舊測試假設：每類 Golden Samples 至少兩張而非恰好兩張（目標類現有八張），以及工程影片 API 測試須顯式啟用，不能依賴正式設定。沒有更改 Golden Samples。獨立只讀收集器的 `--help` 與單元測試通過；其板端 journal 收集尚未驗收。

Qualcomm camera、編碼器、RTP 丟包、MediaMTX／WebRTC、SFIS 真實過帳需在目標板驗證。上線前核對：正式目錄及 service 的 `ExecStart` 指向同一份 YAML、MediaMTX 啟動日誌載入該路徑、`:8889`／`:5000` 監聽、來源 UDP `5004` 有封包、`APL_DMZ_BYPASS` 未開、站點欄位經 SFIS 核准。若仍卡畫面，對照同一時間的 `MATCH_SAMPLE`、RTP／UDP 丟包、WHEP 狀態及瀏覽器 `framesDecoded`，不要只以 `LIVE` 或 MediaMTX 程序存活判定有畫面。
