# Flow Designer 需求評估與實作紀錄

## 需求合理性

### 同時提供按鈕與拖拉排序

合理且建議保留兩者。拖拉適合快速調整長流程；↑／↓按鈕提供精準、可預期、鍵盤與遠端操作友善的替代方式。兩種操作最後都只改同一個 steps array，儲存時重新產生連續的 `step_no` 與 `sort_order`，不會形成兩套順序來源。

### 每個 Step 獨立設定多樣板

必要。單一 APP 或人工操作步驟可能有不同版本畫面、不同合法狀態，或需要同一畫面上的多個必要特徵。實作採用：

- 每個 Step 各自保存一組 OK／NG Template local copies。
- OK ANY：替代畫面。
- OK ALL：同一幀必要條件。
- NG ANY：任一命中立即異常。
- 同一 Template 可被不同 Step 重複引用。

## 本版額外修正

1. v1.1 原本已有 ↑／↓按鈕，但沒有拖拉排序；v1.2 新增 drag handle、drop indicator 與 Alt+方向鍵。
2. v1.1 已有每 Step 多樣板，但介面不夠明確；v1.2 將多樣板改為 Step 專屬 modal，加入來源產品／類型／文字搜尋、只看已選及批次選取。
3. 修正已複製的 local sample 在來源 Region 被刪除後，再次儲存可能遺失的問題。
4. 新增前後端雙重驗證：啟用 Step 必須至少有一個可用 OK Template。
5. 停用 Step 可作為 draft 保存，即使尚未選擇樣板也不影響 runtime。
6. Step/template 與 SOP config 改為單一 transaction 儲存。
7. 顯示 `ALL` 的真實語意為「同一幀全部命中」，避免被誤解成跨時間累積各 Template。

## 尚未實作的進階語意

目前 Step 內 `ALL` 是同一幀組合，不是子畫面依序出現。若要表示：

```text
Step 3：先看到 Testing → 再看到 PASS
```

應拆成兩個 Step，或未來新增 Step 內部的 sub-state sequence。不要用目前的 ALL 代替跨時間順序。
