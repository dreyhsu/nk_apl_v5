# APL 與 DMZ 串接協定 v1 草案

這是供尚未開發的 DMZ 程式實作的接口契約。`dmz_mock.py` 只供本機測試，
不代表已完成正式 DMZ 串接。兩端必須共同實作下列欄位及冪等規則。

## 1. 啟動一輪

掃碼只建立待確認 SN。使用者在 HTTPS 頁面按「確認 SN」，且攝影機已有
新鮮影像，SmartCam 才送出：

```http
POST <DMZ_BASE_URL>/sn/check
Content-Type: application/json
Idempotency-Key: <request_id>
```

```json
{"sn":"10103704EU","cycle_id":"<本輪唯一識別碼>","request_id":"<本次確認固定識別碼>"}
```

DMZ 接受時回覆 HTTP 200 與：

```json
{"accepted":true,"sn":"10103704EU","cycle_id":"<原樣回傳>","request_id":"<原樣回傳>"}
```

只有 `accepted` 是 JSON 布林值 `true`，且三個識別欄位完全一致，才能開始
影像流程。拒絕、錯誤 SN、舊回覆、格式錯誤與逾時均不啟動。
第一版採請求／回覆接口，沒有開放未驗證的非同步 callback。
SN 不預設固定 10 碼；目前接受可列印 ASCII，預設上限 128 字元。
正式 SN 格式及檢查碼可在整合正式 DMZ 時收斂。

## 2. 回報 PASS

四個影像步驟依序完成後保留結果。使用者再次按「確認完成／回傳 PASS」才送出：

```http
POST <DMZ_BASE_URL>/result
Content-Type: application/json
Idempotency-Key: <另一個固定 request_id>
```

必要欄位：

```json
{"sn":"10103704EU","cycle_id":"<同一輪>","request_id":"<本輪 PASS 固定識別碼>","result":"PASS"}
```

請求會包含本輪時間與步驟資料供追溯；DMZ 應接受額外欄位。
接受回覆必須包含：

```json
{"accepted":true,"sn":"10103704EU","cycle_id":"<同一輪>","request_id":"<原樣回傳>","result":"PASS"}
```

收到有效回覆才顯示回報成功並回到等待下一個 SN。HTTP 200 本身不等於
業務接受。NG 不會透過這個接口冒充 PASS；第一版沒有自動送 NG 的協定。

## 3. 重送與中斷

- `cycle_id` 隔離每輪，即使同一 SN 日後重測也不能沿用上一輪進度。
- `request_id` 區別確認 SN 與回報 PASS；同一操作重試保持相同 ID。
- DMZ 必須將請求 ID、原請求及回覆**持久保存**。相同 ID／相同內容應回傳
  相同結果，不能重複建立工單或重複計數；相同 ID／不同內容應回 HTTP 409。
- 網路逾時可能表示 DMZ 已收到但回覆遺失。SmartCam 保留待回報結果，重試
  使用相同 ID；不以「沒有收到回覆」推斷 DMZ 沒收到。
- SmartCam 異常重開時，未完成的影像週期不能直接續算成 PASS。
  已持久保存但尚未確認的 PASS 保留供同 ID 重試。
- 攝影機在等待 DMZ 或 RUNNING 期間中斷會阻止放行，舊回覆不能重新啟動。

## 4. 設定

`config/apl_workflow.json` 的 `dmz` 區段預設 `mode: "disabled"`，因此未完成
接線及 DMZ 設定前無法自動啟動檢測。

正式模式：`mode: "http"`，`base_url` 填完整 HTTPS 位址，TLS 憑證正常驗證。
如 DMZ 使用公司 CA，須在設備信任鏈配置；程式不預設忽略憑證驗證。
遠端純 HTTP 需要明確設定 `allow_insecure_http: true`。

環境變數 `APL_DMZ_MODE`、`APL_DMZ_URL` 可覆蓋設定；不要把帳號密碼寫入 URL。
正式服務啟動環境不可殘留 `APL_DMZ_MODE=mock`。

本機模擬：`mode: "mock"`，限定 loopback，預設 `http://127.0.0.1:8791`。
模擬回覆帶 `mock: true`，介面與本輪資料都會標示測試模式。
模擬器只在記憶體去重，不能作為正式 DMZ 服務部署。
