# 10｜接手交接（2026-09-19）

這份文件寫給**下一個接手開發的模型或人**。它不重複 [03 架構](03-architecture.md)、
[04 API 契約](04-api.md)、[06 交接](06-handoff.md) 已經寫清楚的東西，只記錄
**現況、已定案的判斷、以及你接手時最可能踩到的坑**。

規格優先順序不變：使用者最新明確指示 → 04 wire 契約 → 03 架構 → 06 開發次序。

---

## 1. 現在能跑什麼

| 元件 | 狀態 |
|---|---|
| Server（`src/tea_asr/`） | P0–P3、P2a 完成並實測；全 Unicode PUA 過濾與兩個實測競態修復完成。143 個測試通過（`uv run pytest tests/unit tests/integration -q`） |
| Mac client（`clients/macos/`） | 可用：聽寫、會議記錄、選單列狀態、服務啟停；M0 typed `AppState`／`ServiceProbe` 完成。原生 unified macOS UI（主視窗、狀態、權限、設定、診斷與逐字稿）已完成並通過目前 client 測試；P5a 的收音裝置／聲道、快捷鍵／PTT、feedback、non-activating overlay 與 deterministic 後處理仍按 [06](06-handoff.md) 驗收，P5b 尚未開始 |
| OBS 外掛（另一個倉庫） | Phase A 完成：錯誤與連線狀態只在 Tools／設定診斷，不進字幕畫布；新 source 預設 Fixed 960px，舊 source migration 保留 Auto；CJK effective defaults 已修復。strict two-line layout 延至 Phase B |
| P4 長檔案與保存 | 未開始，`/v1/jobs` 回 404 |

啟動服務：

```bash
cd /Users/c2leb/Codes/tea-asr-service && uv run tea-asr serve
```

健康檢查端點是 **`/healthz`**（不是 `/health`）。服務只聽 `127.0.0.1:8327`。

### 1.1 Mac client 的文字與保存邊界

Mac client 收到 server 的 final 後，依序使用以下概念；server 的 final、
`segment_id`、`start_sample`／`end_sample`、revision 與 timestamp quality 都是
不可變資料：

- `rawTranscript`：server final event 的 `text`，作為 client 後處理唯一輸入；server
  另外提供的 `raw_text` 只供 PUA／模型診斷，不直接貼入。
- `cleanedText`：client 端 deterministic 規則（例如明確 opt-in 的字典／格式化）產生
  的文字；規則不回寫 server，也不改 wire metadata。
- `pasteText`：依目標 app 與輸出政策，由 `cleanedText` 產生的最後貼上文字。
- `appliedSteps`：按順序記錄實際套用的規則 ID；若沒有規則則為空，不以猜測結果補寫。

P5a 只允許本機、可重現的 deterministic 後處理，不引入 LLM 或 cloud formatter。
partial 只能更新自己的預覽；只有 final 的 `rawTranscript` 才能進入
`cleanedText`／`pasteText` 與貼上流程。目標保存邊界是聽寫歷史預設關閉；meeting
autosave 只在使用者明確開始的 meeting session 內啟用。**目前實作尚未符合這條邊界：**
`MainWindowController` 的 dictation final 也會經 `appendTranscriptEntry` 呼叫
`autosaveTranscript`，寫入 `~/Library/Application Support/TEA ASR/meetings/`，因此目前仍會
自動保存聽寫 final。這是待後續 phase 修正的 code issue，不是已完成的 opt-in 保證。未來若
提供文字歷史，必須 opt-in、顯示 retention 與 delete；音訊歷史永不預設保存。這項保存邊界
修正與 P5b（InputMethodKit 組字／游標處修訂）分開，P5b 仍按 [06](06-handoff.md) 驗收
且尚未開始。

---

## 2. 這一輪定案的事（不要重新推翻，除非有新證據）

### 2.1 私用區字元（PUA）的根因已定位：MLX 量化造成

同一批語料、逐筆同 key 對齊的實測：

| 模型 | 含 PUA 句子 | 大小 | Peak RSS | 平均延遲 |
|---|---:|---:|---:|---:|
| 上游 BF16 `JacobLinCool/TEA-ASR-1.1` | **0.0%** | 3.8 GiB | 12.53 GiB | 0.583s |
| `Alkd/TEA-ASR-1.1-MLX-4bit`（生產用） | 70.0% | 1.2 GiB | 1.56 GiB | 0.129s |
| 自轉 MLX 4bit | 70.0% | 1.5 GiB | 1.85 GiB | 0.122s |
| 自轉 MLX 8bit | 63.3% | 2.3 GiB | 2.71 GiB | 0.155s |

**已排除的可能**：不是 decode bug（tokenizer vocab 裡沒有 PUA entry）、不是
Alkd 那一版轉換流程特有（自轉 4bit 與正式版輸出逐字相同）、不是位元寬不夠
（8bit 只降 6.7 個百分點，代價卻是體積 +92%、記憶體 +74%、延遲 +20%）。

**現行對策**：服務端預設過濾全部 Unicode 私用區（BMP U+E000–F8FF、Plane 15
U+F0000–FFFFD、Plane 16 U+100000–10FFFD；`ServiceConfig.filter_pua`，
`TEA_ASR_FILTER_PUA=0` 可關）。`text` 是過濾後結果，`raw_text` 保留原始辨識供除錯。整段被過濾成空字串時
送 `segment.skipped` + `reason="empty"`，不送假的空 final。

**這是權宜之計**。`filter_private_use_characters()` 的 docstring 寫了移除條件：
上游提供不會漏 PUA 的量化 checkpoint 時就拿掉。細節見
[PUA A/B 報告](benchmarks/pua-bf16-ab-report.md)、[8bit 續測](benchmarks/pua-bf16-ab-report.md)。

### 2.2 mini 模型：數字漂亮但暫不換

`JacobLinCool/TEA-ASR-1.1-mini` 實測 MER 比 full 版好（4bit 3.51% vs 4.82%），
體積/記憶體/延遲全面更優，但**量化後仍有 36.7% 的 PUA**，而且目前**沒有官方
MLX checkpoint**。不換的理由是 n=30 單一語料不足以定案，不是因為它不好。

要推進的話，缺的是：更大樣本的 MER 驗證 + docs/05 情境覆蓋 + 一個可發布的
量化 checkpoint。見 [mini 報告](benchmarks/mini-model-report.md)。

雙模型並用（mini 跑 partial、full 跑 final）技術可行、記憶體代價很小
（兩者常駐約 2.45 GiB），但需要真的動架構：`Scheduler` 目前只支援單一 worker、
`app.py` 只建一組 `WorkerSupervisor`/`Scheduler`、`StreamSession` preview 與
final 走同一個 scheduler。

### 2.3 併發上限已實測並真的有 enforcement

以前 `CapabilityLimits.max_continuous_sessions=1` 只是宣告值，**`/v1/stream` 根本
沒有拒絕邏輯**——兩個 client 同時連會兩個都成功。這是一個公開宣告與實作不符的缺口，
已補上。

實測（M4 Pro / 48 GiB，N=1..4 並行 continuous session，每個串 90 秒真實語音）：
零錯誤、backlog 每次都收斂、品質不隨 N 劣化；延遲 p50 0.42→1.09s、p95 1.25→2.08s；
HTTP `interactive` 在 N=4 下仍 100% 成功、p95 ≤0.4s（證明 scheduler 的
`interactive > realtime > preview` 優先權有效）；worker RSS 平在 ~1.5 GB。

**預設值設 2，量到的安全上限是 4**——這是單人本機服務的延遲取捨，不是安全上限。
可用 `config.toml` 的 `[service]` 調整。

新錯誤碼 **`concurrent_session_limit`**，HTTP 429、**WS close 4029**，`retryable=true`。
刻意給獨立的 close code，因為 `queue_full`/`session_limit`/`slow_client` 三者共用
1013，client 無法分辨——那正是 OBS 外掛誤判的根源（見 §4.1）。**1013 三碼共用這件事
本身還沒修**，是已知的既有問題。

### 2.4 模型不放 cache

macOS 曾經把 `~/Library/Caches/TEA ASR/` 整個清掉（1.2 GB 模型消失，服務外觀仍
健康）。現在放 `<repo>/models/`（gitignore），`TEA_ASR_MODELS_DIR` 可改，
`model_spec.py` 的 `default_model_cache()` 是決策點，並有測試擋住路徑退回
`~/Library/Caches`。

---

## 3. 環境上的坑（會直接卡住你）

- **CLT 的 Swift 可能壞掉**。macOS 更新後 Command Line Tools 的 `swift-package`
  會因缺符號直接 abort。`clients/macos/scripts/build-app.sh` 會自動探測並退回
  Xcode 工具鏈；如果你自己下 `swift build` 卡住，這就是原因。
- **系統 Python 的 Pillow 會被更新清掉**。圖示腳本改用 `uv run --with pillow`，
  不依賴系統裝了什麼。
- **選單列圖示可能看不到**。使用者開著 Thaw（選單列管理工具）時會把它藏到螢幕外，
  AX 會回報 `x = -1`。這不是 app 的 bug；排查前先確認 Thaw 是否在跑。
- **不要在 OBS 外掛倉庫跑本機 cmake**。它會把整份 OBS source 下載進 `.deps/` 汙染
  倉庫。所有建置一律走 GitHub Actions。
- **port 8765 被使用者自己的服務長期佔用**，本專案固定用 8327。

---

## 4. 待辦（依重要性）

### 4.1 OBS 外掛：4029／CJK 預設字型修復送 CI ★最高

倉庫 `/Users/c2leb/Codes/obs-plugins/tea-live-subtitle`（`git@github.com:calebweixun/tea-live-subtitle.git`）。

M1 已實機確認載入（OBS 32.2.2 log 出現 `[tea-live-subtitle] loaded version 0.1.0`，
外掛以 `pkgutil --expand-full` 取出 bundle 放進 `~/Library/Application Support/obs-studio/plugins/`，
不需要 sudo）。2026-09-19 後續已下載 workflow run `35423344854`、commit `36c5f12`
的 macOS artifact，使用 `/Users/c2leb/tea-asr-takes/take1.wav`（16 kHz mono，約 75 秒）
實機驗證。OBS 畫布最後以 `Heiti TC Light` 顯示出可讀字幕：
「這個 pr 已經 merge 了，我要 take 一個 release。」證據截圖在
`/tmp/tea-obs-heiti-tc-20260919.png` 與 `/tmp/tea-obs-heiti-caption-large.png`，log 是
`~/Library/Application Support/obs-studio/logs/2026-09-19 14-03-49.txt`。

驗收報告在該倉庫的 `docs/m2-verification.md` 與 `docs/m2-reverification.md`，
**務必先讀**。特別是覆驗那份：上一輪的「多來源防護」修復建立在錯誤前提上
（以為伺服器會拒絕、以為 `session_limit` 代表「別人占用」），後來已退回
（commit `6edbb58`），改成一律信任伺服器 `error` 事件的 `retryable` 欄位。

**已做、尚未送 CI**：client 已對應 `concurrent_session_limit` + close **4029**（§2.3），
會保留伺服器較完整的 error message；若 error text frame 遺失，也會由 4029 產生可操作、
且保持 retryable 的 fallback。錯誤判斷抽成不依賴 Qt/OBS 的 policy，已有 standalone C++
測試覆蓋 4029、1013、舊錯誤污染及 non-retryable 行為。

第一次實機驗證也抓到 Arial 會把中文畫成方框，`PingFang TC` 雖是 macOS 字型名稱，
OBS FreeType 實測卻載入失敗。程式碼預設已改為 Fontconfig 可見的 macOS 系統 family
`Heiti TC`（Windows `Microsoft JhengHei`、Linux `Noto Sans CJK TC`），不覆寫既有 scene
或使用者自訂字型。這份修改仍需推送並讓三平台 CI 建置，再安裝新 artifact 驗證「新建
source 不手動改字型也能直接顯示中文」。

**Phase A（字幕資料流與固定寬度）已完成**：`TeaAsrClient::setStatus()` 的錯誤與連線
狀態只提供給 Tools／設定診斷視窗，字幕 canvas 只畫 partial/final transcript；沒有字幕
時的等待提示由 source 自己顯示，不會把 server error 當成字幕。新建 source 預設為
Fixed 960px，舊 scene 沒有 layout marker 時 migration 保留 Auto 與原本的
`custom_width` 語意；CJK font effective defaults 也會實際傳給 private child source。
這一階段只保證固定寬度與 native wrapping；像 YouTube 的嚴格最多雙行／截切規則延到
Phase B，不能把目前的 `Max Lines` 誤當成 visual two-line 保證。

伺服器本輪另修了兩個實測才發現的競態：
- continuous admission 原本用 registry 計數，兩個同時 `session.start` 可能都通過；現在用
  共享 lock + reservation set 原子預留，並有 barrier 並發測試與失敗清理測試。
- `idle_unloaded` 的 WS 連線原本先背景 reload、卻把舊狀態傳進 session，client 會收到
  non-retryable `model_unavailable`；現在該次連線回 `model_loading` + `retryable=true`，
  背景 reload 行為不變。

殘留缺陷（覆驗列出，未處理）：
- `~TeaAsrClient()` 逾時後的 `terminate()` 兜底可能造成行程級鎖損毀，風險已從
  「卡死呼叫端」轉成「影響範圍更大但機率低」。
- 外掛現在只有不依賴 OBS/Qt 的 error policy 與字型預設靜態測試；WebSocket framing、
  音訊擷取、caption state 與真實 OBS 整合仍沒有自動化測試。

### 4.2 OBS M3

partial 文字的低透明度/不同顏色顯示、CEA-608 closed caption 輸出
（`obs_output_output_caption_text2`）、掉幀數接到 UI。規格見
[08 OBS 外掛](08-obs-plugin.md)。

### 4.3 mini 模型的擴大驗證

見 §2.2。需要更大樣本才能決定要不要換。

### 4.4 1013 close code 三碼共用

`queue_full` / `session_limit` / `slow_client` 共用 1013，client 分不出來。
`concurrent_session_limit` 已經示範了獨立 close code 的做法，其餘三個可以比照。

### 4.5 P4 長檔案與保存

完全未開始。

---

## 5. 圖示資產

- 主程式圖示：`clients/macos/tools/source-cup.png`（GPT 產出，另有 `source-leaf.png` 備用）
  → `make-icon.py` 去背 + Apple squircle + 全尺寸 + `.icns`。
- 選單列：`clients/macos/tools/source-menubar.png`（三狀態並排黑白稿）
  → `make-menubar-icon.py` 裁切成 template。
- 重新產生：`cd clients/macos && ./scripts/make-icon.sh`

**選單列的驗收標準是 @2x（36px），不是 18px**——Retina 上實際顯示的是 @2x，
1x 只有外接非 Retina 螢幕會用到。曾經因為只看 18px 而否決了正確的設計。
已知弱點：error 狀態的驚嘆號徽章在 1x 下會糊在把手上。

---

## 6. 開發方式的一點經驗

這一輪用「開發者 → 驗收者 → 修復者 → 覆驗者」的分工，每一棒都是新的 context，
**產出者不驗收自己的東西**。實際攔下了兩個會直接進下一階段的問題：

1. 開發者自述六項「屬實」，驗收查出架構違反服務端限制。
2. 修復者照著改，覆驗查出**修復本身建立在錯誤前提上**，比原本的行為更糟。

如果讓同一個 agent 自驗，這兩個都會漏掉。建議沿用。

另外：agent 回報「實測」時要追問語料、樣本數與是否序列化執行。這一輪有一個
agent 找不到指定的音檔就自行換了語料——它有明講，但如果沒追問就會誤以為是
同一批資料。
