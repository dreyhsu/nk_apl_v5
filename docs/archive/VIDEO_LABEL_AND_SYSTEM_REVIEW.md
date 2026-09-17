# Video Label 與整體系統巡檢 — v1.4

## 判定

使用者沒有操作錯誤。v1.3 `video-label` 的確存在播放顯示 bug。

原本流程是：

1. 隱藏的 `<video>` 在背景播放。
2. 中央畫面顯示的是 `mainCanvas`。
3. Canvas 只在 `seeked` 且影片暫停，或按下 `Capture Frame` 時更新。

因此按下播放後，`currentTime` 與 Seek Bar 會前進，但中央 Canvas 維持上一張靜態影像，看起來就是「播放毫無動靜」。

## 已修正問題

### 1. 播放畫面完全不動 — Critical

播放期間改為直接顯示真正的 HTML5 Video，Overlay Canvas 保留在上層；暫停後再切回擷取 Canvas。

### 2. 載入後可能沒有第一張畫面 — High

原版只監聽 `loadedmetadata`，但 metadata 可用不代表第一幀已可解碼；且把 `currentTime` 設為 0 不一定觸發 `seeked`。

新版在 `loadeddata` 後自動擷取第一幀。

### 3. 新影片可能誤存上一部影片的舊幀 — High

原版載入新影片時未清除 `S.srcB64` 與舊 Canvas。若新影片尚未成功解碼就按 Save，有機會把上一張參考畫面保存到新標註。

新版載入新影片時立即清除舊幀狀態，直到新幀成功取得前禁止保存。

### 4. 播放中 Save 可能不是當前畫面 — High

原版 `srcB64` 只在手動 Capture／暫停時更新。播放到新位置後直接 Save，仍可能保存上一個 Capture 的畫面。

新版 Save 前會先暫停並擷取當前幀。

### 5. 不支援的 codec 無提示或長時間沒有反應 — High

瀏覽器可播放的格式集合與 OpenCV 不完全相同。AVI、部分 MKV、特殊 H.265／廠商 codec 可能只有 OpenCV 能讀。

新版：

- 顯示 Browser MediaError。
- 4.5 秒內沒有取得第一幀時，自動切換 Server-Frame 相容模式。
- 後端用 OpenCV 上傳、Seek 與擷取畫面。

### 6. 播放失敗後按鈕狀態不可信 — Medium

原版未處理 `video.play()` Promise reject，按鈕可能顯示暫停，但影片實際未播放。

新版會等待 `play()`，失敗時恢復按鈕並顯示錯誤。

### 7. Offline Token Cleanup 競態 — High

原版刪除舊 token 失敗時，用全域 `token` 重新排程。若此時已上傳新影片，重試可能刪除正在推論的新 token。

新版重試固定綁定原 token，不會指向後續新 token。

### 8. 從影片中段開始會錯誤觸發 Timeout — High

原版虛擬時間直接使用影片的絕對秒數。例如從 60 秒開始推論，第一幀就可能被視為已經過 60 秒。

新版把本次開始位置視為 elapsed 0。

### 9. 無 SOP 產品固定顯示 INCOMPLETE — Medium

原版 Done Handler 只讀 `sop.complete`；沒有 SOP 時 `sop=null`，因此即使單幀規則通過也顯示 INCOMPLETE。

新版 Done Payload 提供獨立 `pass` 與 `verdict`。

### 10. Runtime API 洩漏 RTSP 密碼 — High

原版雖新增 `source_uri_masked`，但 `config.source_uri` 仍保留原始 URI，因此 API JSON 仍能看到帳密。

新版 Runtime Status 的兩個 URI 欄位均為遮蔽值。

### 11. 影片 metadata 與 frame seek 防護不足 — Medium

已增加：

- NaN／0 FPS fallback。
- 非數值時間與 product id 驗證。
- Seek time 與 frame index 邊界限制。
- `infer_fps` 上限。
- 長 GOP 影片 sequential grab，降低反覆 random seek 成本。

## 實際測試

已執行：

- Python compile。
- 所有 HTML Inline JavaScript `node --check`。
- 原有 Smoke Test。
- Flow Designer Test。
- Live Flow Graph Test。
- Headless Chromium Video Label E2E：
  - 產生 640×360、4 秒的移動 WebM。
  - 載入後自動顯示第一幀。
  - Play 後真實 Video 顯示，Canvas 隱藏。
  - 約 1.2 秒後 `currentTime > 1 秒`。
  - Pause 後 Video 隱藏、Canvas 顯示。
  - 當前幀已轉成完整 `srcB64`。

## 仍需注意的非程式錯誤邊界

### A. Live SOP 需要明確的開始／結束訊號

Live Stream 無法只靠畫面判斷「這一台產品現在正式開始」或「操作員已經結束但漏做全部步驟」。目前可以：

- 第一個視覺 Evidence 出現後啟動 Step Timeout。
- 使用 Finish Gate 判斷漏步。
- VIDEO EOF 自動 Finish。

正式產線最好以 SN Scan、PLC DI、MES Start／End 或人工 Start／Finish 作為 Session 邊界。

### B. 管理 API 尚無完整驗證

目前存在簡易登入畫面，但產品、SOP、串流設定 API 尚未套用完整 Session／Role 驗證。受控網段 POC 可用；正式部署需要 Nginx SSO、Keycloak 或後端權限中介層。

### C. 多台相機仍為獨立 Session

Multi-Stream 是多個獨立 Worker，不是多台相機共同完成同一工件 Flow。跨鏡頭共用 Session 仍需 Camera Group、Workpiece ID 與 Event Aggregator。

### D. Server-Frame 相容模式不是高 FPS 播放器

每次 Seek 由 OpenCV 擷取指定畫面，適合標註和檢查；大量高解析度影片的流暢播放仍建議轉成 MP4/H.264 或 WebM。

### E. 尚需現場壓力測試

仍需驗證：

- 實際手機錄影 codec。
- Windows／Linux OpenCV FFmpeg build 差異。
- 長影片 Seek 效率。
- 10 路以上 RTSP CPU／RAM／網路負載。
- RTSP 斷線與帳密過期。
