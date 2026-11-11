# NK APL VisionEdge：單張拍照判定

2026-11-09 本機更新：Capture 比對、證據寫入與持續取像已分離；自動檢驗由單一背景工作處理，保留 5 FPS 上限、2 次命中及 20 秒 NOT_MATCHED 規則。SFIS／DMZ 與 HTTPS 設定沿用此版。完整差異、診斷日誌與板端待驗收項目見[Capture／直播實作紀錄](docs/APL_CAPTURE_STREAM_ISOLATION_IMPLEMENTATION_20261109_ZH.md)。以下歷史操作說明須搭配實際設定；PC 通過不代表板端驗收。

## 專案定位

本資料夾是 NK APL 產線的單張判定開發版。操作員上班先輸入工號簽入，之後掃描
SN，系統以「工號 + SN」呼叫 SFIS Step1 驗證組合、鎖定該輪，
等待操作員按下 `Capture & Inspect`，再以同一張影像確認：

1. 治具有蓋板且處於關閉狀態。
2. MCIO 小卡已出現在指定位置。

本版不是四步／五步連續流程，也不是 SmartCam v2.4 `.app`。部署方式為直接執行
Python 程式。

## 目前狀態

- 單張 Template Matching 判定已完成。
- Honeywell 掃碼槍、SN 鎖定、終止與重新掃碼流程已完成。
- Browser Video Test、時間軸、暫停與指定畫面判定已完成。
- 原圖、結果圖、JSON、SN、時間與 History 查詢已完成。
- 影片測試可由設定完全關閉。
- PC 回歸測試已涵蓋工號前置、掃碼槍選擇、SFIS bypass 與原有 Step1／Step4 契約；實體 SmartCam 尚需現場驗收。
- SFIS Step1／Step4 呼叫已接入掃碼與檢測完成流程。
- SFIS Step4 預設由 `step4_enabled=false` 禁止送出，須明確開啟才可使用。
- 40 系樣本與 MCIO 插入深度／電性確認尚未納入。

## 正式操作流程

```text
（未簽入）
  ↓ 輸入工號簽入
WAITING_SN
  ↓ 掃描產品 SN（必須 K2V5 開頭）→ 工號 + 產品 SN → SFIS Step1
SN_CONFIRM（送出 Step1）
  ├─ Step1 回 NO EMP（工號錯誤）→ 不清除任何東西，提示按 Switch operator 改工號後重掃同一張產品 SN
  ├─ Step1 其他 FAIL / 連線失敗 → 保留產品 SN 與簽入，重掃同一張產品 SN 即重送 Step1，或 Terminate
  └─ Step1 接受 ↓
WAITING_MCIO_SN
  ↓ 掃描 MCIO 卡 SN（必須 APL 開頭；不再呼叫 SFIS）
WAITING_CAPTURE
  ↓ Capture & Inspect
JUDGING
  ├─ 不符合 → NOT_MATCHED → 調整後重新拍照
  └─ 符合   → PASS_CONFIRM
                  ├─ SFIS Step4 成功 → COMPLETE → 開放下一筆 SN
                  └─ Terminate Inspection → TERMINATED → 開放下一筆 SN
```

規則：

- 必須先輸入工號簽入才能掃碼；未簽入時 SN 欄位停用，實體掃碼槍也會被站台端擋下。
- 工號每班簽入一次，存在站台端；之後可連續掃 SN。要換人時按 `Switch operator` 重新輸入。
- `--demo` 與 Browser Video Test 也須先簽入工號才能掃 SN；測試不會呼叫 SFIS。切換隔離的影片測試站台後，若顯示未簽入，需在該站台再輸入工號。
- 掃碼槍狀態下方有預設收合的 `Find or change scanner`；新裝置首次選擇後，成功連接過的掃碼槍會保存在 `runtime_data/barcode/scanner_selection.json`，拔插、重啟及已知裝置間更換會自動接回。只有新裝置或辨識不明才需要重新尋找。詳見 [工號與掃碼槍操作](docs/SCANNER_OPERATOR_FLOW_ZH.md)。
- 工號正確與否由 SFIS Step1 判定：回應 `data` 含 `NO EMP` 代表工號錯誤。系統**不清除任何東西**，只提示操作員按 Switch operator 改用正確工號，再重掃同一張產品 SN 重試。
- Step4 目前 `step4_enabled=false`（無法過帳）；畫面會提示聯絡網頁開發人員 SMD（Dre Hsu / TW031561），只能 `Terminate Inspection` 結束。
- 掃描有效 SN 後立即開始並鎖定，不需要額外確認 SN。
- 同一輪未結束時，新掃碼不能覆蓋目前 SN。
- 只有按下 `Capture & Inspect` 才執行正式判定。
- `NOT_MATCHED` 可以用同一個 SN 重拍，每次證據都保留。
- 視覺結果 OK 不代表已上傳，也不會自動解除 SN 鎖定。
- 掃完產品 SN 立即呼叫 SFIS Step1；只有 Step1 接受才進入掃 MCIO 卡與拍照判定。Step1 失敗會保留產品 SN，重掃同一張即重試。
- 只有 SFIS Step4 確認成功或操作員按 `Terminate Inspection` 才解除鎖定。
- 判定中或上傳中禁止終止、切換來源或開始下一輪。

### SFIS 測試／正式切換

目前版控中的 `config/apl_workflow.json` 設為 `dmz.bypass_sfis=true`，僅供工程測試：站台會在本地接受 Step1／Step4，**不會呼叫 SFIS**；畫面也會標示 BYPASS。正式串接前須明確改為 `false`，並依 SFIS 規格與現場驗收結果決定是否開啟 `step4_enabled`。`APL_DMZ_BYPASS=on/off` 可覆寫設定，值不明確時程式拒絕啟動。

舊設定鍵 `dmz.sfis_enabled` 已停用；若仍存在，程式會拒絕啟動，避免原本的離線設定在新版被忽略後誤送正式 SFIS。請先備份設定並明確換成 `bypass_sfis`，不可只更新 Python 檔而保留舊 JSON。

獨立的 `scripts/sfis_api_test.py` 是實際 HTTP API 測試工具，會在該次程序關閉設定檔的 bypass，預設僅送 Step1；只有加上 `--step4-test` 才送 Step4。若環境變數仍強制 bypass，工具直接報錯，不會顯示假成功。此工具可能真的更新 SFIS，僅在取得現場測試授權後使用。

## 判定條件

設定來源：[config/apl_workflow.json](config/apl_workflow.json)

| 項目 | 目前設定 |
|---|---|
| 模式 | `single_snapshot_cover_card` |
| 目標類別 | `50_COVER_CLOSED_CARD_PRESENT` |
| 參考尺寸 | `1920 × 1080` |
| ROI | `751, 254, 431, 588` |
| 方法 | `TM_CCOEFF_NORMED` |
| 門檻 | `0.8` |
| 最小差距 | `0.05` |
| 反例 | `50_COVER_CLOSED`、`50_COVER_OPEN_CARD_PRESENT` |

同一張影像必須同時支持「蓋板關閉」與「MCIO 小卡存在」。尺寸不符時直接拒絕，
不會自動縮放後判定。

## 影片測試

影片測試只供開發，設定位於 [config/edge.ini](config/edge.ini)：

```ini
[features]
video_test_enabled = true
```

正式產線應改為：

```ini
[features]
video_test_enabled = false
```

修改後必須重新啟動程式。關閉時，網頁隱藏整個影片工具列，影片測試 API 回覆
HTTP 403。影片測試結果禁止上傳 SFIS。

開發影片保存在 `test_assets/videos/`，不屬於 Golden Samples 或產線證據。

## 啟動與測試

SmartCam 直接啟動：

```bash
cd /var/rootdirs/opt/VisionEdge_APL
/usr/bin/python3 -B -u visionedge_server.py
```

預設頁面：

```text
https://<SmartCam-IP>:8765/edge
```

PC Browser Video Test：

```powershell
python visionedge_server.py --demo --no-tls --port 8772
```

開啟 `http://127.0.0.1:8772/edge`，再於頁面選擇本機 MP4。

完整 PC 回歸測試：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

PC 無法驗證 Qualcomm QTI 相機、GStreamer、MediaMTX、HDMI 與實體掃碼槍，
這些項目必須在 SmartCam 上驗收。

## 資料位置

- 工站狀態：`runtime_data/apl/station.sqlite3`
- 工站歷史資料表、升級方式與 dashboard 查詢：[STATION_HISTORY.md](docs/STATION_HISTORY.md)
- 判定證據：`runtime_data/apl/evidence/YYYY-MM-DD/`
- Golden Samples：`templates/`
- 開發影片：`test_assets/videos/`
- 舊文件與舊程式備份：`docs/archive/`

每次拍照保存原圖、疊圖結果、JSON 與資料庫索引。正式資料有未完成或未確認上傳
狀態時，不可直接刪除資料庫或 evidence。

## 文件閱讀順序

1. [README.md](README.md)：快速入口。
2. 本文件：目前有效規格與操作。
3. [docs/HANDOVER_ZH.md](docs/HANDOVER_ZH.md)：完整交接、部署與驗收。
4. [docs/QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md](docs/QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md)：相機與串流故障排除。
5. `docs/archive/`：歷史紀錄，只供追溯。

## 已知限制

- Template Matching 依賴固定視角、固定 ROI、解析度與照明。
- 現有樣本數量不足以直接宣告量產辨識率。
- 尚未判斷 MCIO 是否完整插到底或電性正常。
- SFIS 內網連線與 Step4 request/response 契約仍須在現場做正式驗收。
- `server.py` 與 `vision_core.py` 是上游 VisionEdge 相容依賴，目前仍不能移除。
