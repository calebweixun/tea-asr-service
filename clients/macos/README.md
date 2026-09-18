# TEA ASR Mac Client

選單列常駐的 macOS client，連本機的 TEA ASR 服務，提供兩種用法：

- **聽寫**：講完一句就把定稿文字貼進目前有焦點的 app。
- **會議記錄**：開一個視窗即時累積逐字稿，隨時存成 Markdown。

沒有 Dock 圖示（`LSUIElement`），只在選單列出現。

## 建置

```bash
./scripts/build-app.sh
```

用 Command Line Tools 的 Swift，**不需要接受 Xcode 授權條款，也不需要 sudo**。
產物是 `build/TEA ASR.app`，採 ad-hoc 簽章：本機可執行，但不能發給別人
（那需要開發者憑證與公證）。

```bash
open "build/TEA ASR.app"
```

## 使用前

服務要先跑起來，client 從 `~/Library/Application Support/TEA ASR/token` 讀 bearer token：

```bash
uv run tea-asr serve
```

## 權限

| 權限 | 什麼時候要 | 沒有會怎樣 |
|---|---|---|
| 麥克風 | 開始聆聽時 | 無法錄音，會跳說明 |
| 輔助使用 | 只有「自動貼上」需要 | 定稿文字仍會放進剪貼簿，你自己貼 |

輔助使用要手動到「系統設定 → 隱私權與安全性 → 輔助使用」打開。
每次用 `build-app.sh` 重建都是同一個簽章識別碼，所以權限不會每次重問。

## 自測（不需要任何權限）

把一段 16 kHz mono WAV 走完整個 client 路徑，驗證 wire framing、流控與 session 狀態機：

```bash
.build/release/TeaASRClient --selftest /path/to/16k-mono.wav
```

加 `--no-realtime` 會用最快速度送，通常會撞到服務的流量窗口；
加 `--preview` 要求串流預覽（服務要以 `TEA_ASR_EXPERIMENTAL_REVISABLE_PREVIEW=1` 啟動才會被接受）。

## 設定

選單列 →「設定…」可以改服務位址與 port、開關自動貼上與串流預覽，並直接測試連線。
會議記錄每收到一段定稿就自動寫入
`~/Library/Application Support/TEA ASR/meetings/meeting-<時間>.md`，
所以就算 app 當掉或忘記按存檔，內容也還在。

## 設計上的取捨

- **只有 `final` 會被貼出去。** partial 只更新會議視窗自己的那一行，不會去刪使用者已經打的字。
- **流量窗口滿了就中止，不丟音訊。** 丟 frame 會讓時間軸悄悄壓縮，
  server 看到的會是一段從未發生過的連續錄音；[docs/04](../../docs/04-api.md) 禁止這種事。
- **重採樣用 AVAudioConverter**，不是自己寫的線性內插（[docs/03](../../docs/03-architecture.md) 的要求）。
- **剪貼簿會還原**：貼上後把原本的內容放回去，長時間聽寫不會一直吃掉你的剪貼簿。
- **不做 VAD 或斷句**：那是服務的職責，兩邊各做一份只會讓 sample clock 對不上。

## 尚未做

- 沒有輸入法層級的組字區整合（docs/06 的 P5b），所以聽寫是「定稿後貼上」，不是游標處即時修訂。
- 沒有簽章公證，無法直接發給別人。
- 長時間會議的記憶體與穩定性尚未實測。
