# VisionEdge QTI 相機與 MediaMTX 故障排除

## 1. 文件目的

本文件記錄 NK APL 單張拍照判定版 VisionEdge 在 SmartCam 上發生的兩個獨立問題：

1. GStreamer 無法找到 `qtiqmmfsrc`，導致 QTI 相機無法啟動。
2. QTI 相機恢復後，WHEP 請求仍回傳 HTTP `502`，導致網頁串流無法顯示。

適用的開發來源：

```text
D:\work_project\smartcam\NK_APL\VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP
```

本次實測的 SmartCam：

```text
Hostname: usi-smartcam-056325
SSH:      root@169.254.130.130
程式:     /var/rootdirs/opt/VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP
網頁:     https://169.254.130.130:8765/edge
```

不要把登入密碼寫進本文件或程式碼。

## 2. 問題一：QTI 相機外掛顯示 unavailable

### 2.1 畫面與程式錯誤

網頁會顯示：

```text
Camera ERROR
Camera is stopped
QTI backend requested but qtiqmmfsrc is unavailable
```

程式在 `edge_runtime.py` 透過下列判斷確認 QTI 外掛：

```python
Gst.ElementFactory.find("qtiqmmfsrc")
```

當 GStreamer registry 將此外掛列入黑名單時，即使 `.so` 檔案存在，上述查詢仍會
得到 `None`，VisionEdge 因而拒絕啟動 QTI Camera Backend。

### 2.2 2026-09-14 實測證據

外掛檔案存在：

```text
/usr/lib/gstreamer-1.0/libgstqtiqmmfsrc.so
```

但預設 registry 查詢失敗：

```bash
gst-inspect-1.0 qtiqmmfsrc
```

輸出：

```text
No such element or plugin 'qtiqmmfsrc'
```

黑名單中可看到：

```bash
gst-inspect-1.0 -b
```

```text
libgstqtiqmmfsrc.so
```

同一個 `.so` 以直接路徑或全新的暫存 registry 載入時可以成功，證明外掛並未缺失：

```bash
gst-inspect-1.0 /usr/lib/gstreamer-1.0/libgstqtiqmmfsrc.so
```

實測時系統 UTC 時間是 `2026-06-26`，但既有 registry 的時間是
`2026-09-10`。系統時間曾往回調，舊 registry 的未來時間戳與其中保存的黑名單
狀態，使 GStreamer 沒有正常重新掃描此外掛。

### 2.3 修復前檢查

先以 SSH 登入 SmartCam，確認目前執行的 VisionEdge 及工作目錄：

```bash
pgrep -af visionedge_server.py
for p in $(pgrep -f 'python3 -B -u visionedge_server.py'); do
    echo "PID=$p CWD=$(readlink -f /proc/$p/cwd)"
done
```

確認 `CWD` 必須是本次要處理的版本：

```text
/var/rootdirs/opt/VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP
```

如果工作目錄不同，先停止並確認，不可直接刪除其他版本的資料或終止其他應用。

檢查外掛、黑名單、registry 時間及系統時間：

```bash
ls -l /usr/lib/gstreamer-1.0/libgstqtiqmmfsrc.so
gst-inspect-1.0 qtiqmmfsrc
gst-inspect-1.0 -b
ls -l /root/.cache/gstreamer-1.0/registry.aarch64.bin
date -u
```

### 2.4 修復步驟

以下操作會短暫停止 VisionEdge。不要在拍照判定、錄影或上傳過程中執行。

先對確認過的 PID 發送 `SIGTERM`，讓相機管線正常釋放：

```bash
kill -TERM <VisionEdge_PID>
```

確認程序已離開：

```bash
pgrep -af visionedge_server.py
```

只刪除可自動重建的 GStreamer registry，不刪除程式碼、Golden Sample、
`runtime_data`、SQLite 或判定證據：

```bash
rm -f /root/.cache/gstreamer-1.0/registry.aarch64.bin
```

重新掃描並建立 registry：

```bash
gst-inspect-1.0 qtiqmmfsrc
```

成功時應看到：

```text
Name                     qtiqmmfsrc
Filename                 /usr/lib/gstreamer-1.0/libgstqtiqmmfsrc.so
```

再次檢查黑名單；`libgstqtiqmmfsrc.so` 不應再出現：

```bash
gst-inspect-1.0 -b
```

### 2.5 重新啟動 VisionEdge

前景測試方式便於直接看 log，停止時按 `Ctrl+C`：

```bash
cd /var/rootdirs/opt/VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP
/usr/bin/python3 -B -u visionedge_server.py
```

需要退出 SSH 後仍持續執行時：

```bash
cd /var/rootdirs/opt/VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP
nohup /usr/bin/python3 -B -u visionedge_server.py >/tmp/visionedge_console.log 2>&1 &
```

查看 log：

```bash
tail -n 100 /tmp/visionedge_console.log
```

確認相機狀態：

```bash
curl -k -s https://127.0.0.1:8765/api/edge/status | python3 -m json.tool
```

至少確認：

```text
backend = qti
status = LIVE
running = true
backend_status.worker_alive = true
has_raw = true
frame_seq 持續增加
```

2026-09-14 修復後實測：QTI backend 與 Camera Worker 均恢復，影像及推論約為
6 FPS；稍後網頁顯示 Camera Live 約 10 FPS。掃碼槍仍連接於
`/dev/input/event6 (Honeywell 1900-C)`。

## 3. 問題二：WHEP 回傳 HTTP 502

QTI 相機恢復後，VisionEdge log 可能仍出現：

```text
POST /api/stream/whep HTTP/1.1 502
```

這與 `qtiqmmfsrc` 是不同問題：

- `qtiqmmfsrc unavailable`：相機來源無法建立。
- WHEP `502`：VisionEdge 已收到瀏覽器請求，但無法連到 MediaMTX。

實測時 `mediamtx.service` 為 `inactive (dead)`，因此 WHEP proxy 回傳 `502`。

### 3.1 啟動前避免服務衝突

先確認 v2.4 `aicamera` 及其內嵌 MediaMTX worker 沒有執行：

```bash
systemctl is-active sc-app-aicamera
systemctl is-active sc-app-aicamera-w0
pgrep -af mediamtx
```

只有在 `sc-app-aicamera` 與 `sc-app-aicamera-w0` 都不是 active，且沒有另一個
MediaMTX 程序佔用相同連接埠時，才可啟動 standalone `mediamtx.service`。

### 3.2 啟動與檢查 MediaMTX

立即啟動：

```bash
systemctl start mediamtx
systemctl status mediamtx --no-pager -l
```

查看近期 log：

```bash
journalctl -u mediamtx -n 100 --no-pager
```

確認連接埠：

```bash
netstat -lntup | grep -E '8888|8889|8189|5004'
```

若確認本機部署模式需要每次開機自動啟動：

```bash
systemctl enable mediamtx
```

之後若改用正式 v2.4 `aicamera` APP，必須停止並停用 standalone 服務：

```bash
systemctl disable --now mediamtx
```

不可同時執行 standalone `mediamtx.service` 與 app-owned MediaMTX，兩者會競爭
8888、8889、8189 與 5004 等連接埠。

## 4. 完整驗證清單

修復及啟動後重新整理：

```text
https://169.254.130.130:8765/edge
```

確認以下項目：

- 頂部 Camera 顯示 `LIVE`，不再顯示 `qtiqmmfsrc is unavailable`。
- Live Camera 畫面持續更新，不是停留在舊幀。
- `/api/edge/status` 的 `frame_seq` 持續增加。
- AI FPS 有實際數字，Template Matching 可執行。
- `/api/stream/whep` 不再回傳 `502`。
- 掃碼槍顯示 `Scanner connected`，掃碼後 SN 被鎖定至本輪。
- `Capture & Inspect` 只判定當下擷取的影像。
- CPU 溫度可讀取且維持於設備可接受範圍。

## 5. 可忽略與不可忽略的訊息

未接 HDMI／桌面顯示環境時，下列訊息通常是無害警告，不能單獨視為相機啟動失敗：

```text
MESA-LOADER: failed to retrieve device information
Failed to get Wayland EGL display
Failed to initialize X11 EGL display
VendorTagDescriptor ... does not exist
```

必須處理的訊息包括：

```text
QTI backend requested but qtiqmmfsrc is unavailable
QTI camera failed to start
POST /api/stream/whep ... 502
Camera is stopped
worker_alive = false
```

判斷時以 `/api/edge/status`、程序狀態、GStreamer 外掛查詢與 MediaMTX service
狀態交叉確認，不要只看單一 EGL 或 VendorTag log。

## 6. 避免復發

- 新版 recovery 標記會記錄 Linux `boot_id`。同一次開機內若 QTI 清理失敗，
  相機仍保持封鎖；完成重新開機後，程式確認 `boot_id` 已改變，便會自動將舊標記
  移到 `runtime_data/edge/recovery_archive/`，不會永久阻止相機啟動。
- 舊版、不完整或無法解析的 recovery 標記不會自動略過，必須先檢查並做一次性封存；
  這可避免在無法證明已重新開機時，同一個 QMMF 故障狀態被危險地重新取得。
- Recovery 判定不依賴系統日期；SmartCam 時鐘回調不會影響重新開機辨識。
- 啟動 VisionEdge 前先確認 SmartCam 系統時間，不要讓時間在已建立 registry 後大幅回調。
- 若時間同步或韌體切換後突然找不到原本存在的外掛，先比較系統時間與 registry 時間。
- 不要將刪除整個 `runtime_data` 當成此問題的修復方法；它與 GStreamer registry 無關。
- 停止相機程序時優先使用 `SIGTERM`，避免硬體編碼器或 QMMF 相機資源沒有正常釋放。
- 同一時間只允許一個程序持有實體相機。
- 同一組串流連接埠只允許一套 MediaMTX。
