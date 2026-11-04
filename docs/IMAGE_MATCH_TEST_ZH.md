# 資料夾照片比對與人工答案

在專案根目錄執行，將 `照片資料夾` 換成實際路徑：

```bash
python scripts/image_match_test.py --image "照片資料夾"
```

工具會比對該資料夾內所有支援的圖片，不遞迴子資料夾。
支援 JPG、JPEG、PNG、BMP、TIF、TIFF；自動略過先前產生的 `*_overlay` 圖片。
輸入照片尺寸必須符合 `config/apl_workflow.json` 的 `reference_size`。
目前使用三類 Golden Sample，判定關蓋與插卡；每張圖片獨立判定，
不模擬 Auto Capture 的連續命中次數。

## 首次執行

即使沒有答案檔，也會照常執行比對，並自動在照片資料夾產生：

- `image_match_labels.json`：人工答案；首次建立時，每張照片的值為 `null`。
- `image_match_results.json`：這次執行的結果；每次重跑更新。
- `照片名稱_overlay.jpg`：含 ROI、比對分數與插卡／關蓋判定的圖片。

## 填寫人工答案

編輯 `image_match_labels.json`，以照片完整檔名（包含副檔名）為鍵：

```json
{
  "正常產品.png": true,
  "未插卡.jpg": false,
  "尚未確認.png": null
}
```

- `true`：照片實際應該 PASS。
- `false`：照片實際不應該 PASS。
- `null`：尚未標註，照常比對，但不計入準確率與誤判統計。

使用 JSON 的 `true`、`false`、`null`，不要加引號，不接受 `1`、`0`。
工具不會把模型預測寫成人工答案，也不會覆蓋已填的答案。
新增照片會自動補入 `null`；照片移出後，原有答案仍保留，
但沒有對應照片的答案不參與本次統計。
若答案檔格式有誤，工具會顯示錯誤並停止，保留原檔供修正。

## 重跑與檢查誤判

填好答案後，執行同一個指令。末尾的 `MISMATCHES` 列出所有已標註的誤判照片：

```text
MISMATCHES (manual answer vs template matching):
  未插卡.jpg: FALSE PASS; expected=FAIL, actual=PASS, score=..., gap=..., reason=...
  正常產品.png: FALSE REJECT; expected=PASS, actual=FAIL, score=..., gap=..., reason=...
```

`FALSE PASS` 是不該通過卻被放行；`FALSE REJECT` 是應該通過卻被拒絕。
摘要顯示成功比對數、通過數、已比較的答案數、未標註數、誤判數及準確率。
準確率只計算「已標註且成功比對」的照片；若沒有任何可比較的答案則不顯示準確率。

結果 JSON 保留每張照片的：

- `image`、`overlay`：原圖與標註圖片路徑。
- `expected_pass`：人工答案。
- `actual_pass`：實際判定結果。
- `correct`：與人工答案一致為 `true`，不一致為 `false`，未標註或處理失敗為 `null`。
- `error`：處理失敗的原因；成功為 `null`。
- `result`：完整比對分數、分數差、插卡／關蓋條件等資訊。

損壞、無法讀取或尺寸不符的照片會列於 `IMAGE ERRORS`，也記錄在結果 JSON，
其餘照片繼續處理。未標註照片被判為不通過，不算「與答案不一致」。

## 可選參數

```bash
python scripts/image_match_test.py --image "照片資料夾" \
  --labels "人工答案.json" \
  --json "本次結果.json" \
  --no-overlay
```

`--labels` 指定人工答案檔，不存在時自動建立；`--json` 指定結果檔。
`--config` 仍指定比對參數與模板位置，與人工答案檔用途不同。
`--no-overlay` 不寫標註圖片，仍產生答案與結果 JSON；
`--output` 可指定標註圖片輸出資料夾。

程序結束碼：`0` 為成功且無已標註誤判；`1` 為有誤判、圖片處理錯誤或無成功比對；
`2` 為參數或人工答案格式錯誤。
