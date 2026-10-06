# SFIS Step1 提前到 product SN、失敗改為保留 SN 可重試

## Context

現在 snapshot 模式（`single_snapshot_cover_card`）的流程是：掃 product SN → 掃 MCIO SN → 才送 SFIS Step1。但 Step1 的 request body 從來就不含 MCIO 序號（`apl_station.py:224` 只有 `sn` / `cycle_id` / `request_id` / `station_id` / `emp_no`），等 MCIO 掃完才驗沒有任何技術理由，而且失敗時操作員已經白裝了一張卡。

更嚴重的是失敗處理：`_gate_step1()`（`apl_station.py:562`）把 cycle 標成 `TERMINATED` 並呼叫 `_clear_cycle()`，兩個序號全部清空回 `WAITING_SN`；若錯誤訊息命中 `NO EMP` 還會連簽入一起清掉。實務上 SFIS 對 line=TEST / station=AI-CAM 的工號常回 `NO EMP`，等於每次失敗都要重掃全部標籤並重新簽入。

**目標**：掃完 product SN 就立刻送 Step1；失敗時保留 product SN 與 cycle，重掃同一張 product SN 即重送 Step1；`NO EMP` 不做任何清除，只給可行動的提示。不加 config 開關，直接改並更新測試與文件。

新流程：
```
簽入 → 掃 product SN → Step1 ─┬─ 接受 → WAITING_MCIO_SN → 掃 MCIO → WAITING_CAPTURE
                              └─ 失敗 → SN_CONFIRM (sticky)：保留 sn/cycle/簽入
                                        ├─ 重掃同一張 product SN → 重送 Step1
                                        ├─ 改工號（Switch operator）後再重掃
                                        └─ Terminate Inspection → 清空回 WAITING_SN
```

---

## 設計決定

**重試狀態沿用 `SN_CONFIRM`，不新增狀態名。** `_confirm()` 的 `expected`（`apl_station.py:597`）已經是 `SN_CONFIRM`，失敗路徑就是 `self.state = expected`（`apl_station.py:656`）；請求進行中是 `WAITING_DMZ` 而非 `SN_CONFIRM`，所以「進行中」與「卡住待重試」本來就分得開。新狀態名要動 `STATION_LABELS`/`STATION_HINTS`、`workflow_view.PHASE_LABELS`、`camera_fault`、`terminate`、`can_terminate`、`_recover` 與 `tests/test_camera_lifecycle.py:138`（它斷言 `stop.call_count == 9`）等約 8 處，換不到任何行為。

**sticky 判別式：`state == "SN_CONFIRM" and bool(last_error) and request_token is None`。** 這個選擇是刻意的，不是美觀問題 — `scan()` 在 `_accept_sn_locked` 與 `_gate_step1` 之間會放開 `self.lock`（`apl_station.py:497`-508）。`_accept_sn_locked` 在放鎖前就把 `last_error` 清空，所以落在那個窗口的「改工號」或「第二次重掃」都會被拒 / 被靜默吞掉，不會出現 empNo 撕裂。若改用 `request_token is None` 當判別式，這個窗口就是全開的。在 Python 端用一個新的 snapshot 欄位 `step1_retry_required` 對外曝光，UI 與 server 都不要自己重算。

---

## 實作步驟（依相依順序）

### 1. `apl_station.py` — 狀態機

**`_accept_sn_locked()`（`apl_station.py:510`）** 改成回傳 `(reply, gate_cycle)` 二元組（每一個 `return` 都要改），並在**重複掃描守衛之前**插入重試分支 — 順序是關鍵，否則 `apl_station.py:513` 的 `sn in {self.sn, self.mcio_sn}` 會把重掃當成靜默成功吞掉：

1. legacy no-op（:511-512）不動
2. **新增重試分支**：`snapshot_mode and state == "SN_CONFIRM" and self.sn == sn and self.last_error and request_token is None` → 重新產生 `sn_request_id`、清空 `last_error`、journal `SN_RESCANNED_FOR_STEP1_RETRY`、回傳 `(reply, self.cycle_id)`
3. 重複守衛（:513-516）不動 → 現在也順便涵蓋 `SN_CONFIRM (進行中)` 與 `WAITING_DMZ`，正是我們要的「在途中重掃就吞掉」
4. 鎖定拒絕（:517-518）、工號門檻、prefix 檢查都不動
5. 把 `auto_sfis_check = self.snapshot_mode and not self.local_mode and not self.test_mode`（原 `apl_station.py:557`）搬進來，狀態設為 `SN_CONFIRM if auto_sfis_check else "WAITING_MCIO_SN"`
6. `return self._reply(), (self.cycle_id if auto_sfis_check else None)` — legacy 模式 `auto_sfis_check` 為 False，Step1 仍由 `confirm_sn()` 驅動，不受影響

每次重試都換新的 `sn_request_id`：Step1 沒有 outbox 列（Step4 重用 `report_request_id` 是因為 outbox 以它為鍵，`apl_station.py:217`），而重試可能帶**不同的 empNo**，重用 id 會有被 SFIS 以 request_id 去重、把已修好的單再拒一次的風險。

**`_accept_mcio_locked()`（`apl_station.py:546`）**：刪掉 `auto_sfis_check`，狀態一律 `WAITING_CAPTURE`，回傳 `(reply, None)`。不再接觸 SFIS。

**`scan()`（`apl_station.py:492`）**：兩個分支都解二元組；`:502` 的路由不用改（重試狀態不是 `WAITING_MCIO_SN`，自然落到 `_accept_sn_locked`）。

**`_confirm()`（`apl_station.py:596`）**：只改 `:670` 一行 — 成功時 snapshot 模式進 `WAITING_MCIO_SN`（原本是 `WAITING_CAPTURE`）。`expected`/`pending` 維持 `SN_CONFIRM`/`WAITING_DMZ`。順便給 `:636` 的 `SN_CHECK_REQUESTED` 加上 `{"request_id": payload["request_id"]}`，同一個 cycle 的兩次嘗試才分得出來。

**`_gate_step1()`（`apl_station.py:562`）** 失敗分支整段重寫：刪掉 `_save("SFIS_STEP1_REJECTED", state_override="TERMINATED")`、`_clear_cycle()`、`self.operator = ""`。保留的只有：
- `_is_operator_error(error)`（`apl_station.py:439`）仍用來挑訊息。命中時 `last_error` 換成可行動的文字：請按 Switch operator 改工號，再重掃同一張 product SN；後面附上 SFIS 原文
- `_record_sfis({"step": "step1", "phase": "retry_required", "operator_error": ..., "hint": ...})`

這裡不另外 `_save`：原始 SFIS 文字已經由 `_confirm` 的 `SN_CHECK_FAILED`（`apl_station.py:657`）與 `failed` trace 落地，多寫一次只是多一個儲存失敗面。

**`_snapshot_single()`（`apl_station.py:323`）**：
- 新增 `step1_retry = state == "SN_CONFIRM" and bool(last_error) and request_token is None`，並以 `"step1_retry_required"` 對外曝光
- `can_terminate`（`apl_station.py:351`）集合加入 `"SN_CONFIRM"` — **這是避免死局的關鍵**，`can_reset` 跟著生效
- `active_scan_field`（`apl_station.py:357`）在 `step1_retry` 時回 `"sn"`；`SN_CONFIRM(進行中)` 與 `WAITING_DMZ` 仍回 `""`，不邀請在途中掃描

**新增 `_operator_editable_locked()`，改 `set_operator` / `clear_operator`（`apl_station.py:444`-468）**：`request_token` 在途中一律拒；允許 `WAITING_SN`，以及 snapshot 模式的 sticky 重試狀態。在重試狀態改工號時必須同步 `cycle_operator`（`_payload` 送出的 `emp_no` 來源，`apl_station.py:228`）並 journal `OPERATOR_CHANGED {from, to}`。`_payload` 是在 `_confirm` 的鎖內建構（`apl_station.py:631`），所以重試會原子地帶到新工號；`_save` 把 `cycle_operator` 寫進 `detail_json`，崩潰後也還原得到。

`clear_operator` 一定要一起放開：UI 只有按了 `opSwitch` → `/api/apl/operator/clear` 才會重新顯示工號輸入框（`static/apl_ui.js:285`、555-562），不放開的話「請改工號」這個提示無法執行。

**`terminate()`（`apl_station.py:692`）**：`:699` 的允許集合加入 `"SN_CONFIRM"`（與 `can_terminate` 同集合）。`:697` 的 `request_token` 檢查已經在前面，Step1 在途中按 Terminate 仍會被拒，不需額外處理。

**`_recover()`（`apl_station.py:144`）**：在 `:170` 之前插入 snapshot 分支 — cycle 列狀態為 `SN_CONFIRM` 或 `WAITING_DMZ` 時還原成 `SN_CONFIRM`，並寫入「重新掃描同一張 product SN 可重試 Step1，或終止」的 `last_error`。因為還原了 `last_error`，sticky 判別式自動成立，`can_terminate` / `active_scan_field` / `step1_retry_required` 開機就是對的，不需要多存一個欄位。legacy 模式不受影響（legacy 的 `SN_CONFIRM` 仍落到 `RECOVERY_REQUIRED`，因為 legacy 靠 Confirm SN 按鈕重試）。

**`set_test_mode()`（`apl_station.py:379`）刻意不動**：不要把 `SN_CONFIRM` 加進 `:383` 的集合。掛瀏覽器影片會覆寫 `cycle_profile`（`:387`），讓 `_same_transport()` 變 False，重送的 Step1 會永久失敗於「DMZ mode or endpoint differs」。加一行註解說明原因。

### 2. `workflow_view.py` — 玻璃上的 overlay

`current_phases`（`workflow_view.py:40`）與 AMBER 集合（`:47`）都漏了 `SN_CONFIRM`，重試期間 overlay 不會標示 CURRENT 步驟。兩處都加入 `'SN_CONFIRM'`，並把 `PHASE_LABELS['SN_CONFIRM']`（`:11`）從 `'Confirm SN'` 改成 `'Validating SN with SFIS'`（legacy 共用，兩邊讀起來都對）。

### 3. `visionedge_server.py` — `/api/apl/scan` 的 `expect` 門檻

`visionedge_server.py:311`-326 目前把 `expect` 硬對映到 `{'sn':'WAITING_SN','mcio_sn':'WAITING_MCIO_SN'}`，狀態不符回 409 — 重試狀態會被擋掉。改成以 snapshot 的 `active_scan_field` 判斷（snapshot 模式回 `'sn'`、legacy 沒有這個鍵就 fallback 回舊的狀態比對），這樣 server 與 UI 由同一個來源決定，不會各自算。未知 `expect` 的 `ValueError` 必須留在狀態讀取**之前**，`tests/test_api.py:52` 斷言的是 400 不是 409。`@station_command` 整個 view 持有 `_station_lock`（`:174`-186），讀取與 `scan()` 對 HID 路徑仍是原子的。

其餘路由不用改：`rearm_scanner_after_cycle`（`:248`）、scanner 重綁（`:997`）、`/api/edge/config`（`:499`）、`/api/edge/restart`（`:530`）都只認 `WAITING_SN`，重試期間擋住它們是正確的（出口是 Terminate）。HID 掃描路徑（`:207`-213）不帶 `expect`，直接進 `_accept_sn_locked` 的重試分支 — 這是產線上真正會走的路徑。

### 4. `static/apl_ui.js`

- 讀後端旗標 `const retry = station?.step1_retry_required === true`，**不要在 JS 重算判別式**
- `snEditable`（`:279`）與 `canEditOperator`（`:284`）加上 `|| retry`；`mcioEditable` 不動
- 新增 `STEP1_RETRY_LABEL` / `STEP1_RETRY_HINT`（重掃同一張 product SN 重試、或 Terminate；工號被拒時先按 Switch operator），在 `:308`-310 取代標籤與提示，並把 `retry` 加進 `:309` 的 `'bad'` 分支
- 修正兩段現在已經不成立的文案：`SN_CONFIRM` 的 hint 不再提 MCIO 卡；`WAITING_MCIO_SN` 的 hint 改成「product SN 已通過 Step1」
- `submitScan()`（`:523`）的 `station?.state !== spec.expect` 改成比對 `active_scan_field !== spec.field`；`activeScanStep()`（`:392`-398）同樣改讀 `active_scan_field`，保留工號優先規則
- 刪掉 `logStationState` 的 `rolledBack` 啟發式（`:204`-216）— 它偵測的 `WAITING_SN` + `last_error` 轉換不再發生，而且三句話（整個 cycle 被丟掉／兩個序號都要重掃／工號也被拒要重新登入）現在全是假的。改成偵測 `step1_retry_required` 由 false 翻 true 的邊緣，印出真正的復原方式
- `SFIS_PHASE_STYLE`（`:155`）：`rolled_back` → `retry_required`
- `stepStatuses()`（`:361`）：`done[3] = !!station.sn` 在重試期間為真，會把第 4 步（MCIO）畫成 active，但站台要的是 SN 欄位。改成 `!!station.sn && station.step1_retry_required !== true`
- Terminate 的 `confirm()` 文案（`:577`）：重試狀態下 `mcio_sn` 一定是空的，「Both serial numbers will be cleared」不對，改成單數
- **cache-buster 必須換**：`static/edge_dashboard.html` 的 `apl_ui.js?v=20261005-6-sfis-console` 與 `tests/test_ui_static.py:101` 一起改。舊 UI 配新後端會讓重試狀態的 SN 欄位是 disabled、只能 Terminate，所以這不是美觀問題，要寫進交接文件

### 5. journal 事件與 sfis trace phase

| 動作 | 名稱 |
|---|---|
| 移除 | `SFIS_STEP1_REJECTED`（原本只為了用 `state_override` 把列翻成 TERMINATED；`SN_CHECK_FAILED` 已記同樣的錯誤與時間） |
| 新增 | `SN_RESCANNED_FOR_STEP1_RETRY` `{source, request_id}` — 否則 journal 會出現同一 cycle 兩筆 `SN_CHECK_REQUESTED` 卻無法解釋中間發生什麼 |
| 新增 | `OPERATOR_CHANGED` `{from, to}` — 唯一能證明第二次 Step1 帶的是不同 empNo 的紀錄 |
| 更名 | sfis phase `rolled_back` → `retry_required`（`rolled_back` 現在是假的，且 dashboard 把它畫成終止錯誤）；entry 欄位 `operator_cleared` → `operator_error` |

---

## 要改的既有測試

跑法：`python -m unittest discover -s tests -p "test_*.py" -v`（純 stdlib unittest，無 pytest 設定）

- **tests/test_mcio_scan.py**：`:74`-84、`:86`-94（Step1 改在第一次掃描發生，transport 呼叫數與事件順序都變）；`:190`-202 的 rollback 測試與 `:204`-209 的 NO EMP 測試整個換掉；`:213`-228 的 local/test 模式補上「第一次掃描後就是 `WAITING_MCIO_SN` 且 `transport.calls == []`」
- **tests/test_sfis_client.py**：`:262`-274 改名與斷言；`:282`-297 重寫；`:299`-303 補 `sn` 與 state 斷言。`SFISTraceTests`（`:378`-411）有兩個斷言會因為 **Step1 時 `mcio_sn` 還不存在**而掛 — `_record_sfis` 從 `self.mcio_sn` 取預設值（`apl_station.py:407`），tuple 要從 `("K2V5-001","APL-001",...)` 改成 `("K2V5-001","",...)`；phase 序列改 `[begin, failed, retry_required]`；`operator_cleared` → `operator_error`。注意 `SFISTraceTests` 繼承 `SnapshotSFISGateTests`，基底類別的每個改動都會跑兩次
- **tests/test_snapshot_inspection.py**：`:197`-207 `test_failed_step1_releases_sn_for_rescan` 重寫成保留 SN 並重掃重試
- **tests/test_api.py**：`:40`-55 增加一個 `SN_CONFIRM` + `active_scan_field='sn'` + `expect='sn'` → 200 的案例
- **tests/test_ui_static.py**：`:101` cache-buster；補 `STEP1_RETRY_LABEL`、`step1_retry_required`、`active_scan_field` 的存在性斷言，以及 `assertNotIn("rolled_back", script)`

## 要新增的測試（tests/test_mcio_scan.py 的風格）

1. `test_product_scan_sends_step1_before_the_mcio_step` — 第一次掃描後恰好一次 `/sn/check`，payload **不含** `mcio_sn`，狀態 `WAITING_MCIO_SN`
2. `test_mcio_scan_never_sends_a_second_step1` — 兩次掃描後仍只有一次 transport 呼叫
3. `test_step1_failure_keeps_the_product_sn_in_a_sticky_retry_state` — `state=="SN_CONFIRM"`、`sn` 保留、`mcio_sn==""`、`step1_retry_required` True、`active_scan_field=="sn"`、`can_terminate` True、`can_capture` False、`operator` 保留、**cycles 列是 `SN_CONFIRM` 不是 `TERMINATED`**、`assertNotIn("SFIS_STEP1_REJECTED", events)`
4. `test_rescanning_the_same_product_sn_refires_step1` — 兩次 `/sn/check`、同 `cycle_id`、**不同 `request_id`**、最終 `WAITING_MCIO_SN`、journal 有 `SN_RESCANNED_FOR_STEP1_RETRY`
5. `test_a_different_product_sn_during_the_retry_is_still_refused` — 「The current SN is locked」、狀態與 `sn` 不變
6. `test_no_emp_keeps_the_operator_the_sn_and_the_cycle` — `operator` 與 `cycle_operator` 都還在、`sn` 與 `cycle_id` 都還在、`last_error` 提到工號
7. `test_changing_the_employee_number_during_the_retry_feeds_the_next_step1` — NO EMP → `clear_operator()` → `set_operator("TW031561")` → 重掃 → `calls[-1][1]["emp_no"] == "TW031561"`、journal 有 `OPERATOR_CHANGED`
8. `test_the_operator_and_terminate_are_refused_while_step1_is_in_flight` — 在 patch 過的 `transport.send` 內部斷言（照 tests/test_snapshot_inspection.py:111-126 的手法）：狀態 `WAITING_DMZ`、`set_operator`/`clear_operator`/`terminate` 全部失敗、`can_terminate` False
9. `test_a_repeat_scan_while_step1_is_in_flight_is_a_silent_no_op`
10. `test_terminate_clears_a_failed_step1_cycle` — 從重試狀態 Terminate → `WAITING_SN`、`sn==""`、列 `TERMINATED`
11. `test_a_cycle_interrupted_at_the_step1_gate_reopens_as_a_retry` — 失敗後 `close()` → `open_station()` → 還原成重試狀態、`sn` 保留、可 Terminate、重掃可過
12. `test_a_cycle_interrupted_mid_step1_reopens_as_a_retry` — 把列強制成 `WAITING_DMZ` 再重開（把 `_recover` 的決定釘住）
13. `test_local_mode_reaches_the_mcio_step_without_any_step1`
14. `test_a_step1_retry_cannot_capture_or_upload`
15. `test_a_browser_video_cannot_be_attached_during_a_step1_retry` — `set_test_mode(True)` 丟 `ValueError`（釘住上面刻意不動的決定）

---

## 要更新的文件

全部都寫著舊順序：

- `docs/HANDOVER_ZH.md:174`（「兩個 SN 都掃完後才呼叫」）、`:180`（「Step1 失敗會回到 WAITING_SN 並同時清掉兩個 SN…清除簽入」）、`:60`-61（狀態表兩列的語意都反了）、`:193`-197（升級注意事項 — 加新 cache-buster 與「舊 UI 會讓 SN 欄位卡在 disabled」的警告）
- `README_APL_ZH.md:35`-38（ASCII 流程圖）、`:54`（NO EMP 清簽入的規則）、`:61`-62
- `docs/SCANNER_OPERATOR_FLOW_ZH.md:11`-13（步驟 3、4 的順序）、`:17`（路由句要加重試狀態）、`:19`（「兩個序號都會清空」在 Step1 關卡不成立，那裡只有一個）

---

## 風險與要一併知道的副作用

**Step1 的重試次數現在無上限，且可能帶不同 empNo。** transport error 與業務拒絕在 `_confirm` 裡無法區分（都是 `transport.send` 丟出的 `DMZError`，`apl_station.py:643`），所以 timeout 之後站台不知道第一次嘗試有沒有到 SFIS；若操作員接著改工號重試，SFIS 可能對同一張 product SN 留下兩筆不同工號的 Step1 紀錄。計畫裡的三件事讓它可從站台 journal 對帳：每次重試換 `sn_request_id`、`SN_CHECK_REQUESTED` 記 `request_id`、`OPERATOR_CHANGED` 記工號變更。**要跟 SFIS 負責人確認 Step1 是純查詢還是有 side effect（例如報到）**；若有 side effect 可能需要 server 端規則。不建議加重試上限 — 無上限重試搭配一個看得見的 Terminate 比較簡單，也正是 Step4 現有的契約。

**現在可以「Step1 已被 SFIS 接受、但整個 cycle 被放棄」。** 舊流程在 `WAITING_MCIO_SN` 按 Terminate 是在任何 SFIS 接觸之前；新流程那個時點 Step1 已經過了。這個洞本來就存在（在 `WAITING_CAPTURE` 按 Terminate 就是如此），關卡提前只是把窗口擴大一步。寫進交接文件即可，不是阻礙。

**`started_at` 每次 Step1 成功都會重新戳一次**（`apl_station.py:671`），重試後 cycle 時長會低估在關卡上花的時間。本來就被戳兩次（`:532` 然後 `:671`），重試只是讓落差變明顯。要修就把 `:671` 用 `if self.started_at is None` 包起來。

**重試狀態下掃到 MCIO 標籤，會先撞到「SN 已鎖定」而不是 prefix 錯誤訊息**（`apl_station.py:517` 在 prefix 檢查之前）。訊息比 `WAITING_MCIO_SN` 階段不精確，但步驟面板與 hint 已經指出正確欄位，接受，並在 SCANNER_OPERATOR_FLOW_ZH.md 提一句。

**F12 console 的每一行 Step1 trace 現在都會顯示 `MCIO —`**（`static/apl_ui.js:165`），因為那時 MCIO SN 還不存在。正確但看起來不一樣，交接文件寫一行。

**可拆的一項（建議一起做）**：`camera_fault` 的 snapshot `active_states`（`apl_station.py:725`）加入 `"SN_CONFIRM"` 與 `"WAITING_DMZ"`。`WAITING_MCIO_SN` 已經在集合裡，Step1 關卡只是早一步的同一種情境，而且還沒拍任何照、沒有 outbox 列，沒有什麼值得保留。加入 `WAITING_DMZ` 會在請求在途時清掉 `request_token`，但遲到的回應仍會被 `_confirm` 的 `state != pending or request_token != token` 守衛丟棄（`apl_station.py:650`），安全。不做的代價只是操作員多按一次 Terminate。

---

## 驗證

1. `python -m unittest discover -s tests -p "test_*.py" -v` — 全綠（目前基線約 280 項，其中 4 項因缺參考影片 skip）
2. 以 bypass 開著（`config/apl_workflow.json` 目前 `dmz.bypass_sfis: true`）起站台，走一次完整流程：掃 product SN 應該立刻出現 `WAITING_MCIO_SN`（bypass 下 Step1 直接 accepted），掃 MCIO → `WAITING_CAPTURE`
3. 手動驗失敗路徑：把 `bypass_sfis` 改 `false` 指向真實 SFIS（依 memory，line=TEST / station=AI-CAM 的工號會回 `NO EMP`），掃 product SN → 應停在重試狀態，sn 留著、簽入留著、SN 欄位仍可輸入、Terminate 可按、F12 出現 `retry_required` trace 與可行動的提示
4. 同一狀態下按 Switch operator 換工號，再重掃同一張 product SN，確認 `/api/apl/sfis-log` 裡第二次 Step1 的 `emp_no` 是新工號、`request_id` 與第一次不同
5. 重試狀態下重啟站台，確認 `_recover` 還原成重試狀態而不是 `RECOVERY_REQUIRED`
6. HID 掃描器路徑單獨驗一次（它不帶 `expect`，走的程式路徑與瀏覽器不同）
