# 三色燈號指示設計（Light Tower Schema）— PATLITE LR5-LAN / PNS

## Context

燈塔改用 **PATLITE LR5-LAN**，走 **PNS 協定**（TCP binary，非 ZLAN Modbus 繼電器）。
實體塔為 **紅 / 琥珀(黃) / 綠 三色**；藍 / 白 通道與蜂鳴器不使用（恆為 OFF / Stop）。

本專案既有燈號邏輯（`vision_core.py:1680` `set_stack_light`、`vision_core.py:1876` `apply_sop_output`）
是給 ZLAN 四通道繼電器用的布林 on/off，只由 SOP 引擎狀態驅動，**不適用** PATLITE；PATLITE 改由新的 `light_tower.py` 驅動（見文末「實作」）。

**目標**：把四個使用者層級事件 — 相機開啟、自動拍照開始、判定逾時、判定 PASS — 對應到
PATLITE 燈號 pattern。本文件定義 schema、協定對應與實作位置。

---

## PNS 協定摘要（依原廠範例）

- TCP 連線：`socket.connect((ip, 10000))`，預設 IP `192.168.10.1`。
- 封包 header：`struct.pack('>2ssxH', b'AB', cmd, data_size)`，`AB`＝product category。
- 指令 `cmd`：
  - `b'S'` 動作控制（run control）— 後接 **6 bytes** 資料區：`[Red][Amber][Green][Blue][White][Buzzer]`
  - `b'C'` 清除（clear）— 全燈熄、蜂鳴停，data_size=0
  - `b'G'` 讀狀態（get status）
- 回應首位元組：`0x06`＝ACK 正常；`0x15`＝NAK，應 raise。

### LED pattern 碼

| code | 意義 |
|---|---|
| `0` | OFF |
| `1` | ON 恆亮 |
| `2` | 慢閃 blinking(slow) |
| `3` | 中閃 blinking(medium) |
| `4` | 快閃 blinking(high) |
| `5` | 單閃 flashing single |
| `6` | 雙閃 flashing double |
| `7` | 三閃 flashing triple |
| `9` | no change 不改變 |

蜂鳴器碼：`0`=Stop、`1`=Ring、`9`=no change。本專案一律 `0`（靜音）。

> 只有三色，綠燈需同時表達「待命」與「PASS」兩種意義 —
> 以 **閃爍 vs 恆亮** 區分：**閃綠＝等待中，恆綠＝PASS**。

---

## 事件 → PNS 動作控制指令（`S`）

資料區 6 bytes 順序：**`[Red][Amber][Green][Blue][White][Buzzer]`**

| 事件 | R | A | G | B | W | Buz | 資料區 | 操作員解讀 |
|---|---|---|---|---|---|---|---|---|
| **相機開啟**（待機、串流中、無任務） | 0 | 0 | 2 | 0 | 0 | 0 | `00 00 02 00 00 00` | 綠慢閃＝就緒，等待進料 |
| **自動拍照開始**（掃入 MCIO SN，20 秒窗口） | 0 | 2 | 0 | 0 | 0 | 0 | `00 02 00 00 00 00` | 琥珀慢閃＝拍照進行中 |
| **判定逾時**（窗口內未 PASS / 拍照失敗） | 4 | 0 | 0 | 0 | 0 | 0 | `04 00 00 00 00 00` | 紅快閃＝需操作員介入 |
| **判定 PASS** | 0 | 0 | 1 | 0 | 0 | 0 | `00 00 01 00 00 00` | 綠恆亮＝OK |

四狀態互斥，每次送**完整 6-byte 絕對指令**（不用 `9` no-change delta），
避免前一輪殘留通道卡在亮著。系統啟動 / 關閉 / 重置時送 `C` clear 全熄。

對應原廠範例呼叫（verbatim 參數順序）：

```
S  <R> <A> <G> <B> <W> <Buz>
相機開啟      → S 0 0 2 0 0 0
自動拍照開始  → S 0 2 0 0 0 0
判定逾時      → S 4 0 0 0 0 0
判定 PASS     → S 0 0 1 0 0 0
重置 / 熄燈   → C
```

---

## 色彩語意

- **綠閃 → 綠恆亮**：整條 happy path 都是綠色，有動作（閃）＝進行/等待，恆亮＝完成可取件。
- **琥珀＝拍照進行中**：對應 overlay 在 `WAITING_CAPTURE`/`JUDGING` 畫的琥珀色（`workflow_view.py:88`）。
- **紅快閃＝唯一告警態**（逾時）。
- **藍 / 白 / 蜂鳴器**：本站不使用，保留給未來跨週期訊號（如 SFIS / 網路離線）。

---

## 實作（已落地）

- **driver**：`light_tower.py` 的 `PatliteTower` — 照原廠範例組 `S`/`C` 封包，單一背景執行緒持有 socket；
  `request()` 只記下想要的燈號就返回（latest-wins、相同燈號不重送），不阻塞相機迴圈。
  連線失敗或 NAK 會記錄一次警告、每 2 秒重試，恢復後自動補送目前燈號。閃爍由 PATLITE 自身 pattern 處理。
- **觸發**：`edge_runtime.py` `_light_tower_tick()` 在每次推論、auto-capture probe 之後依站台狀態挑事件
  （`light_tower.select_event`）：
  - `PASS_CONFIRM` / `UPLOADING` → `judge_pass`
  - `NOT_MATCHED` **且**本週期是 auto-capture 視窗逾時 → `judge_timeout`
    （手動拍照不符、`RECOVERY_REQUIRED` 不亮紅燈，回到 `camera_on`）
  - probe 已啟動的 `WAITING_CAPTURE`、或 `JUDGING` → `auto_capture_start`
  - 其他相機運作中的狀態 → `camera_on`
  - 相機停止 → 送 `C` 全熄
- **config**：`config/apl_workflow.json` 的 `light_tower` 區塊，**預設 `enabled: false`**。
  IP 會變動，所以 `host` / `port` 寫在 config；改 IP 後重啟服務即可。
  每個事件可寫 `comment` 說明，`red/amber/green/blue/white` 取 0–7、9，`buzzer` 取 0、1、9；
  沒寫的通道為 0。站台啟動時驗證，設定錯誤會直接拒絕啟動。
  狀態可在 runtime status 的 `light_tower`（`connected` / `event` / `error`）看到。

```json
"light_tower": {
  "enabled": false,
  "host": "192.168.10.1",
  "port": 10000,
  "timeout_sec": 1.0,
  "events": {
    "camera_on": {"comment": "...", "red": 0, "amber": 0, "green": 2},
    "auto_capture_start": {"comment": "...", "red": 0, "amber": 2, "green": 0},
    "judge_timeout": {"comment": "...", "red": 4, "amber": 0, "green": 0},
    "judge_pass": {"comment": "...", "red": 0, "amber": 0, "green": 1}
  }
}
```
