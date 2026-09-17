# NK APL VisionEdge：單張拍照判定

## 專案定位

本資料夾是 NK APL 產線的單張判定開發版。操作員掃描 SN 後，系統鎖定該輪，
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
- PC 回歸測試共 92 項通過。
- SIFI 正式 endpoint、認證、payload 與成功回覆契約尚未提供。
- 40 系樣本與 MCIO 插入深度／電性確認尚未納入。

## 正式操作流程

```text
WAITING_SN
  ↓ 掃描有效 SN
WAITING_CAPTURE
  ↓ Capture & Inspect
JUDGING
  ├─ 不符合 → NOT_MATCHED → 調整後重新拍照
  └─ 符合   → PASS_CONFIRM
                  ├─ SIFI 成功 → COMPLETE → 開放下一筆 SN
                  └─ Terminate Inspection → TERMINATED → 開放下一筆 SN
```

規則：

- 掃描有效 SN 後立即開始並鎖定，不需要額外確認 SN。
- 同一輪未結束時，新掃碼不能覆蓋目前 SN。
- 只有按下 `Capture & Inspect` 才執行正式判定。
- `NOT_MATCHED` 可以用同一個 SN 重拍，每次證據都保留。
- 視覺結果 OK 不代表已上傳，也不會自動解除 SN 鎖定。
- 只有 SIFI 確認成功或操作員按 `Terminate Inspection` 才解除鎖定。
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
HTTP 403。影片測試結果禁止上傳 SIFI。

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
- SIFI 尚未配置，目前 `Confirm PASS & Upload to SIFI` 不會形成正式上傳成功。
- `server.py` 與 `vision_core.py` 是上游 VisionEdge 相容依賴，目前仍不能移除。

