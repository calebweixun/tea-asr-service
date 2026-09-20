# TEA ASR Mac Client

選單列常駐的 macOS client，連本機的 TEA ASR 服務，提供兩種用法：

- **聽寫**：講完一句就把定稿文字貼進目前有焦點的 app。
- **會議記錄**：開一個視窗即時累積逐字稿，隨時存成 Markdown。

沒有 Dock 圖示（`LSUIElement`），常駐選單列；直接開啟 app 或再次從 Finder
開啟時會顯示同一個主畫面，不會建立重複視窗。

## 建置

```bash
./scripts/build-app.sh
```

用 Command Line Tools 的 Swift，**不需要接受 Xcode 授權條款，也不需要 sudo**。
產物是 `build/TEA ASR.app`。腳本會優先使用 Keychain 裡的
`Developer ID Application` 憑證；沒有憑證時仍會 fallback 到 ad-hoc，讓本機可以直接開發，
但 ad-hoc 只能本機執行，不能發給別人（那需要開發者憑證與公證）。

也可以明確指定簽章身分：

```bash
TEA_ASR_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" ./scripts/build-app.sh
```

```bash
open "build/TEA ASR.app"
```

## 使用前

服務要先跑起來，client 從 `~/Library/Application Support/TEA ASR/token` 讀 bearer token：

```bash
uv run tea-asr serve
```

## 文字處理邊界

每個 server `final` 都依序保留 `rawTranscript`、產生 `cleanedText`，再產生最後貼上的
`pasteText`，並記錄實際套用且依序發生的 `appliedSteps`。目前預設 pipeline 是嚴格 no-op，
因此不改動任何文字；partial 只更新預覽，不會進入清理或貼上流程。segment、sample、revision
與時間戳 metadata 也會隨 final 保留，處理規則只在本機執行，不回寫 server。

## 權限

| 權限 | 什麼時候要 | 沒有會怎樣 |
|---|---|---|
| 麥克風 | 開始聆聽時 | 無法錄音，會跳說明 |
| 裝置控制和資料取用 | 只有「自動貼上」需要 | 定稿文字仍會放進剪貼簿，你自己貼 |
| 輸入監控 | 只有「按住說話」模式需要 | 按住說話不會啟用，設定頁會引導開啟 |

「裝置控制和資料取用」是 macOS 26 起的名稱，macOS 25 以前叫「輔助使用」；app 會依實際執行的
macOS 版本顯示正確名稱。要手動到「系統設定 → 隱私權與安全性 → 裝置控制和資料取用」打開
（設定 URL anchor 仍是 `Privacy_Accessibility`，沒有跟著改名）。
從系統設定回到 TEA ASR 後，app 會立即並在短暫延遲後重新檢查權限；若 macOS
仍回報未授權，app 會明確提示完全結束後重新開啟，而不會把未確認的狀態顯示成已允許。

TCC 會綁定程式的簽章身分，不是只看 app 路徑。使用 Developer ID 時，授權可跨重建保留；
ad-hoc 的 designated requirement 會以 `cdhash` 識別，改動 binary 後可能需要：

1. 完全結束 TEA ASR。
2. 在「裝置控制和資料取用」清單移除舊的 TEA ASR 項目，再加入目前的 `build/TEA ASR.app`。
3. 重新啟動 app，回到「診斷與權限」頁的「權限」群組按「重新檢查權限」。

若剛授權但仍顯示未允許，先完成一次完整退出／重開；這是 macOS 對目前程式簽章身分的
真實回報，不代表 app 可以安全地假設授權已生效。

## 自測（不需要任何權限）

把一段 16 kHz mono WAV 走完整個 client 路徑，驗證 wire framing、流控與 session 狀態機：

```bash
.build/release/TeaASRClient --selftest /path/to/16k-mono.wav
```

加 `--no-realtime` 會用最快速度送，通常會撞到服務的流量窗口；加 `--preview` 要求串流預覽；
`--url ws://127.0.0.1:PORT/v1/stream` 可以指向別的服務（測試用）。

## 圖示

app 圖示與選單列圖示都由 `Resources/icons/` 提供，來源圖在 `tools/source-*.png`。
要換圖或重新產生：

```bash
./scripts/make-icon.sh [來源.png]
```

腳本會裁掉來源自帶的外框與描邊、套上 Apple 的 squircle 遮罩讓四角真的透明、
內縮到 macOS 的比例，再輸出 16–1024 全尺寸與 `.icns`，並重畫選單列的 template。

選單列**不是**把 app 圖示縮小：`tools/make-menubar-icon.py` 會從來源稿
保留原本的實心杯子、蒸氣與錯誤徽章，依狀態輸出閒置、聆聽中、錯誤三張
template，由系統依深淺色自動染色。

這條管線只依賴 Python 與 Pillow，不需要 Swift——圖示資產不該因為 Xcode 壞掉就做不出來。

## 設定

選單列 →「設定…」可以改服務位址與 port、輸入裝置／聲道、全域快捷鍵、互動模式（切換或按住說話）及開始／停止系統提示音，並直接測試連線。
快捷鍵以穩定的 key code 與修飾鍵旗標保存，不會把原本的 ⌥⌘D 寫死；按住說話會在 key-down 開始、相同按鍵 key-up 停止，重複 key-down 會忽略。
按住說話需要「系統設定 → 隱私權與安全性 → 輸入監控」，切換模式不需要這個權限。
聽寫時的 partial 與狀態顯示在 TEA ASR 自己的非啟用浮動預覽，不會搶走前景 app 焦點；只有 final 會插入，而且每個 segment 只插入一次。
會議記錄每收到一段定稿就自動寫入
`~/Library/Application Support/TEA ASR/meetings/meeting-<時間>.md`，
所以就算 app 當掉或忘記按存檔，內容也還在。

## 設計上的取捨

- **只有 `final` 會被貼出去。** partial 只更新會議視窗自己的那一行，不會去刪使用者已經打的字。
- **預覽只進自己的視窗。** 串流預覽由設定開關決定，聽寫的 partial 只顯示在自有的非啟用浮動預覽，
  會議的 partial 只顯示在會議視窗；兩者都不會進入貼上路徑，final 才會插入。
- **流量窗口滿了就中止，不丟音訊。** 丟 frame 會讓時間軸悄悄壓縮，
  server 看到的會是一段從未發生過的連續錄音；[docs/04](../../docs/04-api.md) 禁止這種事。
- **重採樣用 AVAudioConverter**，不是自己寫的線性內插（[docs/03](../../docs/03-architecture.md) 的要求）。
- **剪貼簿會還原**：貼上後把原本的內容放回去，長時間聽寫不會一直吃掉你的剪貼簿。
- **不做 VAD 或斷句**：那是服務的職責，兩邊各做一份只會讓 sample clock 對不上。
- **時間軸缺口會自動續錄**：機器睡醒後服務會中止 session（v0.1 沒有 resume），
  client 自動開新 session 繼續錄，並在會議逐字稿裡用橘色標出那道缺口，而不是假裝沒發生。
  會議時間戳用絕對時間計算，所以換 session 之後仍在同一條時間軸上。

## 尚未做

- 沒有輸入法層級的組字區整合（docs/06 的 P5b），所以聽寫是「定稿後貼上」，不是游標處即時修訂。
- 沒有簽章公證，無法直接發給別人。
- 長時間會議的記憶體與穩定性尚未實測。
