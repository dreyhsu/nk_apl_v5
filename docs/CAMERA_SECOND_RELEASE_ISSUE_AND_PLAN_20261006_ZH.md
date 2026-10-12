# 相機第二次釋放失敗：問題統整與修正計畫

更新日期：2026-10-07。狀態：問題未結案；最小來源管線已重現第二次 HAL 關閉失敗，修正尚未確認。

最新實機結論見[2026-10-07 最小重現診斷紀錄](../../../camera_release_probe/DIAGNOSIS_20261007_ZH.md)：第一輪通過，第二輪收到 243 張影格且 EOS 成功，NULL 在約 0.277 秒回 FAILURE；CamX 報 Sensor／CSIPHY 兩個 orphaned／stale handles，cam-server 因 SIGABRT 結束。來源端最小管線已重現，不需先加入編碼或 MediaMTX 分支才能觸發。接下來優先追查 SensorCache／CSL session 所有權及 FastRPC 收尾，詳細證據與未確認部分見該紀錄。

2026-10-07 工具補充：已在獨立資料夾新增[相機來源釋放診斷工具](../../../camera_release_probe/README_ZH.md)，供 P1 最小來源管線測試。包含兩輪子程序監督、EOS／NULL 狀態、原生輸出、系統 journal／kernel、cam-server 身分對照與 recovery 保護。使用者已在開發機執行 .3 並提供失敗紀錄；不代表產線驗收或硬體修復。下面 2026-10-06 的現場紀錄保留為歷史對照。

## 1. 本次要解決什麼

相機第一次開啟、停止可以完成；再次開啟後，第二次停止失敗。停止失敗會使後續啟動被 recovery guard 阻擋，無法維持正常的連續操作。

目標是同一次開機中能重複啟動、停止，並確實釋放相機，不依賴重開機、刪除保護標記或強殺程序作為正常操作方式。Ctrl+C、UI 停止相機及啟動中取消，都必須走一致且有時間上限的釋放流程。

範圍是 `NK_APL/APL_cowork_2/nk_apl_v5`，分支 `feature/sfis-bypass-switch`。保留工號、SN、掃碼槍與 SFIS 契約，不混入 Go Live 自動收集版流程。`smartcam_ai_inference_SDK_HPH_3` 僅作停止順序的比較來源，不整包覆蓋。

## 2. 已有實機證據

2026-10-06 唯讀 SSH 查詢對象是開發機 `usi-smartcam-056325`，IP `192.168.50.212`、MAC `00:10:c6:05:63:25`，BSP `v2.4-20-dev`。不是產線 `746708` 的驗收結果。

部署位置為 `/var/rootdirs/opt/nk_apl_v5`。板上 `edge_runtime.py`、`camera_worker.py` 與本機 `dfde633` 的雜湊相同；這只證明兩個核心檔案一致，不代表整包部署均已核對。

以下時間均為當次紀錄的 UTC：

| 項目 | 第一次停止 02:06:07 | 第二次停止 02:09:08 |
|---|---|---|
| worker | PID 1911 | 新 worker，PID 4627 |
| callback 排空 | 成功 | 成功 |
| 主 pipeline EOS | 成功 | 成功 |
| pipeline NULL | 成功，約 0.242 秒 | FAILURE，約 0.194 秒，停在 READY、pending NULL |
| worker 回收 | exit 0，已回收 | exit 70，已回收，未強殺 |
| cam-server | PID 569 正常關閉 Camera 0 | PID 569 因 SIGABRT 結束，systemd 重新啟動成 PID 6682 |

第二次停止的 element 診斷中，`camsrc` 未到 NULL；已記錄的 `encoder`、`frame_sink`、`h264_sink`、`stream_udp`、`display` 均已到 NULL。當時沒有活動錄影，不能把這次故障解釋為 MP4 尚未收尾。

HAL 紀錄的關鍵差異：第一次有 sensor／CSIPHY release success 及 `Camera(0) successfully closed`；第二次出現 `bReleaseDevice=0`、`2 devices orphaned`、`Stale device handles on Close`，隨後 cam-server ABRT。

**目前確認的是失敗發生在相機來源關閉及 HAL 資源釋放路徑；尚不能只憑這些訊息確定是哪一段插件、QMMF／HAL 快取或應用操作順序造成殘留。** 本次沒有可對應的 cam-server core／backtrace，因此不能宣稱已定位到廠商程式的某一行。

完整原始對照、雜湊及查詢指令見[原修正計畫第 14 節](CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md#14-第二次停止失敗的實機對照與修正分工)。

## 3. MediaMTX 是否是原因

目前沒有證據顯示 MediaMTX 是此次相機釋放失敗的起因。

當時 MediaMTX 是獨立接收 `udp+rtp://127.0.0.1:5004` 的服務，PID 維持 4298、重啟次數為 0，未發現與相機的 PartOf／BindsTo 連動。相機先在 02:09:08 失敗，WebRTC session 在 02:09:09 關閉，RTP 在 02:09:18 逾時。

相機來源停止後，MediaMTX 收不到影像而斷流是可能的後續結果，不等於 MediaMTX 持有相機。仍應用下列分支比較排除串流管線對停止順序的影響，不能只因為它是獨立服務就完全排除間接影響。

## 4. 已修正與尚未解決的界線

| 項目 | 目前狀態 | 後續要求 |
|---|---|---|
| Ctrl+C、worker 結果回報與有界回收 | 已有實作 | 重複實機操作驗收 |
| callback 停止、錄影收尾、主 EOS 後轉 NULL | 已有實作 | 主 EOS 成功仍會第二次失敗，不能視為根治 |
| NULL 失敗與逾時分開記錄、element 狀態診斷 | 已有實作 | 保留，補齊重現條件與 HAL 對照 |
| recovery guard、相機擁有權與失敗保留 | 已有實作 | 不可用刪標記掩蓋不安全狀態 |
| 第二次 camsrc／HAL close 失敗 | 實機確認，未修復 | 最小重現、分支隔離後修根因 |
| UI 停止、錄影中停止、業務錯誤中的釋放 | 有既有設計及 PC 測試 | 尚不能宣稱所有實機階段已驗收 |
| TLS／502／串流設定差異 | 另類問題 | 與硬體 NULL 失敗分開追蹤，不用改埠代替修復 |

既有 PC 測試結果不能證明 Qualcomm 硬體已釋放。程序退出、worker 已回收，也不能單獨證明 HAL 已關閉相機。

## 5. 修正順序

### P0：固定比較基準

記錄完整部署版本與關鍵檔案雜湊、設定、BSP／插件版本、boot ID、PID 及啟動指令。保留第一與第二次停止的應用、kernel、cam-server 記錄，使用同一時間線對照。

輸出：可重現條件與版本清單。已有的故障證據足以開始製作診斷工具，不要求使用者先手動重現一次才開始處理。

### P1：建立獨立、可停止的最小重現工具

預計新增 `scripts/camera_release_probe.py`，不匯入應用的判定、SFIS 或錄影流程。先測 `qtiqmmfsrc → fakesink`，保留與應用相同的相機、caps 及 PREVIEW pad 設定；確認設定讀回值與收到新影格後才算啟動成功。

實際工具位置（2026-10-07）：`NK_APL/camera_release_probe/camera_release_probe.py`，部署到 `/var/rootdirs/opt/camera_release_probe/`；採獨立資料夾，不放進 APL 的 scripts。詳見上方操作文件。編碼器、顯示與 RTP 等 P2 分支仍待後續逐項加入。

每輪使用新的子程序，由外層監督時間上限，記錄 JSONL：輪次、boot ID、PID、影格數、EOS、set_state／get_state 回傳值、目前／pending 狀態、cam-server PID 前後差異及子程序退出結果。

先測兩輪以重現第二次失敗。任一輪 NULL 失敗、cam-server ABRT 或非預期重啟即停止；不自動刪 recovery 標記、不無限重試、不在失敗後繼續污染比較條件。只有安全基準下才擴充連續測試次數。

### P2：一次只加入一個管線因素

| 比較階段 | 加入內容 | 要回答的問題 |
|---|---|---|
| A | 相機來源與 fakesink | 沒有應用分支時是否仍第二次關閉失敗？ |
| B | 編碼器 | 編碼／buffer 持有是否改變關閉結果？ |
| C | appsink 與實際 callback | callback、影格引用或等待順序是否造成差異？ |
| D | overlay／display 分支 | 顯示與資源所有權是否介入關閉？ |
| E | RTP 與 MediaMTX 接收 | 完整串流分支是否才觸發問題？ |

各階段保持相同相機設定，失敗後先恢復到已知安全狀態再比較。對照 HPH_3 的 pad 設定、EOS、收尾與 NULL 順序；它的 EOS 後延遲可作受控實驗，不能直接當成「加兩秒就修好」。不複製無限等待。

### P3：依重現結果修改根因

- **A 即失敗：** 將最小腳本、相同 BSP／插件身分與兩次 HAL close 對照交給相機／韌體維護方，追查 sensor cache、裝置引用計數與 release 路徑；需要 core 時另安排受控擷取。不可虛構停用快取參數或直接呼叫未公開 vendor 釋放函式。
- **A 正常，加入某分支才失敗：** 修改該分支建立／停止順序、callback 生命週期與 buffer 所有權，以 `edge_runtime.py`、`camera_worker.py` 的實際責任界線修正；不靠強殺掩蓋。
- **應用協調有競態：** 檢查 `camera_lifecycle.py` 與 `visionedge_server.py` 的 Start／Stop 序列化、取消啟動與舊結果隔離。只有證據指向該處才擴充修改，不改 SFIS request／response 契約。

停止成功仍必須同時符合：必要錄影完成、相機到 NULL、worker 結果匹配本次 session、程序已回收；任何不安全失敗不得回報成功。強殺只能是有界的最後回收手段，不能稱為硬體釋放成功。

把相機長駐可減少重開次數，但不是這次「真正釋放失敗」的修復，也不自動改成此架構。

### P4：實機驗收後才結案

| 情境 | 最低驗收 |
|---|---|
| 手動執行、Ctrl+C、再次執行 | 連續 30 輪 |
| UI 停止相機、再次啟動 | 連續 20 輪 |
| 啟動途中取消 | 連續 10 輪，無遲到影格／舊 worker |
| 單獨停止錄影 | 連續 10 輪，MP4 可播放、預覽繼續、可再錄影 |
| 錄影中 UI 停止相機或 Ctrl+C | 各 5 輪，錄影收尾及完整相機釋放 |
| 無工號、SN 拒絕、判定錯誤、SFIS 等待／錯誤 | 各情境驗證停止不被業務鎖阻擋；不向正式 SFIS 送測試交易 |
| 注入停止失敗 | 顯示真實失敗、保留保護、不假報釋放成功 |

每輪都須取得新影格、正常到 NULL、回收 worker；不得出現 cam-server ABRT、非預期服務重啟或持續資源增長。錄影情境另外檢查輸出檔案，不只看 UI。

「停止錄影」與「停止相機」語意保持分開：前者正常只結束錄影分支，後者才釋放來源。若要改成兩者都關相機，需另列使用者行為變更，不能暗中更動。

## 6. 預計產出與目前執行界線

下一階段先產出最小重現腳本與其純邏輯測試，再於開發機安排受控比較；根據結果決定應用修正或 BSP／插件修正。部署／實機啟停需與當次授權範圍一致，產線待開發機驗收後才安排。

程式修正時同步補充 `tests/test_camera_shutdown.py`、`tests/test_runtime_shutdown.py` 或相關生命週期測試，並更新交接文件中的版本、已通過情境與限制。測試不能只 mock NULL 成功，必須涵蓋 FAILURE、逾時、重複停止與第二次啟動。

本次僅整理文件，沒有新增診斷工具、修改程式、重啟服務或操作相機。歷史實作細節與既有問題總表保留於[原修正計畫](CAMERA_RELEASE_REPAIR_PLAN_20261005_ZH.md)，後續以實際驗收結果更新狀態，不預先承諾問題已永久消失。
