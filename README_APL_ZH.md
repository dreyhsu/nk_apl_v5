# NK APL VisionEdge：單張拍照判定

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
- PC 回歸測試共 128 項通過（其中 4 項因缺少參考影片而 skip）。
- SFIS Step1／Step2 呼叫已接入掃碼與檢測完成流程。
- SFIS Step2 預設由 `step2_enabled=false` 禁止送出，須明確開啟才可使用。
- Shopfloor（SFIS）呼叫可由 `dmz.sfis_enabled=false` 完全關閉，供無內網 SmartCam 離線驗機；
  關閉時不會建立 SFIS transport，也不會開任何 socket。
- 40 系樣本與 MCIO 插入深度／電性確認尚未納入。

## 正式操作流程

```text
（未簽入）
  ↓ 輸入工號簽入
WAITING_SN
  ↓ 掃描有效 SN（工號 + SN → SFIS Step1）
  ├─ Step1 回 NO EMP（工號錯誤）→ 清除簽入，需重新輸入工號
  ├─ Step1 其他 FAIL / 連線失敗 → 保留簽入，SN 釋放可重掃
  └─ Step1 接受 ↓
WAITING_CAPTURE
  ↓ Capture & Inspect
JUDGING
  ├─ 不符合 → NOT_MATCHED → 調整後重新拍照
  └─ 符合   → PASS_CONFIRM
                  ├─ SFIS Step2 成功 → COMPLETE → 開放下一筆 SN
                  └─ Terminate Inspection → TERMINATED → 開放下一筆 SN
```

離線模式（`dmz.sfis_enabled=false`）的流程：

```text
WAITING_SN（不需簽入工號）
  ↓ 掃描有效 SN（不呼叫 Step1）
WAITING_CAPTURE
  ↓ Capture & Inspect
JUDGING
  ├─ 不符合 → NOT_MATCHED → 調整後重新拍照
  └─ 符合   → PASS_CONFIRM
                  ├─ Confirm OK & Save Locally → COMPLETE（reported=false）→ 開放下一筆 SN
                  └─ Terminate Inspection → TERMINATED → 開放下一筆 SN
```

規則：

- 必須先輸入工號簽入才能掃碼；未簽入時 SN 欄位停用，實體掃碼槍也會被站台端擋下。
- 工號每班簽入一次，存在站台端；之後可連續掃 SN。要換人時按 `Switch operator` 重新輸入。
- 工號正確與否由 SFIS Step1 判定：回應 `data` 含 `NO EMP` 代表工號錯誤，系統清除簽入並要求重新輸入工號。
- **連線中**的站台，Step2 目前 `step2_enabled=false`（無法過帳）；畫面會提示聯絡網頁開發人員 SMD（Dre Hsu / TW031561），只能 `Terminate Inspection` 結束。離線模式不受此開關影響。
- 掃描有效 SN 後立即開始並鎖定，不需要額外確認 SN。
- 同一輪未結束時，新掃碼不能覆蓋目前 SN。
- 只有按下 `Capture & Inspect` 才執行正式判定。
- `NOT_MATCHED` 可以用同一個 SN 重拍，每次證據都保留。
- 視覺結果 OK 不代表已上傳，也不會自動解除 SN 鎖定。
- 掃碼後先呼叫 SFIS Step1；只有 Step1 接受才可進入拍照判定。
- 只有 SFIS Step2 確認成功或操作員按 `Terminate Inspection` 才解除鎖定；離線模式改由 `Confirm OK & Save Locally` 解除。
- 判定中或上傳中禁止終止、切換來源或開始下一輪。

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

## 離線模式（無 Shopfloor 連線）

沒有接到 Shopfloor 內網的 SmartCam 可以把 SFIS 呼叫整個關掉，設定位於
[config/apl_workflow.json](config/apl_workflow.json) 的 `dmz` 區塊：

```json
{
  "dmz": {
    "sfis_enabled": false
  }
}
```

正式產線必須是 `true`（缺少這個 key 也視為 `true`）。修改後必須重新啟動程式。

`sfis_enabled=false` 與 `mode="disabled"` 是兩件不同的事：

| 設定 | 意義 |
| --- | --- |
| `sfis_enabled: false` | 刻意離線測試。不建立 SFIS transport、不開任何 socket，結果存本地，整條流程跑得完。 |
| `mode: "disabled"` | 設定錯誤保護。仍會建立 transport，但每次 `send()` 都直接丟錯。 |

關閉後的差異：

| 項目 | 連線中 | 離線（`sfis_enabled=false`） |
| --- | --- | --- |
| 工號簽入 | 必填 | 非必填（可自願簽入，仍會記錄） |
| 掃碼後 | 呼叫 Step1，進 `SN_CONFIRM` | 不呼叫，直接進 `WAITING_CAPTURE` |
| 判定 OK 後的按鈕 | `Confirm OK & Send SFIS Step2` | `Confirm OK & Save Locally` |
| `step2_enabled` 影響 | 有 | 無 |
| `last_result.reported` | `true` | 恆為 `false` |
| `last_result.upload_status` | — | `SKIPPED_SFIS_DISABLED` |
| 事件名稱 | `PASS_ACKNOWLEDGED` | `PASS_SAVED_OFFLINE_NO_SFIS` |
| outbox | 成功後 `ACKED` | 完全不寫入（不留 `PENDING`） |
| 原圖／結果圖／History | 保留 | 一樣保留 |

啟動時若開關為關閉，主控台會印出 `[VisionEdge] WARNING: SFIS OFFLINE ...`；
網頁的 SFIS 狀態列會常駐顯示
`SFIS off by configuration — inspections are saved on this station only`（琥珀色）。
`/api/edge/status` 的 `features.sfis_enabled` 也可供監控判讀。

[scripts/sfis_api_test.py](scripts/sfis_api_test.py) 不受這個開關影響，仍會直連正式 SFIS。

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
- SFIS 內網連線與 Step2 request/response 契約仍須在現場做正式驗收。
- 離線模式（`sfis_enabled=false`）完成的 PASS 不會進入 outbox，也沒有自動補送 SFIS 的機制；
  需要回補只能依 `cycles`／`events`／`attempts` 紀錄人工處理。
- `server.py` 與 `vision_core.py` 是上游 VisionEdge 相容依賴，目前仍不能移除。
