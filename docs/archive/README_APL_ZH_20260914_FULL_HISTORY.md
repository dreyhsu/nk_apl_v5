# VisionEdge APL：單張拍照確認 MCIO 小卡

> 資料夾整理說明：正式程式與設定保留在專案根目錄；開發影片集中於
> `test_assets/`；舊 VisionEdge 測試集中於 `tests/legacy/`；歷史報告與舊版
> 原始碼備份集中於 `docs/archive/`。`runtime_data/` 只保存執行狀態與判定證據，
> 不應視為可部署原始碼。

## 本複製版的開發計畫（2026-09-10）

> 狀態：2026-09-10 本機開發版第一版已完成。單張判定、SN 鎖定、終止、
> 照片證據、影片控制、History API 及英文操作介面均已建立並通過本機驗證。
> 下方歷史說明保留作為來源紀錄；本複製版需求以本節為準。

本版本的開發來源為：

`D:\work_project\smartcam\NK_APL\VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP`

SmartCam 上若出現 `qtiqmmfsrc is unavailable`、Camera stopped 或 WHEP `502`，
請參考 [VisionEdge QTI 相機與 MediaMTX 故障排除](docs/QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md)。
該文件記錄 2026-09-14 實機診斷證據、GStreamer registry 重建、VisionEdge
重新啟動、MediaMTX 啟動及完整驗證方式。

這是一個獨立的簡化 Demo。原本的五步 Workflow、30 系列錯板判定及相關
程式碼會保留，但在本模式下先不啟用。實作時應使用明確的設定模式切換，
例如 `inspection_mode: "single_snapshot_cover_card"`，不要刪除或大段註解
既有邏輯，方便之後比較及恢復。

### 目標

本版只確認拍照當下是否符合：

`50_COVER_CLOSED_CARD_PRESENT`

也就是同一張判定照片中，必須同時確認「蓋板已關閉」及「MCIO 小卡已安裝」。
只看到小卡或藍色拉帶不能判 OK；只看到蓋板也不能判 OK。第一版不判斷先前
放板、關蓋順序、30／40／50 板型，也不使用其他狀態排除錯板。

Golden Sample 來源固定為：

`D:\work_project\smartcam\NK_APL\APL_golden_sample\50_COVER_CLOSED_CARD_PRESENT_CROOP`

目前有兩張正樣本，約為 `134×132` 與 `145×127`，主要包含藍色 MCIO 拉帶
及連接區域。既有測試資料的正確狀態分數約為 `0.935～0.939`，Demo 起始門檻
先以 `0.80` 作為比對實驗起點，並在結果中保存實際分數、命中的模板及匹配位置。
既有分數與門檻不能直接視為「蓋板及小卡雙條件」已通過驗證。

現有局部模板是否足以同時證明蓋板關閉及小卡安裝，仍需驗證；不能只因小卡
區域分數高就當成兩個條件都成立。實作方案可評估涵蓋兩者的組合模板，或
在同一張照片中分別比對蓋板與小卡區域後取 AND；採用方式及所需樣本另行確認。

### 本次已確認的四項決定

1. 掃碼後等待拍照、未匹配成功（`NOT_MATCHED`）、判定 OK 後等待確認，
   都可以按 `Terminate Inspection`，表示使用者終止本次判定程序，不是確認 PASS。
2. 每次拍照判定都保存，包括首次拍照、每次重拍及未匹配成功的照片；
   不只保存最後一次或 OK 的結果。
3. 一筆有效完整 SN 接受後即鎖定本輪；只有使用者確認上傳並收到 SIFI 成功回覆、
   保存結案，或按 `Terminate Inspection` 完成終止後，才開放掃描下一個 SN。
   相同或不同 SN 都不能覆蓋本輪，也不能先排入下一輪；重拍繼續使用原 SN。
   只有判定 OK、剛按下上傳、上傳中、上傳失敗或結果尚不確定，都不會自動解鎖。
4. 判定同時看蓋板與 MCIO 小卡：同一張照片、同一治具中的兩項條件都成立才可
   顯示 OK，不能把旁邊另一件產品或治具外的小卡納入。

### 實作進度（2026-09-10）

- `config/apl_workflow.json` 已啟用 `single_snapshot_cover_card` 模式。
- `apl_matching.py` 已加入同一張照片的目標比對，並以關蓋無卡與開蓋有卡樣本
  作為兩個反例條件；兩個條件都通過才接受。
- `apl_station.py` 已加入掃碼自動開始、SN 鎖定、`Capture & Inspect`、
  `NOT_MATCHED` 重拍、`PASS_CONFIRM`、SIFI 成功解鎖及終止解鎖狀態。
- `inspection_store.py` 已加入每次拍照的原圖、結果圖、JSON 與 SQLite 索引保存。
- `edge_runtime.py`、`video_test.py` 已停止單張模式的整片預判與掃碼倒帶，
  並加入播放、暫停、跳轉及選定畫面判定的後端能力。
- `visionedge_server.py` 已加入拍照判定、終止、影片控制、History 與證據下載 API。
- `static/edge_dashboard.html` 與 `static/apl_ui.js` 已改成英文單張判定介面：掃碼
  自動開始、`Capture & Inspect`、SIFI 未設定提示、`Terminate Inspection`、
  影片播放／暫停／時間軸與 Inspection History 均已接上後端。
- 影片測試與正式相機共用同一個 APL 歷史資料庫，因此切回相機後仍能反查影片
  測試的 SN、判定、來源及證據；影片本體仍以 UUID 隔離，不因同名而覆蓋。
- 舊 `test_local_workflow.py` 已改成明確載入 `legacy_workflow`，不再被本複製版的
  單張預設模式影響。完整測試為 `69/69` 通過，另通過 Python AST 與 JavaScript
  語法檢查。
- 瀏覽器已驗證固定美式實體鍵位輸入、掃碼自動鎖定、影片 Seek／Pause／Resume、
  EOF 停格、手動拍照判定、NOT MATCHED 重拍狀態，以及 History 的原圖／結果圖／
  JSON 連結；瀏覽器 console 無錯誤或警告。

### 本開發版的 PC 啟動與測試

本次驗證使用現有 `torch_anomalib_3` 環境，不需執行 `.sh`，也沒有新增或安裝套件：

```powershell
cd D:\work_project\smartcam\NK_APL\VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP
conda run -n torch_anomalib_3 python visionedge_server.py --demo --demo-video ..\test1.mp4 --no-tls --port 8767
```

瀏覽器開啟 `http://127.0.0.1:8767/edge`。`--demo-video` 可換成另一支已存在的
1920×1080 H.264 MP4；也可不帶 `--demo-video`，開啟頁面後用 `Choose Test Video`
選擇影片。停止程式使用 `Ctrl+C`。

完整本機測試指令：

```powershell
cd D:\work_project\smartcam\NK_APL\VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP
conda run -n torch_anomalib_3 python -m unittest discover -s tests
```

### 操作流程

```text
等待 SN
→ 使用者掃描條碼
→ SN 自動建立並鎖定本輪，開始等待拍照，不需要再按 Confirm SN
→ 使用者完成蓋板及安裝 MCIO 小卡
→ 使用者按 Capture & Inspect
→ 程式擷取當下最新且未過期的完整畫面
→ 判定同一張照片是否同時符合蓋板關閉及 MCIO 小卡已安裝
→ 保存此次原圖、結果圖、SN、判定及時間，不覆蓋前次拍照
   ├─ 兩項條件都符合：顯示 OK，結果保持常亮並等待人工結案
   └─ 任一條件不符合或無法確認：顯示 NOT MATCHED，允許調整後重拍
→ 使用者選擇處理方式
   ├─ OK 後 Confirm PASS & Upload to SIFI → 收到成功回覆並保存結案
   │  └─ 自動解除 SN 鎖定，等待下一個 SN，不必再按終止
   └─ Terminate Inspection → 保存終止紀錄，解除 SN 鎖定，等待下一個 SN
```

`Terminate Inspection` 不必等拍照或匹配成功才可使用；掃碼後等待拍照及
`NOT_MATCHED` 時同樣可終止。終止不刪除已保存的照片、SN 或判定歷史。

只有按下 `Capture & Inspect` 時才進行正式判定。Live Camera 可以持續顯示，
但不可在背景持續累積判定或自動完成本輪。相機模式使用當次擷取的新鮮原始幀；
影片模式使用使用者選定並已完成解碼的暫停幀。兩者都必須讓畫面、判定與保存
資料指向同一張影像，不能使用斷線前留下的舊相機畫面或上一個影片時間點。

### 判定及重拍規則

- 同一張照片同時確認蓋板關閉及 MCIO 小卡已安裝，且符合比對門檻時顯示
  `OK`，但尚未完成結案；任一條件不符合或無法確認都不可判 OK。
- 未命中時顯示 `NOT MATCHED`，第一版不直接保存為正式 NG。
- 未命中可能來自尚未完成操作、手部遮擋、視角偏移或 MCIO 狀態不符，
  因此允許在同一個 SN 下重新拍照。
- 新一次拍照更新目前候選結果，但每次拍照的原圖、結果圖與判定紀錄全部保留，
  包括 `NOT_MATCHED`；每次使用獨立 attempt ID，不覆蓋前次證據。
- 一旦得到 OK，鎖定該張結果照片與判定，直到使用者選擇上傳或終止。
- OK 後不可由後續 Live Camera 畫面自動改回 UNKNOWN 或 NOT MATCHED。

### SIFI 結案規則

按鈕名稱確定為 `Terminate Inspection`。其意思是「使用者選擇不上傳，
終止本次判定程序」，不是確認通過或成功完成。

視覺判定、程序狀態與傳輸結果必須分開記錄：

- `Confirm PASS & Upload to SIFI`：上傳成功後才能標示 `UPLOADED` 並結案。
- 使用者確認上傳、收到 SIFI 成功回覆並保存結案後，自動解除本輪 SN 鎖定並
  回到等待掃碼，不必再按終止。原輪次保留 PASS、`COMPLETE`、`UPLOADED` 與
  完整影像紀錄，不得因開放下一輪而改成 `TERMINATED` 或刪除紀錄。
- SIFI 上傳失敗：保持 OK 與相同 SN，不可顯示已上傳；允許使用者重試。
- 僅按下上傳按鈕不會解除鎖定；上傳中、失敗或逾時結果不確定時仍禁止新掃碼。
- `Terminate Inspection`：保存終止紀錄及已有的影像證據，清除本輪 SN 並回到
  等待狀態；不得把此輪標示為最終 PASS、COMPLETE 或已上傳。
- 例如：照片比對成功後選擇終止，記錄 `visual_result: OK`、
  `cycle_status: TERMINATED`、`upload_status: NOT_SENT_BY_OPERATOR`、
  `termination_reason: OPERATOR_SELECTED_NO_UPLOAD`。查詢頁需能同時顯示這些欄位。
- 上述「未送出」僅適用於確定未送出的請求。若 SIFI 請求已送出但逾時，
  可能已被對方接收，應保留 `upload_status: UNKNOWN` 待核對；不可改成未送出。
- SIFI endpoint、認證及 payload 尚未提供。未設定時必須顯示
  `SIFI not configured`，不可使用假的成功回覆，也不能因此自動解除 SN 鎖定。

SIFI 正式 API、認證、資料欄位、成功回覆及重送規則仍待提供；影片測試是否
允許對外送出資料也列為待討論項目。

### 影片模擬測試（已加入需求）

開發時不一定能拍攝產線，因此本版須能用影片模擬現場相機，讓使用者操作
相同的掃碼、拍照判定、終止及結果檢視流程。

影片測試由 `config/edge.ini` 的工程設定控制：

```ini
[features]
video_test_enabled = true
```

開發及驗證時設為 `true`。正式產線設為 `false` 並重新啟動 VisionEdge；關閉後
網頁不顯示影片選擇與播放工具，後端影片測試、Browser Video 及 Canvas 判定 API
亦會回傳 HTTP `403`，不能只靠手動呼叫 API 繞過。此設定不影響 Live Camera、
掃碼、拍照判定與 History。

啟用時可以先掃 SN，再於 `WAITING_CAPTURE` 或 `NOT_MATCHED` 選擇影片；程式保留
同一筆 SN、cycle ID 與 Workflow 狀態，將該輪標記為 Browser Test 並禁止送正式
SIFI。`JUDGING`、判定 OK、上傳中及復歸狀態仍禁止切換來源。完成或終止該輪後
才能切回 Live Camera，避免同一輪混用不同來源。

- 保留影片輸入、中文檔名顯示及同名影片隔離，起始支援 1920×1080 H.264 MP4。
- 提供 Play、Pause、Resume 及時間軸拖曳到指定時間，並顯示目前時間與總長度。
  直接輸入秒數、`HH:MM:SS.mmm` 與逐幀前後移動列為後續建議功能。
- 跳轉後等待後端完成解碼並顯示選定幀，完成前停用 `Capture & Inspect`。
  保存實際幀號或影片時間戳，不能只保存滑桿要求跳轉的時間。
- 暫停時可掃碼並按 `Capture & Inspect`，判定所見的那一幀；掃碼不能自行
  讓已選好的畫面跳回影片開頭。
- 播放中按判定，建議先暫停並顯示此次選中的幀，再對同一張原始尺寸影像判定。
  Live 預覽縮放或畫面上的按鈕、文字不可成為模型輸入。
- 有意暫停的影片是有效測試來源，不應被相機「畫面過期／未更新」規則判成故障。
- 單張拍照模式不需預先判定整支影片，也不會套用先前時間點的 OK。
  固定約 10 FPS 的整片分析留給原五步 Workflow 模式。
- 建議影片到尾端維持最後一幀並暫停，不自動循環；回到開頭由操作員控制。
- 紀錄來源為 `video_test`，並保存影片 ID、原始名稱、實際時間戳與幀號。

### 影像證據與資料反查（已加入需求）

每次拍照判定均須保存，包括首次拍照及所有重拍，不論結果為 OK 或
`NOT_MATCHED`。每張照片須能對應 SN、判定結果及拍照時間；保存原圖、結果圖
與判定紀錄，建議以 JSON 加上資料庫索引建立關聯。結果圖可直接讀到 SN、
視覺判定與含時區的時間，原圖保持乾淨。多次拍照與相同 SN 使用獨立 cycle ID、
attempt ID 防止覆蓋；重拍不建立另一個 SN，也不刪除本輪先前的失敗照片。

目前存放於 `APL_DATA_DIR/evidence/YYYY-MM-DD/`，同一個日期資料夾保存原圖、
結果圖及 JSON，索引位於 `APL_DATA_DIR/station.sqlite3`。檔名範例：

```text
20260910T063012123Z_HTS721010A9E630_OK_<cycle_id>_<attempt_id>_raw.jpg
20260910T063012123Z_HTS721010A9E630_OK_<cycle_id>_<attempt_id>_result.jpg
20260910T063012123Z_HTS721010A9E630_OK_<cycle_id>_<attempt_id>.json
```

檔名使用安全的 SN 字串；JSON 與資料庫保留完整原始 SN。時間以 UTC 保存並
保留顯示時區；拍照時間與影片內的相對秒數分開。檔名的 OK 是視覺判定，
本輪是否 TERMINATED、COMPLETE 及 SIFI 狀態應從結案紀錄讀取。

必要欄位包括：SN、cycle ID、attempt ID、拍照時間、來源模式、視覺判定、
分數、門檻、模板與設定版本、匹配框、原圖／結果圖路徑、程序狀態、上傳
狀態、終止原因、結案時間。影片來源另加影片 ID、名稱、幀號／時間戳。
圖片與索引寫入成功後才能把該次結果提供給使用者確認上傳。

2026-09-10 檔案盤點結果：

- `static/edge_dashboard.html` 的 Media 已能查看、下載照片與影片，依類型篩選；
  尚未提供 APL SN、判定結果、SIFI 狀態的反查。
- `edge_runtime.py` 的 `snapshot()` 目前保存 `raw_<時間>.jpg` 或
  `result_<時間>.jpg`，並未建立 SN、cycle 與拍照證據的完整關聯。
- 舊介面 `static/monitor.html` 已有歷史列表與影像詳情，`server.py` 有
  `/api/logs`、單筆詳情及原圖／結果圖 API。它們查的是 `inference_logs`，
  列表的 serial 來自 `products.serial`，不是本次 APL 掃碼的 SN。
- APL 的 `apl_station.py` 另用 `station.sqlite3` 保存 `cycles`、`events`、
  `outbox`。因此可參考既有歷史介面，但仍須接上 APL 週期與照片證據，
  不能把舊 product serial 當成掃碼 SN。

目前 APL 主介面已有英文 `Inspection History` 入口，第一版支援 SN 與視覺結果
查詢，並提供單輪詳情、各次拍照的時間順序及原圖／結果圖／JSON 開啟；不只顯示
最後一次判定。日期範圍、程序狀態、上傳狀態、來源篩選及分頁列為後續功能。
一般 Media 頁面繼續管理普通拍照和錄影。
終止的紀錄也須可查；尚未拍照就終止時顯示「無照片」，不填入其他輪次的圖。

### 建議狀態

```text
WAITING_SN
WAITING_CAPTURE
JUDGING
NOT_MATCHED
PASS_CONFIRM
UPLOADING
COMPLETE
TERMINATED
RECOVERY_REQUIRED
```

`NOT_MATCHED` 可以重新拍照；`PASS_CONFIRM` 只能透過兩個人工結案按鈕離開。
`WAITING_CAPTURE`、`NOT_MATCHED`、`PASS_CONFIRM` 均允許終止。有效 SN 接受後，
鎖定期間任何再次掃碼都不可更動本輪 SN、判定、照片或建立另一個 cycle。
鎖定須涵蓋 SmartCam 掃碼槍、瀏覽器輸入及 API，不能只把輸入框停用；重新整理
或重連頁面仍須恢復本輪 SN 及鎖定狀態，不得重播先前拒絕的掃碼。
只有確認上傳成功並保存 `COMPLETE`，或使用者終止並保存 `TERMINATED` 後，
才解除 SN 鎖定並回到 `WAITING_SN`；歷史輪次狀態及影像保留，不隨畫面重置而清除。
相機中斷、程式重啟或儲存失敗時不可自動形成 PASS。

### 待討論事項與建議預設

以下是建議，尚未視為使用者已確認：

| 項目 | 建議 | 需要決定的原因 |
|---|---|---|
| 判定／上傳執行中的終止 | 建議判定中的終止須處理晚到結果；SIFI 送出中須先確認請求狀態，不可直接宣稱未送出 | 避免上一輪結果寫入新 SN，或對外已送出卻被記成未送出 |
| 兩項條件的比對方式 | 驗證現有組合模板是否足夠；不足時評估蓋板與小卡各自 ROI 比對後取 AND | 判定範圍已確認為兩者同時成立，但現有局部正樣本不代表已證明兩者 |
| 影片播放操作 | 掃碼保留選定時間；播放中拍照自動暫停；EOF 停在最後一幀 | 避免開發測試時錯過選好的畫面 |
| 影片資料送出 | 預設不送正式 SIFI；有測試 endpoint 時才測試通訊 | 測試照片與真實產品履歷需能辨識 |
| 查詢資料保存 | 先全留，不自動刪除；空間不足時停止新增判定並提示 | 確定每天拍照量、保存天數與備份方式後再訂清理規則 |
| SIFI 契約 | 確認傳 SN／結果即可，或連照片一起傳；確認 request ID 去重及逾時查詢方式 | 決定什麼才算上傳成功，以及重試會不會產生重複履歷 |

單張拍照模式須根據同一張影像確認兩項條件，不得把不同時間點的蓋板與小卡
結果拼成 OK。原多類別的第一、第二名分差 `min_gap` 與跨影像 `stable_frames`
不適用於本模式。保留原設定供舊模式
使用，不把同一張照片重複計算兩次來冒充穩定證據。ROI 與 0.80 門檻先作起點，
用正常、無卡、遮擋等選定影片幀驗證後再調整，分數不表示成功機率。

### 實作順序與驗收重點

1. 新增單張模式：有效完整 SN 自動開始，等待拍照、NOT MATCHED 重拍、OK 保持、
   Terminate Inspection，以及原流程停用切換。
2. 完成影片暫停與選時，確認「顯示幀＝判定幀＝保存原圖」。先建立本機測試記錄。
3. 綁定影像、SN、時間與狀態，完成 Inspection History 查詢及下載。
4. 取得 SIFI 契約後接上真實上傳；驗證成功、失敗、逾時不確定與重送情境。

驗收應涵蓋：無 SN 不接受判定、跳轉未完成不能拍照、NOT MATCHED 不可確認 PASS、
終止後不能顯示完成或上傳成功、同 SN 多輪不覆蓋、原圖不含 UI 疊字、重新開啟
查詢仍能對到相同圖片與 SN。拍照、重拍與上傳重複按鈕不得建立重複結果；
影片模式不因暫停觸發相機故障。SIFI 契約提供前，上傳路徑保持未設定狀態。

本次確認需求另須驗收：

- 掃碼後未拍照、NOT MATCHED、OK 待確認三種情況均可終止，且不形成確認 PASS。
- SN 鎖定期間重掃同 SN 或其他 SN，都不覆蓋現有資料、不建立或排入下一輪。
- 確認上傳成功並保存結案後可掃下一個 SN，無須再按終止；完成終止後也可掃下一個。
  單純判定 OK、上傳中、上傳失敗或逾時未確認時，不可自動開放新掃碼。
- 每次重拍都有獨立紀錄；先 NOT MATCHED、後 OK、再終止，仍可查到兩次照片。
- 蓋板關閉且已裝小卡才可 OK；關蓋無卡、開蓋有卡、開蓋無卡均不可 OK。
- 小卡在治具外、只看到拉帶或蓋板被遮擋而無法確認時，不能僅靠局部高分判 OK。

### 保留與停用範圍

本版本保留以下既有能力：

- SmartCam 與 PC 的相機來源。
- MediaMTX／網頁 Live 畫面。
- 英文介面、溫度、錄影、Media 頁面及影片測試。
- Honeywell 掃碼槍及固定 ASCII 鍵位輸入。
- 本機 SQLite 週期與事件紀錄。
- 原本五步 Workflow 與 30 系列判定程式碼，但在此模式不執行。

本次規劃不修改原始 `VisionEdge_APL`、Golden Sample、SmartCam 裝置，也不製作
`.app`。依 SmartCam 開發規範，PC 只能完成語法、邏輯與 OpenCV 測試；QTI
相機、MediaMTX 及實際硬體結果仍須由使用者部署到 SmartCam 後驗證。

### 已知限制

這是只有正樣本的 Template Matching。它只能表示畫面與「蓋板關閉且 MCIO
小卡存在」的外觀相似，不能證明 MCIO 電性正常或已完整插到底。正式上線前
仍應收集未插卡、插歪、未插到底、其他板型、手部遮擋及光線變化等影像，
驗證誤判率並重新決定 ROI 與門檻。

---

## 以下為複製時保留的原五步版本說明

> 歷史內容保留原文，其中的「最新版」路徑、四步／五步描述及啟動指令屬舊版
> 紀錄，不代表本複製版已實作上述新需求。後續實作完成再更新本複製版操作說明。

本版源自 `richard/VisionEdge`，建立於 `NK_APL/VisionEdge_APL`，沿用原本的
Live、Media、Settings 與 Flow Studio 介面。Template Matching 採用 NK_APL
已做影片驗證的互斥分類方式；以 `firmware_update/smartcam_ai_inference_SDK`
的相機、MediaMTX 與 HDMI 管線作為移植參考。

這份 README 是目前單張 APL 規格入口。上游 VisionEdge 的原始 README 與歷史
review 文件已集中在 `docs/archive/`，不能取代本文件的 APL 操作規格。

目前這個單張判定開發版位於
`D:\work_project\smartcam\NK_APL\VisionEdge_APL_COVER_CLOSED_CARD_PRESENT_CROOP`，
版本標記見 `APL_DEVELOPMENT_VERSION.txt`。此目錄是 PC 開發來源；未經使用者
明確指示，不部署至 SmartCam，也不製作 `.app`。

## 1. 操作流程

```text
掃描 SN → 畫面顯示待確認 → 按 Confirm SN & Start
→ 開始四步影像檢測 → 四步全部完成、保持綠色
→ 按 Confirm Completion → 本機保存 PASS → 等待下一個 SN
```

掃碼結尾的 Enter **只送入待確認 SN**，不會代替第一次人工 OK。
首次 OK 前可以看到影像，但不能累積本輪工序。
目前設定 `station_mode: "local"`，掃碼內容直接作為 SN，不連線或等待 DMZ。
本機保存 PASS 不代表已回報其他系統；未來 DMZ 契約與程式仍保留。
完成後不再使用舊影片原型的「3 秒／下一次封蓋自動重置」。

正式影像順序固定為：

1. `50_PRODUCT_LOADED`
2. `50_COVER_CLOSED`
3. `50_COVER_CLOSED_CARD_PRESENT`
4. `50_COVER_OPEN_CARD_PRESENT`

EMPTY 不是第一步。30 板穩定出現會在檢測中鎖定 NG；UNKNOWN 不解除 NG，
穩定看到 EMPTY 才視為已移除，結束該 NG 週期並等待重新掃碼。
EMPTY 不會算成下一輪的「50 板已放置」。第一版不將 NG 自動送至 DMZ。

## 2. 資料與比對

- `templates/`：從既有 `APL_golden_sample` 複製的七類、共 14 張 Golden Sample。
- `config/apl_workflow.json`：四步順序、NG 類別、ROI、門檻、掃碼及 DMZ 設定。
- `runtime_data/apl/station.sqlite3`：正式週期、事件、待回報與回覆紀錄。
- `runtime_data/demo/`：PC 模擬使用獨立週期紀錄，與正式資料分開。
- `runtime_data/edge/`：原有拍照、錄影資料。
- `db/inspection.db`：上游 Flow Studio 的獨立產品／樣板資料庫。

固定參數起點：1920×1080、搜尋 ROI `751,254,431,588`、門檻 0.80、
第一與第二類最低分差 0.05、相同 Raw 連續 2 次才更新 Stable、分析目標 10 FPS。
UNKNOWN 不顯示灰色匹配框。不同尺寸的影像會被拒絕，不會偷偷放大或退回全圖搜尋。
旋轉、視角、焦距或解析度變更後需要重新驗證 Golden Sample 與 ROI。

第一版產線判定讀取 JSON 與 `templates/`，**不會直接套用 Flow Studio DB 的編輯**。
Flow Studio 保留作為樣板／流程編輯參考，頁面會提示此限制。更新正式模板及
JSON 後，在本輪結束的狀態停止並重啟程式，使新設定生效。

來源的 `.git`、快取、舊資料庫、uploads、runtime_data 與 ZIP 未帶入新版。
原始 VisionEdge、產線 SDK 及 Golden Sample 沒有被修改。

## 3. PC 查看與測試

本機既有 `torch_sister` 環境有 Flask、NumPy、OpenCV；不必為同源网页安裝
flask-cors。只有額外設定 `TM_CORS_ORIGINS` 時才需該選用依賴。

在 PowerShell 啟動，不需要 DMZ mock：

```powershell
cd D:\work_project\smartcam\NK_APL\VisionEdge_APL
conda run --no-capture-output -n torch_sister python -B visionedge_server.py --demo --demo-video ..\test1.mp4 --no-tls --port 8767
```

開啟 `http://127.0.0.1:8767/edge`。以 `TEST-APL-001` 輸入 SN，再按確認。
範例影片會循環。尚未完成的檢測遇到影片回到開頭會要求復歸，禁止跨影片輪次
累積成 PASS。PC 回放不是 Qualcomm、MediaMTX 或 HDMI 的實機驗證。

Live 頁面的 Choose Test Video 可上傳影片，再按 Play Video Test。
支援可解碼的 MP4/MOV/AVI/MKV/M4V、1920×1080，單檔上限 1 GB。
請在影片開始後掃描測試 SN、確認；短片若來不及操作，可取消後重新播放。
同檔名使用獨立 UUID 資料夾；影片及獨立測試紀錄放在 `runtime_data/video_tests/`。
完成或取消本輪後，按 Use Live Camera 回到原輸入。切換前必須先停止錄影。
影片來源只在記憶體切換，不覆寫 `config/edge.ini`，不使用正式工站的紀錄庫。

停止程式使用 Ctrl+C。

```powershell
conda run --no-capture-output -n torch_sister python -B -m unittest discover -s tests -v
```

## 4. SmartCam 啟動前

短期採獨立目錄部署，不是已封裝或安裝完成的 v2.4 `.app`。
預定板端目錄為 `/var/rootdirs/opt/VisionEdge_APL`。
部署時需要將本工具程式、config、templates、static 一起帶入；不要帶 PC demo 紀錄。

1. 確認實際板號、韌體與 Ethernet IP，確認相機視角與 Golden Sample 相同。
2. 先停止目前持有相機的 inference/capture app，等待釋放；同一顆相機只能有一個 owner。
3. 確認板端已有 Python 3、Flask、NumPy、OpenCV、PyGObject、Cairo，以及
   `qtiqmmfsrc`、`qtivtransform`、`jpegenc`、`cairooverlay`、`v4l2h264enc`、
   `h264parse`、`rtph264pay`、`waylandsink`（HDMI 模式）等 GStreamer 插件。
4. 確认 MediaMTX 二進位位置及版本支持所附 RTP/ICE TCP 設定。
   本工具包含配置與啟動腳本，不內嵌另一份 MediaMTX 執行檔。
5. `config/edge.ini` 選 QTI、確認 HTTPS 憑證／金鑰及埠號。來源憑證的主機名
   未必符合此工站；部署需配置與實際位址相符、用戶端信任的憑證。
6. `config/apl_workflow.json` 目前使用 `station_mode: "local"`。
   不需設定 DMZ URL；正式 DMZ 接入須另行切換模式及重新驗收。
7. 檢查存儲空間；不自動刪除未回傳的工站結果。

`visionedge-apl.service` 使用板端既有 `mediamtx.service`，不能再啟動第二個
MediaMTX 佔用 8889。隨附的 `mediamtx.yml` 是要經備份後取代既有設定的候選檔，
`run_mediamtx.sh` 只供獨立驗證。確認後才安裝到設備；工具不會自行覆寫既有
SDK 服務或系統設定。

## 5. HTTPS 與 HDMI

MediaMTX 路徑：相機 → Template Matching／最新疊圖 → 共用 H.264 encoder
→ 本機 RTP → MediaMTX → 網頁 WebRTC。
HDMI 分支顯示同一份判定與流程資訊，未接螢幕時採 headless 模式。
HDMI 的影像輸出不等於板端有網頁瀏覽器；兩次 OK 先從 PC HTTPS 網頁操作。

沿用產線 SDK 的網路方式：HTTPS 提供頁面及 WHEP 信令，WebRTC 影像另走
ICE/TCP 5000；MediaMTX internal WHEP 8889、RTP loopback UDP 5004。
「僅網頁顯示」表示不接 HDMI，並不表示網路只開一個 HTTPS 埠即可。
MediaMTX advertised IP 必須填 PC 實際可達的 SmartCam IP，不能沿用舊產線固定 IP。

主要 AI 畫面使用 MediaMTX；Raw 選項是原始影像診斷。
影像中斷會顯示錯誤與停止接受新工序，不把最後一張畫面當成持續有效的判定。

## 6. 掃碼槍接法

SmartCam 單一 Type-C → 有外接電源的 Hub → Honeywell Xenon 1900g。
程式依 VID/PID `0c2e:0901` 動態尋找 HID keyboard，支援 Shift 大寫、Enter
結尾、拔插重找；不寫死 `/dev/input/event6`，也不將 SN 固定為 10 碼。

Type-C 為 Host 時，以 Ethernet HTTPS／SSH 管理；不能同時透過同一接線 USB ADB。
掃碼程式需要讀取對應 `/dev/input/event*` 的權限。
PC 模式亦可將掃碼槍當鍵盤輸入網頁 SN 欄位。

## 7. 回報、故障與復歸

- 正常完成後保持結果，第二次人工 OK 才保存本機 PASS。
- 本機儲存失敗不放行；測試結果標記 `test_mode`，不混入正式結果。
- 未來 DMZ 模式保留失敗重試與請求 ID 機制，目前不啟用。
- 週期中斷或重新開機不會沿用舊 Stable 完成未結束的檢測。
- 相機停止／故障時中止 RUNNING，需重新確認工件並復歸。
- 工站 SQLite 必須備份及規劃保存期限；不要在 pending PASS 尚未結案時刪除它。
- 詳細 DMZ 契約見 [DMZ_PROTOCOL_ZH.md](docs/DMZ_PROTOCOL_ZH.md)。

## 8. 上線驗收

軟體檢查、PC 回放及網頁操作完成後，仍須在 SmartCam 驗證：

- 掃碼／第一次 OK 前不推進；目前無 DMZ 通訊。
- 四步正確順序、30 誤放與移除、UNKNOWN 遮擋、重複按鈕及第二次 OK 回報。
- MediaMTX 真實 WebRTC 影像、HDMI/headless 切換與兩者判定座標一致。
- 相機停止重啟、錄影 EOS finalize、MediaMTX 故障、掃碼槍拔插、主機重啟。
- 長時間溫度、節拍、掉幀、磁碟、網路中斷與同 ID 重送。

目前沒有 40 系樣本，也沒有可證明 MCIO 完整插到底或電性正常的判定。
既有 Golden Sample 是小量固定視角資料；產線驗收不能用 PC 單次成功替代。

## 9. 目前介面與驗證

Live、Media、Settings、Flow Studio 介面改為英文；Live 不顯示串流協定細節。
PC 和 SmartCam 的 Workflow 使用同一份繪圖規格：灰色待辦、黃色目前步驟、
綠色完成；只顯示有辨識到的匹配框。Live SOP Flow 在 System Status 上方。

System Status 每 2 秒讀取真實 Linux thermal zone，優先顯示最高 CPU 溫度，
排除電壓／電流通道。PC 沒有對應感測器時顯示 Unavailable，並非 0°C。

2026-09-08：52 項測試通過，含本機 SN、四步順序、影片辨識完成、30 系復歸、
上傳資料隔離、來源停止失敗保護與溫度解析。完整更新記錄見
[UI_LOCAL_SN_UPDATE_ZH.md](docs/UI_LOCAL_SN_UPDATE_ZH.md)。

## 10. 歷史狀態：2026-09-07（非目前部署狀態）

PC 已以 `test1.mp4` 完成一輪端到端操作：掃描候選、第一次人工 OK、四步依序
完成、結果常亮、第二次人工 OK 及 mock PASS 回覆。40 項單元測試全數通過，
Golden Sample 七類各兩張均可辨識；瀏覽器 console 沒有錯誤。這些結果是 PC
回放與 mock DMZ 驗證，不是 SmartCam QTI 效能或正式 DMZ 驗收。

開發機 `usi-smartcam-056325` 可從 Ethernet `192.168.50.212:22` 以 SSH 管理，
韌體為 `v2.4-20-dev`，Python 3.12.12 具備 Flask、OpenCV、NumPy、PyGObject、
Cairo。QTI、Cairo overlay、H.264、RTP、Wayland 與 JPEG 的 GStreamer 元件均存在。

板端現有 MediaMTX 雖已啟動 8889，但 `webrtcAdditionalHosts` 仍是
`169.254.130.130`，且啟用 UDP 8189；上線前須備份後改成目前可達 IP
`192.168.50.212` 與工廠允許的 ICE/TCP 5000 設定。檢查當下沒有在 USB sysfs
或 `/dev/input/by-id` 看到 Honeywell，需重新確認有供電 Hub 與掃碼槍枚舉。

目前只完成板端唯讀盤點，尚未複製、安裝、停用舊服務或佔用實體相機。
