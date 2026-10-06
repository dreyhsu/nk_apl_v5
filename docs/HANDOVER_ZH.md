# NK APL 單張判定版本交接手冊

## 1. 交接範圍

PC 原始碼：

```text
D:\work_project\smartcam\NK_APL\APL_cowork_2\nk_apl_v5
```

SmartCam 直接部署慣用目錄：

```text
/var/rootdirs/opt/VisionEdge_APL
```

這是獨立的單張判定版本。`NK_APL/VisionEdge_APL` 的多步流程是另一個 lineage，
不可互相覆蓋設定或程式。

2026-10-05 本次修正基準為 `feature/sfis-bypass-switch` 的 `34d0833`（17:19:51 +08:00）。
相機釋放修正尚在 working tree；來源、實際部署目錄與執行程序要分別核對。
即使裝置目錄叫 Go Live，也不能據此認定執行的是自動錄影模式。

## 2. 功能目標

操作員上班先輸入工號簽入，再掃描 SN（系統以工號 + SN 呼叫 SFIS Step1），
完成蓋板與 MCIO 小卡安裝後按下 `Capture & Inspect`。程式只判定
該張照片；同一張照片必須同時符合蓋板關閉及 MCIO 小卡存在，才顯示 OK。

完成 OK 後由操作員選擇：

- `Confirm OK & Send SFIS Step4`：視覺判定 OK 後回報 SFIS。
- `Terminate Inspection`：不上傳，保存終止紀錄並開放下一筆 SN。

## 3. 核心元件

| 檔案 | 責任 |
|---|---|
| `visionedge_server.py` | HTTPS、APL API、頁面、掃碼與服務入口 |
| `edge_runtime.py` | 相機、畫面、錄影、擷取與推論執行環境 |
| `apl_station.py` | SN 鎖定、狀態機、上傳與終止規則 |
| `apl_matching.py` | ROI 與 Template Matching 判定 |
| `inspection_store.py` | 原圖、結果圖、JSON 與 History 索引 |
| `barcode_reader.py` | Honeywell HID 掃碼事件 |
| `video_test.py` | 開發影片來源與正式來源隔離 |
| `stream_proxy.py` | WHEP 同源代理 |
| `workflow_view.py` | OpenCV workflow 疊圖 |
| `static/apl_ui.js` | 英文操作介面、SN、影片、History 與狀態顯示 |

`visionedge_server.py` 目前仍引用上游 `server.py`；`edge_runtime.py` 仍使用
`vision_core.py`、`camera_worker.py` 與 `overlay_renderer.py`。這些檔案看似舊版，
但尚未解耦前不能刪除。

## 4. 狀態機

| 狀態 | 意義 | 可用操作 |
|---|---|---|
| （未簽入） | 尚未輸入工號 | 輸入工號簽入；SN 欄位停用 |
| `WAITING_SN` | 已簽入，等待下一筆產品 SN | 掃碼、切換操作員、選影片、切回相機 |
| `SN_CONFIRM` | 送出 Step1 的瞬間狀態；若 Step1 被拒則黏在此處等重試（`step1_retry_required=true`） | 重掃同一張產品 SN 重試、切換操作員、終止 |
| `WAITING_MCIO_SN` | 產品 SN 已通過 Step1，等待 MCIO 卡 SN | 掃 MCIO 卡、終止、開發時切影片 |
| `WAITING_CAPTURE` | 兩個 SN 已鎖定（Step1 在產品 SN 時已通過），等待照片 | 拍照判定、終止、開發時切影片 |
| `JUDGING` | 正在判定與保存證據 | 等待 |
| `NOT_MATCHED` | 未同時符合兩個條件 | 重拍、終止、開發時換影片 |
| `PASS_CONFIRM` | 視覺 OK，等待人工結案 | 上傳或終止 |
| `UPLOADING` | 正在送出 PASS | 等待結果 |
| `RECOVERY_REQUIRED` | 相機、儲存或程序中斷 | 檢查後終止該輪 |

安全原則：一輪未完成時，任何來源的新 SN 都不能覆蓋既有 SN。上傳失敗或回覆
不確定時，仍保持鎖定。

## 5. 判定設定

`config/apl_workflow.json` 目前重要值：

```json
{
  "inspection_mode": "single_snapshot_cover_card",
  "reference_size": [1920, 1080],
  "search_roi": [751, 254, 431, 588],
  "threshold": 0.8,
  "min_gap": 0.05,
  "method": "TM_CCOEFF_NORMED",
  "target_state": "50_COVER_CLOSED_CARD_PRESENT",
  "snapshot_reject_states": [
    "50_COVER_CLOSED",
    "50_COVER_OPEN_CARD_PRESENT"
  ]
}
```

`target` 分數必須達門檻，且分別高於「關蓋無卡」與「開蓋有卡」至少
`min_gap`。判定框只在接受時顯示。

調整 ROI、門檻或樣本前，必須保留原始設定，並使用正常、無卡、開蓋、遮擋、
偏移與錯誤安裝資料重新驗證。

## 6. 相機與串流

`config/edge.ini` 目前主要設定：

- Backend：`qti`
- Camera：`0`
- 畫面：`1920 × 1080 @ 30 FPS`
- Rotation：`180`
- Web：HTTPS `8765`
- Live view：WebRTC
- RTP：`127.0.0.1:5004`
- MediaMTX WHEP upstream：`127.0.0.1:8889`
- Media path：`smartcam`

同一時間只能有一個程序持有相機。不要同時啟動另一套 VisionEdge、capture app
或 v2.4 camera app。MediaMTX 也只能由一種部署方式持有相同連接埠。

若出現 `qtiqmmfsrc is unavailable`、相機無法啟動或 WHEP 502，依照
[QTI 相機與 MediaMTX 故障排除](QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md)處理。

## 7. 掃碼槍

目前掃碼器設定：

- Vendor ID：`0c2e`
- Product ID：`0901`
- 裝置名稱：`Honeywell`
- SN：可列印 ASCII、不含空白、最多 128 字元

SmartCam Type-C 必須是 Host，並使用有外接電源的 Hub。此模式下，日常管理應走
Ethernet HTTPS／SSH，同一個 Type-C 接口不能同時作為 USB ADB device。

正式設備、IP、MAC 與 USB Host guard 記錄位於上層：

```text
../production_smartcam_records/
../smartcam_usb_host/
../smartcam_barcode_listener/
```

## 8. 影片測試

Browser Video Test 用於無法進入產線時的開發驗證：

- MP4 留在 PC 瀏覽器，不上傳整支影片。
- 可播放、暫停、前後 5 秒及拖曳時間軸。
- `Capture & Inspect` 只傳送當下 Canvas 畫面。
- 掃碼後仍可在 `WAITING_CAPTURE`／`NOT_MATCHED` 選擇或更換影片。
- 切換時保留同一個 SN 與 cycle ID。
- 測試結果禁止上傳 SFIS。

正式產線務必設定：

```ini
[features]
video_test_enabled = false
```

## 9. 證據與 History

每次判定嘗試均保存：

- 原始擷取圖
- 有判定框與狀態的結果圖
- Matching JSON
- SN、cycle ID、attempt ID
- 判定時間、來源與影片相對時間
- 視覺結果及結案／上傳狀態

OK、NOT_MATCHED 與終止紀錄必須分開理解。視覺 OK 不代表已上傳；
`TERMINATED` 不得顯示成正式 COMPLETE。

## 10. SFIS 串接

正式串接使用 `station_mode=remote`、`dmz.api_style=sfis`，且必須設 `dmz.bypass_sfis=false`。目前版控設定為工程測試用的 `bypass_sfis=true`；這會在本地接受 Step1／Step4，完全不送 SFIS，不能當作正式過帳成功：

- 操作員先在頁面輸入工號簽入（`POST /api/apl/operator`），工號存在站台端記憶體，掃碼時鎖進該 cycle 並帶入 Step1／Step4 的 `empNo`。切換操作員用 `POST /api/apl/operator/clear`。
- Step1：**掃完產品 SN 後立即**呼叫 `/api/SFISAPI/Step1`（不等 MCIO 卡），由 SFIS 驗證「工號 + 產品 SN」組合。Step1 的 body 不含 MCIO 卡序號，所以無須先裝卡即可驗證。
- Step4：影像 OK 且操作員確認後呼叫 `/api/SFISAPI/Step4`。
- Step4 有獨立安全開關 `step4_enabled`，預設為 `false`；未明確開啟時不會送出 HTTP request，畫面並會提示聯絡網頁開發人員 SMD（Dre Hsu / TW031561）。
- 目前已確認欄位：`line`、`station`、`sn`、`empNo`（小寫，依 Swagger）；`empNo` 取簽入工號，未簽入時 fallback 設定檔 `employee_no`。
- Step4 另外帶 MCIO 卡序號，欄位名由 `dmz.step4_mcio_field` 決定（預設 `mciosn`）。**此欄位名尚未經 SFIS Swagger 確認**，上線前必須用 `scripts/sfis_api_test.py --step4-test --mcio-sn <APL...>` 對真機驗證；欄位名不符時只需改設定檔，不必改程式。缺 MCIO 序號時 Step4 會在送出前就被擋下。
- API 即使回覆 `success=true`，只要 `data` 以 `FAIL` 開頭仍視為業務拒絕。
- Step1 失敗會**保留產品 SN 與 cycle**，黏在 `SN_CONFIRM`（`step1_retry_required=true`）：操作員排除原因後**重掃同一張產品 SN** 即重送 Step1（每次重試換新的 `sn_request_id`），或按 Terminate 清掉重來；不允許拍照判定。若失敗訊息含 `dmz.operator_error_markers`（預設 `NO EMP`）代表工號錯誤，站台**不清除任何東西**，只提示操作員按 Switch operator 改用正確工號再重掃（改工號會記 `OPERATOR_CHANGED` 並帶進下一次 Step1 的 `empNo`）。
- Step4 失敗會保留 SN、cycle 與相同 request ID，供操作員重試。
- Step1 的重試次數目前無上限；transport error 與業務拒絕在站台端無法區分，timeout 後站台不知道上一次嘗試是否已到 SFIS。若操作員改工號後重試，SFIS 可能對同一張產品 SN 留下兩筆不同 `empNo` 的 Step1 紀錄。站台 journal 以 `SN_RESCANNED_FOR_STEP1_RETRY`、`SN_CHECK_REQUESTED` 的 `request_id` 與 `OPERATOR_CHANGED` 保留對帳線索。**須與 SFIS 負責人確認 Step1 是純查詢或有 side effect（例如報到）**；若有 side effect 可能需要 server 端去重規則。
- 關卡提前的副作用：在 `WAITING_MCIO_SN` 按 Terminate 現在等於「Step1 已被 SFIS 接受但 cycle 被放棄」（與既有的 `WAITING_CAPTURE` Terminate 同性質，只是窗口早一步）。
- F12 的每行 Step1 trace 會顯示 `MCIO —`，因為 Step1 執行時 MCIO 卡序號尚未存在，屬正常。
- `APL_DMZ_URL`、`APL_SFIS_LINE`、`APL_SFIS_STATION`、`APL_SFIS_EMP_NO` 可覆寫設定檔。

Step4 目前依現有資料先使用與 Step1 相同的四個欄位。若 SFIS 的 Step4 Swagger
另有結果碼或必填欄位，填入 `step4_extra_fields` 並補做現場驗收後才可視為量產完成。
測試程式必須在自己的 client 設定中明確加入 `"step4_enabled": true`，不可依賴正式設定檔開啟。
手動測試使用 `scripts/sfis_api_test.py --sn <SN>` 時只會呼叫 Step1；只有另外明確加入
`--step4-test` 才會在該次測試程序中暫時允許一筆 Step4，正式設定仍維持關閉。使用
`--step4-test` 時必須同時給 `--mcio-sn`。

### 升級注意（兩段掃碼版本）

`_recover` 會把升級前就存在的 `PASS_CONFIRM` cycle 以空的 `mcio_sn` 還原，該筆的 Step4 會以
`MCIO card serial number is missing` 失敗（可按 Terminate 脫身，證據保留，但該台要重跑）。
**升級前請先把所有未結束的 cycle 收到 `WAITING_SN`。** 另外 `static/apl_ui.js` 的 `?v=` 版本字串
已改為 `20261006-1-step1-on-product-sn`，更新時務必連同 `edge_dashboard.html` 一起部署，否則瀏覽器會
拿舊 UI 配新後端：Step1 失敗時舊 UI 不認得 `step1_retry_required`，會把 `SN_CONFIRM` 重試狀態的
SN 欄位停用，操作員只能按 Terminate 重來，無法原地重掃重試。
這支工具會忽略設定檔的 `bypass_sfis=true` 並改走 HTTP；若環境變數 `APL_DMZ_BYPASS` 強制開啟則拒絕執行，避免假成功。實際執行前需確認網路與 SFIS 測試授權。

舊版的 `dmz.sfis_enabled` 不能與新版直接混用：新版遇到此鍵會拒絕啟動，防止原本 `false` 的離線站台意外連向 SFIS。更新程式時須同步審核 `config/apl_workflow.json`。`bypass_sfis` 只接受 JSON 布林值；正式 Step1／Step4 的 URL、四個欄位、回覆規則及 Step4 安全開關維持原樣。

## 11. PC 開發驗證

在專案根目錄執行：

```powershell
python visionedge_server.py --demo --no-tls --port 8772
```

測試頁面：

```text
http://127.0.0.1:8772/edge
```

回歸測試：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

此指令涵蓋本目錄的測試；`tests/legacy/` 是上游 VisionEdge 測試，不在此指令範圍。PC 結果不能取代 SmartCam HID 與現場 SFIS 驗收。

## 12. SmartCam 啟動

前景啟動可直接查看 log：

```bash
cd /var/rootdirs/opt/VisionEdge_APL
/usr/bin/python3 -B -u visionedge_server.py
```

停止使用 `Ctrl+C`，等待回到 shell prompt 及停止結果，不要連續重開或跳過收尾。
若實際部署在別的目錄，先確認該目錄的程式／設定，再替換上述 `cd`。
啟動前確認：

```bash
ps -ef | grep -E 'visionedge|python3' | grep -v grep
systemctl status mediamtx --no-pager
ss -lntup | grep -E ':(8765|8889|5000)\b'
```

不要停止平台 `cam-server`；它是 Qualcomm 相機服務的一部分。

### 停止與重新啟動（2026-10-05 本機修正）

2026-10-06 第二階段已補 Web Stop Camera 與站台紀錄／鎖的隔離。Ctrl+C 和 Web Stop 共用 `camera_lifecycle.py`；GET `/api/edge/lifecycle` 可查 operation、phase、station_update 和 release 結果。全業務階段、錄影停止與 UI 修正及限制見[計畫第 12 節](CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md#12-第二階段所有業務階段都能停止與回收)。

Stop Recording 只回收錄影資源並保留預覽；Stop Camera／Ctrl+C 才要求回收相機來源。不能用錄影狀態變成 IDLE 當成 camera 已釋放的證據。工號、SN 錯誤不得阻擋相機停止，這項完整矩陣尚未驗收。

正常停止依序封鎖新工作、排空 callback、錄影 EOS／mux finalize、驗證 recorder／camera NULL，
回傳 worker ACK，再由父程序 wait/reap。日誌可定位：

```text
STOP_REQUESTED → CALLBACK_DRAIN → RECORDER_FINALIZE → PIPELINE_NULL
→ WORKER_ACK → WORKER_REAP → STOP_COMPLETE / RELEASE_FAILED
```

初始化途中也能取消；第二次 Ctrl+C 不會跳過收尾。父程序 graceful wait 上限 22 秒，
必要時 TERM 3 秒、KILL／wait 2 秒；正常停止通常更快。重複 stop 使用同一結果。
runtime 停止協調 deadline 32 秒；網頁 Stop 及設定重啟最多等待 45 秒才視為 HTTP 逾時。

`RELEASE_FAILED` 表示安全釋放無法確認，禁用重新啟動、實體相機拍照及錄影。
程式關閉失敗回傳 exit code 70。錄影 finalize 失敗但相機已安全 NULL，仍會回報失敗，
不能顯示成保存成功；不能把安全釋放與 MP4 完整性混為一談。

正常退出後可用下列只讀指令確認：

```bash
echo "last_exit=$?"
pgrep -af 'visionedge_server|camera_worker'
cat /proc/sys/kernel/random/boot_id
cat runtime_data/edge/camera_recovery_required.json
cat /run/visionedge-camera-0.recovery.json
```

`echo` 必須緊接在前景程式退出後。正常情況沒有 recovery JSON，`cat` 找不到檔案並不代表失敗。
camera ID 不為 0 時替換 `/run` 檔名；lock 檔存在本身不代表有人持鎖。
本機 marker 及 Linux `/run` 共享 guard 防止同 boot 不確定釋放後再取得；不要刪 marker 強行解鎖。
發生強制退出或未確認 NULL 時先保存日誌，必要時依維護程序 reboot，不能宣稱 kill 後相機必已恢復。

完整範圍與待驗收清單見 [相機停止修正紀錄](CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md#11-2026-10-05-實作與驗證紀錄)。
第二階段 PC 回歸為 271 項、267 通過、4 跳過；尚未部署或完成 Qualcomm 實機重複取得驗收。Stop Recording 等待 11 秒後仍未完成會升級停止相機；站台通知未完成時禁止重新啟動，不會假裝資料已保存。

## 13. 部署內容

正式部署應包含：

- 核心 Python 程式
- `config/`
- `templates/`
- 操作頁面所需的 `static/`
- `mediamtx.yml` 與經核准的 service 定義

不要部署：

- `test_assets/`
- `tests/`
- `docs/archive/`
- `__pycache__/`、`.pyc`
- PC 的 `runtime_data/`
- 舊測試資料庫、WAL、SHM 或 lock

TLS private key 必須受控，不應任意散布或放入一般交接壓縮檔。

此次釋放修正需同批更新 `camera_lifecycle.py`（新增）、`camera_worker.py`、`edge_runtime.py`、`overlay_renderer.py`、
`visionedge_server.py`、`apl_station.py`、`static/edge_dashboard.html` 與 `static/apl_ui.js`，避免漏掉新模組或新版父程序配舊 IPC worker。
不覆蓋設備憑證、業務設定或 runtime 資料；不需要新增 service 或套件。

## 14. 上線驗收清單

2026-10-06 開發機 `056325` 已出現 HAL close 時 cam-server `SIGABRT`，不是單純
NULL 等待逾時。主 pipeline EOS／NULL 診斷修正見
[修復紀錄第 13 節](CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md#13-實機失敗定位與主-pipeline-停止修正2026-10-06)。
這次修正尚未部署驗收，不可把 PC 通過當成硬體已修復。
停止階段 log 保存在 `runtime_data/edge/logs/camera_parent.log` 與 `camera_worker.log`，
每檔 2 MiB、3 個備份；查主 pipeline EOS、NULL 回傳值與分支 state，並核對
`journalctl -b -u cam-server.service`。cam-server 重啟不代表 sensor 已恢復。

- [ ] SmartCam 時間正確，重開後不會回到錯誤日期。
- [ ] 相機只有一個 owner。
- [ ] 同 boot 完成 Ctrl+C／重新啟動 30 輪、初始化中取消 10 輪、網頁 Stop／Start 20 輪；每輪驗證新 frame 與 worker reap。
- [ ] HTTPS 頁面可開啟。
- [ ] Live 畫面與判定座標一致。
- [ ] MediaMTX／WHEP 可長時間顯示。
- [ ] CPU 溫度即時更新。
- [ ] 掃碼槍重開機與拔插後都能恢復。
- [ ] 掃碼後不能被第二筆 SN 覆蓋。
- [ ] 正常、無卡、開蓋、遮擋與偏移樣本結果正確。
- [ ] NOT_MATCHED 可重拍且保留每次證據。
- [ ] OK 後只能由上傳成功或終止解除鎖定。
- [ ] 影片測試在正式設定中不可見且 API 被拒絕。
- [ ] 儲存空間不足時不會刪除未送出的證據。
- [ ] SFIS 成功、失敗、逾時與重送皆依現場正式契約驗證。

## 15. 尚未完成

- SFIS Step4 schema 確認與現場網路驗收。
- 40 系 Golden Samples。
- MCIO 插入深度及電性狀態判定。
- 現場不同照明、角度、手部遮擋與長時間準確率測試。
- 從上游 `server.py`／`vision_core.py` 解耦並縮小部署檔案清單。
