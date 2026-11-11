# APL 1109｜Capture 與持續取像分離：實作及驗證紀錄

日期：2026-11-09。來源為 `NK_APL/APL_1109/nk_apl_v5`，基底 `main@199972c824cea33d37b75d11291b33af242dc92c` 的本機修改。本文記錄已完成的原始碼與 PC 驗證；尚未部署 SmartCam，沒有宣稱產線、Qualcomm 編碼器或 WebRTC 實機驗收通過。

設計依據見[修改計畫](plan/APL_CAPTURE_STREAM_ISOLATION_PLAN_20261109_ZH.md)。原始版本差異見[修改前版本盤點](APL_1109_VERSION_REVIEW_20261109_ZH.md)。

## 1. 修正的原因與處理範圍

原版手動 Capture 與預覽共用 `frame_lock`，存證又持有站點鎖；自動模板比對同步跑在取像迴圈。這些是原碼與 PC 阻塞實驗已確認的等待點。Capture 變慢會使 `frame_seq`、預覽與疊字更新停頓，可能觸發既有網頁的新鮮度判斷；它們不等於已證明 MediaMTX process 崩潰。

本次改的是相機取像之後的 Python 排程、結果發布及存證。沿用 QTI 的單一相機、既有 GStreamer tee／JPEG 分支／共享 H.264 編碼器，以及 RTP → MediaMTX → WebRTC 路徑。沒有調大 MediaMTX buffer、改 port、改畫面旋轉、降低正式門檻或移除 Golden Sample，也沒有修改網頁顯示邏輯。

## 2. 現在如何執行

```text
QTI 相機 ──→ 原有疊圖／H.264／RTP ──→ MediaMTX ──→ WebRTC
    └──→ JPEG／APL 取像 ──→ 發布最新影格、序號、來源時間、相機世代
                   ├──→ 不等待檢驗鎖的預覽與疊字狀態
                   ├──→ 手動 Capture 固定一張 → 比對 → 準備證據
                   └──→ 單一背景自動比對 → 命中／逾時 → 沿用同張結果
                                                               ↓
                                           同一交易寫 attempts／cycle／event
                                                               ↓
                                              發布正式結果 → 原有 Step4 流程
```

- **取像**：收到影像就發布 raw 與序號。結果 JPEG 使用獨立發布序號，避免把新 raw 序號配上舊 result。預覽嘗試讀站點狀態，鎖忙時使用前次完整快照；預覽路徑不再每張查 SQLite 的 attempts 數量。
- **手動 Capture**：同一段鎖內固定影像、來源時間、序號與相機世代，拒絕超過 3 秒的相機影像。QTI 使用 worker 隨影像帶回的單調時間。已受理照片不會被下一張取代。檔案回放仍使用使用者選取的影格。
- **HTTP**：live Capture 只在受理時持有站點命令鎖；比對及寫圖期間讓狀態 API 可回應。多個瀏覽器同時 Capture，第二個立即回覆 409，不排隊成為下一次檢驗。瀏覽器影片模式維持既有輸入保護。
- **自動檢驗**：同時最多一個背景工作。忙碌期間收到的新畫面繼續提供預覽，不累積比對工作；空閒後取最新一張。每個影格序號最多增加一次連續命中。自動命中的正式提交直接沿用該次比對結果，免除第二次完整比對。
- **5 FPS 與 20 秒**：5 FPS 是探測上限；例如一次需 400 ms，實際探測頻率會下降，不補做積欠工作。從可檢驗狀態開始計時，以比對完成時判定是否已逾時。19.99 秒開始、20 秒後才完成的命中，按 `not_matched` 處理。20 秒是接受命中的時間窗，並非保證圖檔與 DB 在第 20.000 秒已完成；最後一張計算及 I/O 耗時會記錄。
- **存證與 PASS**：原圖、結果圖、JSON 的編碼與寫入移出站點鎖及 DB 交易。檔案先以暫存檔、fsync、rename 寫妥，再於同一 SQLite 交易寫 attempts、cycle 和成功事件。交易未成功時，外部不能看到可送 Step4 的 PASS。
- **回復資料**：以完整 attempt ID 關聯三個檔案及受理事件。失敗只清除該次未提交檔案；重新開啟站點時也依受理事件清除中斷的未提交證據，不掃除歷史有效證據。schema 仍為 2；檔名末端 attempt ID 由 12 碼改為完整 32 碼，History 使用 DB 路徑。
- **停止／切換**：以工件、站點世代、相機世代及 attempt 數量拒絕晚到工作。停止或關閉期間不允許尚未完成的檢驗變成 PASS；活著的 writer 不提前關 DB。背景收尾使用既有 Stop 剩餘期限，未結束的檢驗擋住重開。保持既有 QTI EOS／NULL／復原規則。
- **ROI**：先裁搜尋區再轉灰階；全圖座標、7 類共 20 張樣本、門檻及反例比較規則保留。

正式 SFIS／DMZ 的送收、request ID、Step4 與 HTTPS 設定檔未改動。本次沿用 `auto_capture.enabled=true`、`fps=5`、`required_hits=2`、`timeout_sec=20`、`timeout_action=not_matched`、`step4_enabled=true`、`bypass_sfis=false`。PATLITE、19／12 碼序號及相機硬體釋放程式保持既有功能。

## 3. 修改檔案

| 檔案 | 修改內容 |
|---|---|
| `edge_runtime.py` | raw/result 發布分離、固定拍照身分、單一背景探測、停止保護、探測摘要 |
| `apl_station.py` | 不阻塞預覽的狀態快照、正式檢驗分階段、原子提交、取消及 writer 關閉保護 |
| `inspection_store.py` | 準備圖檔與插入 DB 分開、原子檔案寫入、精確清理未提交證據 |
| `visionedge_server.py` | Capture 防重入、live Capture 縮小命令鎖範圍、shutdown 檢查檢驗工作 |
| `apl_matching.py` | 搜尋 ROI 先裁圖再轉灰階 |
| `tests/test_snapshot_inspection.py`、`tests/test_runtime_shutdown.py` | 並行、逾時、取消、DB／磁碟錯誤、停止期限及 HTTP 回歸 |
| `tests/test_matching.py`、`tests/test_video_temperature.py` | 修正原版已存在的測試預期：實際模板為 20 張，Browser Test 在 bypass=false 時應禁用 SFIS |

## 4. 可看的診斷資訊

沿用 `camera.lifecycle` 日誌。QTI parent 啟動後，寫至 `runtime_data/edge/logs/camera_parent.log`，沿用每檔 2 MiB、3 份輪替；終端也會輸出。單獨做純 PC 測試時是否有 INFO 輸出取決於入口的 logging 設定。

| 紀錄 | 判讀 |
|---|---|
| `AUTO_CAPTURE` | 約每 5 秒及命中／逾時時記錄工件、影格序號、elapsed、probe 次數、busy_frames、最近比對毫秒及觸發原因 |
| `CAPTURE_COMMITTED` | attempt、影格序號、match_ms、evidence_ms、commit_ms；自動檢驗的 match_ms 沿用原探測耗時 |
| `CAPTURE_FAILED` | 失敗發生於 matching、evidence 或 commit；原始錯誤保留 |
| `CAPTURE_CANCELLED` | 取消後未發布結果的 attempt 與影格序號 |
| `/api/edge/status` 的 `auto_capture` | 新增 `busy_frames`、`last_probe_ms`、`probe_count`、`actual_probe_fps`；計數以當前探測窗口為範圍 |

`busy_frames` 是探測忙碌時略過的候選次數，不是相機或 RTP 丟包數。`actual_probe_fps` 是窗口中完成探測次數除以 elapsed，不是編碼器 FPS。Evidence JSON 的 source 保留 `captured_from_frame_seq`、`camera_generation`；自動檢驗另有 `probe_ms`、trigger、hits、elapsed。

以下為 2026-11-09 PC 故障注入測試的實際紀錄節錄。6062 ms 是測試刻意暫停，不是 SmartCam 性能測量：

```text
CAPTURE_COMMITTED attempt=ed7ed3ec05be4ced8838fd8cd107b5b5 seq=1 match_ms=6062.0 evidence_ms=32.0 commit_ms=0.0
CAPTURE_COMMITTED attempt=b97e19be07a342f8bc4871d60cb067f6 seq=583 match_ms=0.0 evidence_ms=63.0 commit_ms=0.0
CAPTURE_COMMITTED attempt=a3abace2cc6e404295a14929127cda36 seq=590 match_ms=0.0 evidence_ms=15.0 commit_ms=47.0
```

## 5. PC 驗證結果

環境：Windows，`C:/Users/NUKO/miniforge3/envs/torch_sister/python.exe`，OpenCV 4.11.0、NumPy 1.26.3。使用原有環境，未安裝／升級套件。

```powershell
cd D:\work_project\smartcam\NK_APL\APL_1109\nk_apl_v5
& C:\Users\NUKO\miniforge3\envs\torch_sister\python.exe -B -m unittest discover -s tests -q
```

- 完整回歸：370 項，366 通過、4 項因外部參考影片缺失跳過，0 失敗，約 26 秒。
- 真正的 `EdgeRuntime._run()` 搭配替身相機：比對刻意阻塞超過 6 秒，取像持續增加超過 60 張；另獨立阻塞準備圖檔與 DB 插入，放行前仍能發布新的 raw/result。
- 存下的 raw 圖像內容與捕獲時的 `captured_from_frame_seq` 相符，後續新畫面未取代已受理照片。
- 存證阻塞期間，Flask 真正路由可回傳最新 JUDGING 狀態；並行第二次 Capture 回覆 409，資料庫只有一次 attempt。
- 自動工作忙碌時不堆積；同張不重算命中；手動與自動相遇不重複提交。20 秒邊界、相機世代更換、停止中寫 DB、close 中寫證據、日誌提交失敗及中斷回復均有回歸。
- ROI 修改前後以 20 張 Golden Sample 嵌入 1080p 畫布，加 `NK_APL/1102.jpg`，共 21 張比對，完整判定字典相同。PC 耗時不能代替 SmartCam 每張耗時。
- 既有 SFIS／DMZ、PATLITE、瀏覽器影片、檔案回放、錄影、釋放與重開測試包含於完整回歸；使用替身服務，沒有連接正式 SFIS。

## 6. 板端待驗收及剩餘界線

這次已解除確認到的 Python 鎖與同步計算等待，但 CPU／記憶體頻寬仍由同機工作共用。GStreamer 的編碼後可丟棄 queue 沿用原版；改成阻塞可能影響共享錄影分支，尚未調整。

下一輪依計畫第 4 節，在同一開發板、相同模板與 UI 做 Capture／20 秒逾時／兩個接收端／錄影／反覆 Stop-Start。同步比對取像序號、loopback RTP 時間戳、MediaMTX path/session、接收端解碼影格。若仍閃爍，可從這些時間線區分 Python 更新停頓、實際 RTP 中斷、接收端解碼或頁面狀態切換；本次 PC 結果不能證明這些硬體及網路段已通過。

未執行 Git commit／push、板端檔案覆蓋或服務重啟。更新目標仍是上述 APL_1109 資料夾。
