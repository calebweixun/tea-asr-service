# 10｜接手交接（2026-09-19）

這份文件寫給**下一個接手開發的模型或人**。它不重複 [03 架構](03-architecture.md)、
[04 API 契約](04-api.md)、[06 交接](06-handoff.md) 已經寫清楚的東西，只記錄
**現況、已定案的判斷、以及你接手時最可能踩到的坑**。

規格優先順序不變：使用者最新明確指示 → 04 wire 契約 → 03 架構 → 06 開發次序。

---

## 1. 現在能跑什麼

| 元件 | 狀態 |
|---|---|
| Server（`src/tea_asr/`） | P0–P3、P2a 完成並實測。133 個測試通過（`uv run pytest tests/unit tests/integration -q`） |
| Mac client（`clients/macos/`） | 可用：聽寫、會議記錄、選單列狀態、服務啟停 |
| OBS 外掛（另一個倉庫） | M1+M2 完成，CI 三平台綠，**尚未實機驗證字幕真的會出現** |
| P4 長檔案與保存 | 未開始，`/v1/jobs` 回 404 |

啟動服務：

```bash
cd /Users/c2leb/Codes/tea-asr-service && uv run tea-asr serve
```

健康檢查端點是 **`/healthz`**（不是 `/health`）。服務只聽 `127.0.0.1:8327`。

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

**現行對策**：服務端預設過濾（`ServiceConfig.filter_pua`，`TEA_ASR_FILTER_PUA=0`
可關）。`text` 是過濾後結果，`raw_text` 保留原始辨識供除錯。整段被過濾成空字串時
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

### 4.1 OBS 外掛：實機驗證字幕真的會出現 ★最高

倉庫 `/Users/c2leb/Codes/obs-plugins/tea-live-subtitle`（`git@github.com:calebweixun/tea-live-subtitle.git`）。

M1 已實機確認載入（OBS 32.2.2 log 出現 `[tea-live-subtitle] loaded version 0.1.0`，
外掛以 `pkgutil --expand-full` 取出 bundle 放進 `~/Library/Application Support/obs-studio/plugins/`，
不需要 sudo）。**但沒有人實際對著麥克風講話、確認字幕出現在畫面上。** 這是目前
最大的未知。

驗收報告在該倉庫的 `docs/m2-verification.md` 與 `docs/m2-reverification.md`，
**務必先讀**。特別是覆驗那份：上一輪的「多來源防護」修復建立在錯誤前提上
（以為伺服器會拒絕、以為 `session_limit` 代表「別人占用」），後來已退回
（commit `6edbb58`），改成一律信任伺服器 `error` 事件的 `retryable` 欄位。

**還沒做**：伺服器現在會送 `concurrent_session_limit` + close **4029**（§2.3），
client 端**尚未對應**。這是下一個該接的東西。

殘留缺陷（覆驗列出，未處理）：
- `~TeaAsrClient()` 逾時後的 `terminate()` 兜底可能造成行程級鎖損毀，風險已從
  「卡死呼叫端」轉成「影響範圍更大但機率低」。
- 外掛沒有任何自動化測試。

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
