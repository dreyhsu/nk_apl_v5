# Live Flow Graph 需求評估與實作紀錄

## 合理性

需求合理。SOP 防漏系統若推論頁只顯示最終 PASS／FAIL，操作員無法知道系統正在等待哪一步，也無法分辨是辨識尚未穩定、步驟遺漏、逾時或視覺 NG。即時流程圖能把狀態機的內部狀態轉成現場可理解的操作指引。

## 採用的顯示模型

目前 SOP Engine 是線性順序狀態機，因此流程圖採垂直有向路徑，而不是自由節點圖：

1. 每一個 Step 是一個節點。
2. 節點之間依 `step_no / sort_order` 連線。
3. `current_step_id` 決定 CURRENT 節點。
4. `status` 決定 PENDING、DETECTING、DONE、TIMEOUT。
5. `alarm.step_id` 決定警報關聯節點。
6. Optional Step 使用虛線框，不會誤導為必要 Gate。

這與目前狀態機語意一致。若未來要支援分支、條件跳轉、平行步驟或跨相機匯流，需要先擴充後端 Flow Schema，不能只靠前端畫出假的分支。

## 實作頁面

### Offline Video SOP Inference

- 完整 Live Flow Map。
- 顯示 Flow 完成數與百分比。
- 顯示目前應執行步驟。
- DETECTING 顯示命中／Hold 累積進度。
- 影片結束後保留最終完整／缺漏狀態。

### Multi-Stream SOP Center

- 每一路串流有自己的獨立流程圖。
- 每張卡片顯示該 Stream 的目前 Step、完成比例和警報。
- 圖片與狀態 JSON 仍分開載入，不會重新把 Base64 塞回狀態輪詢。
- Flow 區域可獨立捲動，步驟多時不會撐壞整個 Dashboard。

### SOP Vision Flow Monitor

- 原步驟清單改為具有連接線的流程圖。
- 保留原有 KPI、事件、警報、三色燈狀態與操作按鈕。

## 後端 Payload 補強

每個 Step runtime 新增顯示所需的設定摘要：

- 樣板數量與 OK／NG 數量。
- ANY／ALL 邏輯。
- 連續命中目標。
- Hold Time 目標。
- Timeout。
- 是否允許 Out-of-order。

這些欄位在 Engine Reset 時由 Step Config 複製到 Runtime State，確保離線、多路與正式監控頁使用完全相同的資料來源。

## 邏輯邊界

- 一個 Product 目前只有一條線性 Flow。
- Step 內的多樣板 ANY／ALL 不會展開成獨立流程節點；它們是該 Step 的辨識條件。
- `ALL` 仍代表同一幀全部命中，不代表跨時間依序命中。
- 若需顯示每個 Template 的即時分數，可再加入 Step 展開詳情，但不應把 Template 誤畫成 SOP Step。

## 驗證

已完成：

- Python 語法檢查。
- 三個推論頁 JavaScript 語法檢查。
- 原 Smoke Test。
- Flow Designer Test。
- Live Flow Graph Payload／HTML Contract Test。
- Step 1 完成後 `current_step_id` 正確移至 Step 2。
- Runtime payload 中包含多樣板數量、Hit 與 Hold 目標。
