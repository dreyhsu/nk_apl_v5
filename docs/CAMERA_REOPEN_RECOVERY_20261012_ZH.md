# 相機重開恢復與 BSP 延遲待辦

更新：2026-10-12。來源：APL cowork_2 的 `feature/camera-reopen-recovery` 本機修改。
初稿為本機實作；後續已取得 056325 的 APL 重開與 MediaMTX 對照紀錄，見本文末節。
尚未完成 APL 整體硬體驗收；不是 HAL 釋放問題的根治。
不變更 SFIS／ShopFloor、工號、SN、ROI、Golden Sample 或 BSP。

## 已知證據

先前 056325 開發機的獨立來源測試，共 10 輪皆取得新幀；第 2、4、6、8、10
輪關閉失敗並發生 cam-server 重啟，第 3、5、7、9 輪約 39.6 秒取得首幀。
原始記錄：`../../../camera_release_probe/logs/20260626T214044Z_1b8ab700/`。
採集工作日期 2026-10-11，板端時鐘仍為 6 月，不能以目錄日期推定採集日期。
這是最小來源測試，不能直接推論 APL 錄影、WebRTC、推論與整站流程通過。

第 3 輪建立 qtiqmmfsrc 從 monotonic 202.332733 到 240.781190，约 38.45 秒；
PLAYING 請求 240.799600，首幀 241.795907，啟播後約 1 秒。
同次 journal 可見 I2C sensor probing 與 NCS ProbeSensor 依序 timeout；
NCS sensor 1/2/3/4/8/9 的時間約為 225.672 / 228.678 / 231.685 / 234.693 /
237.711 / 240.719，RecorderService 隨後接受連線。
40 秒主要是服務重新初始化路徑的觀察值，不是應用固定 sleep；
探測延遲與第二次 close 崩潰是兩個待修問題。

## 恢復流程

1. 停止仍執行 callback drain、EOS、NULL、ACK、worker reap。
   失敗仍回報 `safe=false`／`RELEASE_FAILED`，不偽裝釋放成功。
2. 僅在有新幀、ACK、worker 已正常回收、EOS 與 callbacks 完成，且 camsrc
   停在 READY→NULL、其餘已觀察元素到 NULL、錄影未收尾失敗時，產生
   `reopen_eligible` 證據。強制 kill、未知 guard、缺 ACK 不放行。
3. 下一次 Start／Recover 或新程序啟動，檢查兩份 guard 的 boot、session、
   內容一致性、舊 worker 已退出。等待不同 PID/start-time 的 cam-server
   穩定存在 3 秒，最長 60 秒；不主動 restart service。
4. worker 初始化／新幀啟動期限由 20 秒延長至 70 秒，容納觀察到的約 40 秒。
   不是保證總操作 40 秒完成；服務等待、IPC、初始化各有獨立上限。
5. worker 至少發布兩幀、父程序收到兩個新鮮且遞增的時間戳才完成啟動。
   此為應用影像到達驗證，不等於 sensor 原生 PTS 或完全釋放證明。
   確認 ticket 未變更、cam-server 已替換後，封存舊 guard，保留恢復警告。
6. UI 顯示 Recover Camera／等待提示；啟動中可取消，恢復前不可拍照／錄影。
   站台進行中卡控與通知同步保留。Stop 不會自行重新開啟相機。
7. 不變更 MediaMTX、不讓相機永久常駐、不刪未知 guard。

## BSP 設定與真正釋放修復：待確認

尚未確認此 BSP 有可安全調整的公開設定，不臆造參數或直接降低 timeout。
韌體維護者需依實際 image／元件 Build ID 檢查：

- sensor 探測清單是否包含未安裝元件；板型、sensor XML／binary、device tree
  與載入模組是否配套。
- NCS 是否有 enable、sensor allowlist、probe timeout 的 build/runtime 設定；
  提供正式名稱、位置、預設值、作用範圍與回復方式。NCS sensor ID 不能直接
  當作相機 ID，不能只因 timeout 就關閉。
- QMMF／CamX 是否重複探測；縮短探測是否影響正常相機、EIS 或其他功能。
- 第二次 StopCamera／HAL close 的 cache handle、引用計數、重複銷毀、非同步
  teardown 是否有修補。需與 image 精確匹配的 symbols／source／backtrace。
- 修改前後至少 10 輪首幀延遲、NULL 結果、cam-server identity、journal 對照。

先唯讀蒐集 service unit／環境、os-release、qtiqmmfsrc inspect、CamX／CHI／QMMF
hash，查閱該 BSP build recipes 與 vendor 文件。不要直接修改產線 sensor 設定、
service 或共享函式庫。本次未進行新的 BSP 設定搜尋或板端變更，以上是交接待辦。

## 驗證與交付

- PC：recovery eligibility／marker／取消、subprocess IPC 啟停、lifecycle、Node UI。
  預設 ml_env 缺 Flask；完整回歸使用既有 amr_pol_ocr，沒有安裝套件。
  本次完整回歸 337 項：332 通過、4 跳過、1 錯誤。
  錯誤是 test_live_cycle_can_switch_to_browser_video_without_resetting_sn
  存取 `after['dmz']['bypass']` 的 KeyError；未擴大修改該影片／SFIS 行為。
  相機、runtime shutdown 與 UI 專項共 92 項通過；git diff --check 通過。
- 同批交付 camera_recovery.py、camera_worker.py、camera_lifecycle.py、edge_runtime.py
  與 static/edge_dashboard.html；保留板端設定與資料。
- 開發板至少 10 輪 Start→持續新影像→Stop；另測 Ctrl+C→重啟、恢復中取消、
  錄影停止失敗、未知 guard、工號／SN 錯誤與 WebRTC 重連。
- 保存 parent/worker lifecycle log、status、guard/archive、cam-server journal、
  首幀延遲與錄影結果；新影像成功與釋放失敗分欄，不只看 service active。
- 不符合恢復證據時保留 log 並停止，不手動刪 guard 假装修復。
  回復舊版需整組回復，舊版會重新嚴格阻擋 guard；保留所有診斷資料。
- 仍不能保證永不失敗；BSP 無法恢復、完整 APL 管線差異須實機驗收。

## 2026-10-12 補充：重複開關、MediaMTX 與等待時間

### 證據範圍

工作日期為 2026-10-12；板端時鐘為 2026-06-26，以下時間均照原始 log 的 UTC
記載，不將板端日期當成實際工作日期。設備為 `usi-smartcam-056325`，ADB serial
為 `cae5c705`；APL 位於 `/var/rootdirs/opt/nk_apl_v5`，MediaMTX 為 v1.19.1。

本節來源為使用者提供的兩份完整終端紀錄，以及同次 ADB 唯讀檢查：

- MediaMTX 終端紀錄：附件 `c4085239-698e-4c43-a919-3d601704211e/貼上的文字.txt`。
- APL 終端紀錄：附件 `aa490d97-8937-486a-903b-e2ff8af85dd8/貼上的文字.txt`。
- 板端 `runtime_data/edge/logs/camera_parent.log`、`camera_worker.log`、API 狀態、
  程序與 systemd 狀態，以及 loopback UDP 5004 封包觀察。

附件位於提出問題的電腦之 Codex attachments，並未收錄於 Git。板端
`camera_recovery.py`、`static/apl_ui.js`、`stream_proxy.py` 的 SHA-256 與本機
同名檔案相符；這只證明上述三個檔案，並非整份部署與工作樹完全一致。

### 三次恢復時間與結論

| RECOVERY_WAIT | RECOVERY_FRESH_FRAMES | 耗時 | MediaMTX path online | WebRTC 建立／讀取 |
|---|---|---:|---|---|
| 22:03:31.588 | 22:04:13.382 | 41.794 秒 | 22:04:13 | 22:04:13 |
| 22:05:16.634 | 22:05:57.605 | 40.971 秒 | 22:05:58 | 22:05:59 |
| 22:08:45.754 | 22:09:26.929 | 41.175 秒 | 22:09:27 | 22:09:29 |

三次均約 41 秒，這批紀錄尚不支持「每重開一次，恢復就持續變慢」的結論。
這是恢復請求到 APL 新幀的時間，不含使用者停留時間，也不是瀏覽器第一個
解碼影格的量測；WebRTC 建立／讀取成功不能單獨證明畫面持續更新。

三輪 WORKER_BOOT 分別為 22:03:35.345、22:05:20.435、22:08:49.542。
worker 啟動到 APL 新幀約 38.037、37.170、37.387 秒，主要等待落在 worker
初始化到新幀之間。現有 `wait_replacement()` 每 0.1 秒檢查，要求替換後的
cam-server 穩定存在 3 秒，最長等待 60 秒；沒有固定 `sleep(40)`。
仍需加入更細初始化時間點，才能把這段時間精確拆為 import、GStreamer、
qtiqmmfsrc 建立、PLAYING 與首幀；不能只憑本批 log 歸因到某一個 BSP 呼叫。

### MediaMTX 是否必須重啟

22:03:28 的停止流程中，callbacks 與 EOS 完成，但 `camsrc` 的 READY→NULL
失敗；encoder、frame_sink、h264_sink、stream_udp 與 display 已到 NULL。
22:03:38 MediaMTX 才回報 UDP 5004 timeout。相機來源停止供應後，串流路徑
失去 RTP，符合這次觀察到的先後順序。

完整紀錄修正先前僅憑 PID 的判斷：使用者確實在 22:04:04 按 Ctrl+C 關閉
MediaMTX，22:04:08 重開。因此不能聲稱第一輪整段 MediaMTX 都沒有重啟，
也不能由「重開後恢復」推定重開就是恢復原因。

後兩輪則有更直接的證據：同一個 MediaMTX 自 22:04:08 執行至 22:14:08，
經過兩次來源中斷，仍在 22:05:58、22:09:27 自行恢復 path，接著 WebRTC
重新連上。這兩輪不需要重啟 MediaMTX。ADB 另觀察到 APL 幀數增加與 UDP
5004 持續收到封包，但沒有在瀏覽器量測第一個解碼影格。

以下問題需分開處理：

- 最早 21:58:50 的 `:5000 bind: address already in use` 是 listener 衝突；
  同次前序檢查曾確認 gstd 佔用 5000。這與相機關閉失敗不同。
- 恢復期間 WHEP 404，對應 MediaMTX 的 `no stream is available`，不能一律
  當成 API 路由寫錯。22:04:05 的 502 落在手動停止、重開 MediaMTX 的空檔。
- `OPTIONS` 成功只能證明 HTTP 端點回應，不能當作 RTP／畫面就緒證明。
- 設定仍有 `webrtcAdditionalHosts: [192.168.50.212]`，但本次 ICE 已使用
  `169.254.130.132:5000` 成功連線；不能將舊位址直接認定為這次失敗原因。

### ADB 檢查時的服務狀態

板端 22:16:10 檢查時，APL 與 MediaMTX 程序都已停止，對應使用者在
22:14:08 停止 MediaMTX、22:14:10 停止 APL。最後一次 APL shutdown 回報
`success=true`、`safe=true`。cam-server 仍 active，PID 17669、NRestarts=6；
NRestarts 是本次服務累計值，不應全部歸因於本節三輪。

`mediamtx.service` 為 disabled/inactive，ExecStart 使用
`/var/rootdirs/opt/mediamtx/mediamtx.yml`；此次手動啟動使用
`/var/rootdirs/opt/nk_apl_v5/mediamtx.yml`。兩者設定來源不同，不能直接啟用
既有 service 就宣稱等同這次手動測試。`gstd.service` 此時 inactive，但仍
enabled，後續開機需再檢查 5000 的實際擁有者。

### 檢查與重連改善方案（尚未實作）

1. **增加相機與串流的聯合就緒檢查。** APL 處於 STARTING／恢復中時，顯示
   恢復階段及已等待秒數；確認本次 worker/session 的新幀時間與幀數持續
   增加後，再判斷串流就緒。不可沿用上一輪的 LIVE、幀數或時間戳。
2. **查 MediaMTX 的實際 path 狀態。** 評估啟用 Control API 並明確綁定
   `127.0.0.1:9997`，由後端查 `/v3/paths/get/smartcam` 的 ready 與輸入
   計數增量。應使用有上限的查詢逾時及短期快取，避免每個瀏覽器各自密集
   探測。API 尚未開啟／無法查詢要回報 unknown，不冒充串流中斷。
3. **新幀與 path 就緒後立即發起一次 WHEP。** 現有 `wantedStreamKey()`
   主要檢查 running，尚未配合恢復階段；失敗後約 3 秒重試，另有 15 秒
   無解碼影格 watchdog。恢復期間可見多次無來源連線。改為等待就緒、
   就緒轉換時立即連線，同一 generation 僅允許一個嘗試，取消舊請求與
   session，避免重複連線或多等一個重試週期。現有重試未見逐輪累積退避。
4. **分層顯示原因。** 區分相機恢復中、MediaMTX 未啟動、RTP 未到達、
   WHEP/ICE 失敗及瀏覽器解碼停滯。path 尚未 ready 時不重啟 MediaMTX；
   PID 或 HTTP 存活也不能直接當成影像正常。
5. **補足耗時紀錄。** 以 monotonic 記錄停止、cam-server 替換、worker
   boot、初始化各階段、APL 新幀、path ready、WHEP 成功與瀏覽器首個
   decoded frame。每筆附 camera generation/session，避免混用不同輪次。

預計修改範圍為 `static/apl_ui.js`、`stream_proxy.py`、`visionedge_server.py`
的狀態整合，以及 `mediamtx.yml` 的本機管理 API；如需細分相機初始化耗時，
再於 `camera_worker.py`／`edge_runtime.py` 加診斷時間點。確切欄位與錯誤
契約於實作時確認。本次只記錄方案，沒有修改程式、啟用 API 或調整板端服務。

此方案可减少來源未就緒時的無效連線與播放器額外等待；不能宣稱能消除
約 37～38 秒的初始化時間，亦不代表已修復 HAL 釋放失敗。

### 後續驗收與官方參考

- 至少 10 輪開關，MediaMTX 保持同一程序，分別量測正常重開與釋放失敗後
  恢復。以瀏覽器 framesDecoded 持續增加驗證，不能只看 WebRTC connected。
- 測恢復中取消、快速重複按鈕、重新整理頁面、MediaMTX 未啟動、5000
  被佔用、RTP 中斷及管理 API 不可用；不得並行啟動相機 owner。
- 分別記錄相機新幀與瀏覽器新幀延遲、WHEP 嘗試次數、停止結果與
  cam-server identity；若後續輪次變慢，再由各階段耗時定位累積點。
- [MediaMTX Control API 說明](https://mediamtx.org/docs/features/control-api)。
- [v1.19.1 API 定義](https://github.com/bluenviron/mediamtx/blob/v1.19.1/api/openapi.yaml)：
  對照目前版本的 path ready 與輸入計數欄位。
- [v1.19.1 設定範本](https://github.com/bluenviron/mediamtx/blob/v1.19.1/mediamtx.yml)：
  確認 api、apiAddress 及認證設定；不要直接套用最新版文件的所有參數。
