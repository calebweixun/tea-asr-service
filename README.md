# TEA ASR Service

在 Apple Silicon Mac 上運行的本機語音辨識服務，讓輸入工具、字幕工具、OBS 與會議紀錄共用一份模型。

**目前狀態：P0效能可行、品質驗收尚未通過；P1的HTTP與WebSocket utterance切片已在真模型上跑通。** 指定模型已在M4 Pro真實載入與推論，私用區字元問題仍未解決。VAD、continuous profile與正式client尚未開始；P2a串流預覽已有實作但未通過驗收，預設關閉。

## 先看這裡

- **技術選擇：** Python 3.12、MLX／mlx-audio、FastAPI、單一模型工作程序；原生 macOS 運行。
- **優先模型：** `Alkd/TEA-ASR-1.1-MLX-4bit`，先驗證混合量化載入與本機品質。
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
| 交給 Sol 或其他模型開始開發 | [06 開發交接](docs/06-handoff.md) |
| 理解類似系統聽寫的預覽與上下文修訂 | [07 串流修訂規劃](docs/07-contextual-streaming.md) |

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

服務只監聽 `127.0.0.1:8765`。首次啟動會在 `~/Library/Application Support/TEA ASR/token` 建立權限0600的token。短音訊端點與WS utterance session都接受最多30秒、16 kHz mono PCM s16le；詳見 [API契約](docs/04-api.md)，機器可讀版本在 [docs/api/](docs/api/)（`uv run tea-asr export-schemas` 重新產生，測試會檢查是否過期）。本機結果見 [P0報告](docs/benchmarks/p0-report.md)。

## 實作進度

| 階段 | 狀態 | 說明 |
|---|---|---|
| P0 模型可行性 | 效能通過、品質未通過 | 私用區字元leak未解；真實語料corpus尚未建立 |
| P1 短音訊API | 已實作 | HTTP transcription、健康探針、capabilities、status、bounded scheduler、typed errors、OpenAPI／WS schema |
| P2 即時音訊 | 部分 | WS utterance session（binary frame、sample clock、flow window、commit/stop/cancel）可用；continuous profile與VAD未實作，回 `unsupported_option` |
| P2a 串流修訂 | 實作但未驗收 | 需 `TEA_ASR_EXPERIMENTAL_REVISABLE_PREVIEW=1` 才啟用；未啟用時 `partial_transcripts=false` 且 revisable 請求回 `unsupported_option` |
| P3 服務管理 | 未開始 | 無 LaunchAgent、無 idle unload、無 singleton lock |
| P4 長檔案與保存 | 未開始 | `/v1/jobs` 不存在，回404 |
| P5 client | 未開始 | 只有 `examples/` 下的參考 client |

能力宣告跟著這張表走：`capabilities` 只有在對應驗收通過後才會把 feature 設為 true。

## 設計基準

研究日期：2026-09-18。原始專案 [DSDALAB/lcsy-asr-csinputmethod](https://github.com/DSDALAB/lcsy-asr-csinputmethod) 固定於 `018777c41e929f17cee41ad25eae49625fe4f452`。

本案承接原案的 client/server 分離、常駐模型與語音輸入體驗；以 macOS、多用途 API 與長時間可靠運行重新規劃，不直接搬移 Windows 專用程式。
