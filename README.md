# TEA ASR Service

在 Apple Silicon Mac 上運行的本機語音辨識服務，讓輸入工具、字幕工具、OBS 與會議紀錄共用一份模型。

**目前狀態：P0–P3與P2a已完成並實測，Mac選單列client可用。** 指定模型已在M4 Pro真實載入與推論。私用區字元的成因已定位在MLX量化本身（上游BF16為0%），服務端預設過濾，見 [PUA A/B報告](docs/benchmarks/pua-bf16-ab-report.md)。Silero VAD已接上並用真實口語校準，串流預覽通過驗收且預設開啟。Final 左上下文 carry 已實作但預設關閉，等待 speakers／music-8900 真模型品質驗收。

## 先看這裡

- **技術選擇：** Python 3.12、MLX／mlx-audio、FastAPI、單一模型工作程序；原生 macOS 運行。
- **預設模型：** pinned `Alkd/TEA-ASR-1.1-MLX-4bit`；使用者可明確選用本機 pinned 8-bit 衍生檔 `tea-1.1-mlx-8bit`。
- **第一個里程碑：** 一段音訊可靠地進來，一份繁體中文／中英混合逐字稿可靠地出去。
- **即時能力路線：** v0.1先完成分段轉錄；v0.1.1／P2a緊接實作「邊說邊出字、依後文修訂、最後定稿」，P0就驗證其成本。
- **輕量原則：** 一份模型、有限佇列、有限音訊緩衝；無 Docker、Redis、Celery、Electron 或常駐翻譯模型。

## 文件地圖

| 你現在想做什麼 | 閱讀文件 |
|---|---|
| 理解產品範圍與四種 client | [01 產品規劃](docs/01-product.md) |
| 查原案實際架構、模型限制與證據 | [02 研究紀錄](docs/02-research.md) |
| 確認技術棧、程序、排程與部署 | [03 架構決策](docs/03-architecture.md) |
| 實作 client 或 server 通訊 | [04 API 契約](docs/04-api.md) |
| 執行模型驗證、效能與可靠性驗收 | [05 驗證計畫](docs/05-validation.md) |
| 看真實語料的品質實測 | [P0 品質報告](docs/benchmarks/p0-quality-report.md) |
| 看切段、長跑與串流預覽的實測 | [P2 切段](docs/benchmarks/p2-segmentation-report.md)、[P2 長跑](docs/benchmarks/p2-soak-report.md)、[P2a 預覽](docs/benchmarks/p2a-preview-report.md) |
| 交給 Sol 或其他模型開始開發 | [06 開發交接](docs/06-handoff.md) |
| 理解類似系統聽寫的預覽與上下文修訂 | [07 串流修訂規劃](docs/07-contextual-streaming.md) |
| 開 OBS 直播字幕外掛的 repo | [08 OBS 外掛規格](docs/08-obs-plugin.md) |
| 用 Mac 選單列 app 聽寫或做會議記錄 | [clients/macos](clients/macos/) |
| 實際測試與回報問題 | [09 測試指南](docs/09-testing-guide.md) |
| 用真實錄音覆驗 OBS live subtitles 與字幕畫面 | [09 真實錄音字幕 soak](docs/09-testing-guide.md#六真實錄音字幕-soak) |

建議先閱讀 01 的「範圍」，再把 06 最後的開發提示交给實作模型。API 定義以 04 為準；不確定事項以 05 的驗證關卡為準。

## 目前可執行

```bash
uv sync --extra dev
uv run tea-asr doctor
uv run tea-asr model-prepare
uv run tea-asr serve
```

另一個終端機：

```bash
uv run tea-asr status
uv run tea-asr transcribe path/to/16k-mono.wav
uv run python examples/stream_wav.py path/to/16k-mono.wav
```

要讓它登入時自動啟動（只有明確執行 install 才會建立登入項目）：

```bash
uv run tea-asr service install
```

```bash
uv run tea-asr service status
```

```bash
uv run tea-asr service uninstall
```

設定放 `~/Library/Application Support/TEA ASR/config.toml`，欄位打錯會直接報錯而不是被忽略：

```toml
[service]
port = 8327
idle_unload_s = 900   # 閒置這麼久就卸載模型釋放記憶體；0 或 keep_warm 可關閉
keep_warm = false
asr_model = "tea-1.1-mlx-4bit"  # 可明確改成 tea-1.1-mlx-8bit
carry_context_s = 0.0  # final 左上下文；未通過 gold 品質門檻前維持關閉
carry_context_max_gap_s = 1.5
```

### 選擇 8-bit ASR 模型

模型選擇只影響服務載入的那一份 ASR worker。預設仍是 4-bit；你可以在設定檔設 `asr_model = "tea-1.1-mlx-8bit"`，或啟動服務前執行 `export TEA_ASR_MODEL=tea-1.1-mlx-8bit`。改回預設值可設 `tea-1.1-mlx-4bit`。`tea-asr model-prepare --variant 8bit` 只驗證 `TEA_ASR_MODELS_DIR/mlx-8bit-selfconv` 的完整檔案集與 SHA-256，不會下載 8-bit 模型；若缺檔或 hash 不符，`tea-asr serve` 會以 `model_unavailable` 停止、寫錯誤日誌，`tea-asr doctor` 會顯示所選變體及原因，不會載入 4-bit。需要重製時，請在 repository root 執行錯誤訊息中印出的 `benchmarks/convert_quant.py` 命令。

8-bit 檔案由 `JacobLinCool/TEA-ASR-1.1` revision `bda08df76d4fd6b487b4a1dd7f0bddf8541696f8` 以 `mlx 0.32.2`、`mlx-audio 0.4.5`、`mlx-lm 0.31.3` 本機轉換；每個輸出檔案 pin 與轉換命令記在 [`models.lock.json`](models.lock.json)。授權鏈是 TEA-ASR-1.1 的 MIT 授權衍生檔，並保留底層 `Qwen/Qwen3-ASR-1.7B` 的 Apache-2.0 通知，見 [`NOTICE`](NOTICE)。

在使用者人工訂正的三場教會 SRT 離線評估中，8-bit + carry 3 秒相對 4-bit + carry 3 秒的 CER 低 **0.27 個百分點**（95% CI **−0.44 至 −0.10**）；模型檔大 **92%**。這不代表即時串流有同樣的速度或品質差異。真實 server 的 10 分鐘 8-bit／4-bit 對照尚未執行：load time、worker RSS、preview decode p50/p95、worker busy、final latency p95 與首 6 分鐘 CER **待填**。coordinator 的可重現檢查腳本為 [`/Users/c2leb/Codes/tea-asr-service/.soak/run-8bit-check.sh`](/Users/c2leb/Codes/tea-asr-service/.soak/run-8bit-check.sh)；結果與未驗證項目見 [church evaluation report](docs/benchmarks/church-eval-2026-10.md) 和 [06 handoff](docs/06-handoff.md)。

服務啟動時會檢查 port 與 singleton lock，第二份實例會被拒絕並告訴你是誰占著。

## 想先體驗效果

目前只有 `examples/` 下的參考 client，沒有選單列 app 或輸入法；最接近日常使用的是麥克風即時 demo（需要 `brew install ffmpeg`）：

```bash
uv run python examples/mic_stream.py --list-devices
```

```bash
uv run python examples/mic_stream.py --device 2
```

對著麥克風一句一句講，停頓一下 server 就會自己斷句並印出該段定稿——不用按任何鍵。按 Enter 結束整個 session；加 `--utterance` 則改回自己標記段落。

「邊說邊出字、依後文修訂」預設就是開的，client 加 `--revisable` 即可：

```bash
uv run python examples/mic_stream.py --device 2 --revisable
```

要關掉預覽（只要定稿）：

```bash
TEA_ASR_REVISABLE_PREVIEW=0 uv run tea-asr serve
```

沒有麥克風也可以用合成語音試：

```bash
say -v Meijia "這份 PR 已經 merge 了，我們下午跟 client 開會。" -o /tmp/demo.aiff && ffmpeg -y -i /tmp/demo.aiff -ar 16000 -ac 1 /tmp/demo.wav && uv run python examples/stream_wav.py /tmp/demo.wav
```

體驗時會看到的已知限制：一直講不停會在 12 秒附近硬切（`boundary="max_duration"`，會先找安靜點）、utterance 單段最多 30 秒。辨識結果原本會夾帶私用區字元，根因已定位在 `Alkd/TEA-ASR-1.1-MLX-4bit` 的 4bit 量化本身（上游 BF16 checkpoint 是 0%，見 [PUA vs BF16 A/B 報告](docs/benchmarks/pua-bf16-ab-report.md)），現在服務預設會把這些字元從 `text` 過濾掉（`filter_pua` 設定／`TEA_ASR_FILTER_PUA` 環境變數可關閉），`raw_text` 與 `warnings:["private_use_characters"]` 仍保留原始資訊供除錯。串流文字會把一般重複單位限制為預設 3 次；可用 `repetition_single_char_limit`、`repetition_multi_char_limit` 與對應的 `TEA_ASR_REPETITION_*_LIMIT` 環境變數調整。含數字或 CJK 數字的 1–6 字元單位連續超過預設 6 次時會修剪到 3 次，可用 `repetition_numeral_loop_limit`／`TEA_ASR_REPETITION_NUMERAL_LOOP_LIMIT` 調整；單一十進位數字至少要連續 10 次才修剪，且緊鄰其他數字或 CJK 數字的單位會保留，因此 `1000000元`、`0999999999`、`一九九九年` 與 `零零七` 不會被截斷。ASCII 字母單位只在兩側都沒有 ASCII 字母時修剪。final 的 `raw_text` 不變，`warnings` 會帶 `repetition_trimmed`。VAD 參數（句尾靜音 500 ms、最短語音 160 ms、pre-roll 600 ms、最大 12 秒）已用真實口語校準過一次，但只有單一語者與單一麥克風。

模型與 VAD 資產放在倉庫內的 `models/`（已 gitignore，可用 `TEA_ASR_MODELS_DIR` 改），**不放 `~/Library/Caches`**——macOS 會在磁碟吃緊時把那裡整個清掉，實際發生過一次。服務只監聽 `127.0.0.1:8327`（電話鍵盤上的 T-E-A；刻意避開 8765 那類 AI 工具常用的 port）。首次啟動會在 `~/Library/Application Support/TEA ASR/token` 建立權限0600的token。短音訊端點與WS utterance session都接受最多30秒、16 kHz mono PCM s16le；詳見 [API契約](docs/04-api.md)，機器可讀版本在 [docs/api/](docs/api/)（`uv run tea-asr export-schemas` 重新產生，測試會檢查是否過期）。本機結果見 [P0報告](docs/benchmarks/p0-report.md)。

## 實作進度

| 階段 | 狀態 | 說明 |
|---|---|---|
| P0 模型可行性 | 通過 | 真實語料CER 4.92%（200筆）、真人口語MER 3.97%。私用區字元leak已定位為MLX量化造成（上游BF16 0%、自轉8bit仍63.3%），服務端預設過濾，見 [品質報告](docs/benchmarks/p0-quality-report.md)、[PUA A/B](docs/benchmarks/pua-bf16-ab-report.md) |
| P1 短音訊API | 已實作 | HTTP transcription、健康探針、capabilities、status、bounded scheduler、typed errors、OpenAPI／WS schema |
| P2 即時音訊 | 已實作、已校準、已長跑 | WS utterance與continuous皆可用：Silero VAD自動斷句、有序片段管線、推論不阻塞收音。真人口語MER 3.97%（[切段報告](docs/benchmarks/p2-segmentation-report.md)）；連續一小時386段0錯誤、延遲p95 0.57秒、記憶體平穩（[長跑報告](docs/benchmarks/p2-soak-report.md)）。多路併發已實測，N=1..4零錯誤，預設上限2（[併發報告](docs/benchmarks/concurrency-report.md)）|
| P2a 串流修訂 | 已驗收，預設開啟 | 首次可見延遲 p95 0.91 秒、final 與 final-only 完全一致、混合負載不互相阻塞，見 [P2a 報告](docs/benchmarks/p2a-preview-report.md)。`TEA_ASR_REVISABLE_PREVIEW=0` 可關閉 |
| P3 服務管理 | 完成 | LaunchAgent install/uninstall/status、singleton lock、port 檢查、idle unload 與重新載入、TOML 設定、JSON log 輪替、關閉時 drain、睡眠喚醒偵測與 worker 健康探測 |
| Recognition hints | 已實作，預設關閉 | `TEA_ASR_CONTEXT_HINTS=1` 開啟 dictionaries、profiles 與 deterministic replacements；`GET /v1/dictionaries` 逐檔回報無效字典而保留有效項目，session start 仍拒絕無效 profile。Model prompt 需另外開啟 `TEA_ASR_CONTEXT_PROMPT=1`，且只有 hints 已開時生效；prompt 預設關閉、仍屬 experimental，prompt token 數只記 server log。4段真實講道音訊 smoke 未見改善。見 [04 API 契約](docs/04-api.md) |
| Singing labels | 已實作，預設開啟（需 YAMNet 資產） | server 用 YAMNet（Apache-2.0，ONNX，約 16 MB，`tea-asr model-prepare` 下載並釘 hash）為每段標 `speech`／`singing`（`segment.audio_class`，約 1.5 秒首判、最多修訂一次；final 帶 `audio_class`），只標記不改轉錄，隱藏字幕由 client 決定。留一檔驗證：speech 視窗誤判 0.0%、歌唱片段首判 83%／最終 95%、29 分鐘講道 0 次誤判；但只有 3 首歌（同一樂團）、1 段背景音樂下講話，**未在真實 OBS 實機驗收**。缺資產時不宣告能力；`TEA_ASR_SINGING_DETECTION=0` 關閉。見 [評估報告](docs/benchmarks/singing-eval-report.md)、授權見 [NOTICE](NOTICE) |
| Final 左上下文 carry | 已實作，預設關閉，等待真模型評估 | Final 最多帶前段尾音 0–5 秒，去除前一 final 尾端重疊；無可信重疊時重跑 segment-only。preview 不帶 carry。答案間隔統計及評估方式見 [07](docs/07-contextual-streaming.md)；品質門檻未通過前不可開啟 |
| P4 長檔案與保存 | 未開始 | `/v1/jobs` 不存在，回404 |
| P5a Mac client | 可用，未完整驗收 | 選單列 app：聽寫（定稿後貼進前景 app）與會議記錄（即時視窗＋Markdown 匯出），見 [clients/macos](clients/macos/)。P5b 輸入法組字區整合未做 |

能力宣告跟著這張表走：`capabilities` 只有在對應驗收通過後才會把 feature 設為 true。

## 這個版本不做什麼

不是省略，是明確不支援。client 不該假設這些行為存在：

- **Ephemeral**：預設不把音訊或逐字稿寫進資料庫。服務重啟後 session 與逐字稿都不在了。
  會議記錄的保存是 Mac client 自己寫檔，不是服務的持久化。
- **不支援 resume**：斷線就是結束。重連要開新 session、新的 sample clock。
  機器睡眠醒來後服務會主動送 `timeline_gap` 並關閉連線，而不是把缺口兩側接起來假裝連續。
- **沒有精準時間戳**：`timestamp_quality` 永遠是 `segment`，只有片段的起迄範圍，
  沒有逐字對齊。字幕可用，但不要拿它做逐字高亮。
- **沒有翻譯、沒有語者分離**：`capabilities` 裡這些 feature 都是 false，
  請求相關選項會被拒絕而不是被忽略。
- **Recognition hints 預設不可用**：需 server 明確設定 `TEA_ASR_CONTEXT_HINTS=1`。Model prompt 還需 `TEA_ASR_CONTEXT_PROMPT=1`；它維持 experimental 並預設關閉，因為4段真實講道音訊 smoke 未見改善。審閱過的 deterministic replacements 是建議工具。
- **單機單模型**：一台機器一份服務、一個 worker、一份模型。第二個實例會被拒絕。

## 安裝

```bash
uv sync
uv run tea-asr model-prepare
```

要打包成 wheel 給另一台 Apple Silicon Mac：

```bash
uv build
```

產物在 `dist/`。模型與 VAD 資產不在 wheel 裡，裝好後仍要跑 `tea-asr model-prepare`。

這個 wheel 已在乾淨的 venv 裡實測過：安裝後 `tea-asr doctor` 可以正常執行並連上服務
（2026-09-19，arm64 Python 3.12）。

## 設計基準

研究日期：2026-09-18。原始專案 [DSDALAB/lcsy-asr-csinputmethod](https://github.com/DSDALAB/lcsy-asr-csinputmethod) 固定於 `018777c41e929f17cee41ad25eae49625fe4f452`。

本案承接原案的 client/server 分離、常駐模型與語音輸入體驗；以 macOS、多用途 API 與長時間可靠運行重新規劃，不直接搬移 Windows 專用程式。
