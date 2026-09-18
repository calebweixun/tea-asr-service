# 02｜原案與模型研究紀錄

> 「已查證」代表文件／原始碼閱讀結果，不代表已在本機執行或量測。

## 研究範圍與可重現性

原案經 Git 取得並建立 codebase-memory-mcp 索引；使用架構查詢、符號搜尋、程式片段與呼叫追蹤交叉核對。GitHub 公開頁本次讀取回傳 404，但本機 Git 取得成功。以下 commit permalink 需相應 repo 權限。

| 來源 | 本次固定版本 |
|---|---|
| lcsy-asr-csinputmethod | `018777c41e929f17cee41ad25eae49625fe4f452` |
| mlx-audio | `v0.4.5`／`04151c6abb74b886f879a4457ccdc96761f10102` |
| Alkd MLX 模型 | `caee57a908b6d64be08a6462c7a21ececbd4d7cb` |

尚未下載權重、安裝推論環境或跑 ASR。原案 README 中的延遲、RAM、並行人數與辨識品質宣稱，不作為本案實測證據。

## 原案實際資料流

```mermaid
flowchart LR
  A[Windows 熱鍵與 cpal 收音] --> B[預緩衝／RMS VAD]
  B --> C[start + 完整 PCM + end]
  C --> D[Axum WebSocket handler]
  D --> E[共用 PyWorkerAsrEngine]
  E --> F[stdin JSON + base64 PCM]
  F --> G[Python worker / AsrEngine]
  G --> H[PyTorch Qwen ASR]
  H --> I[final 文字]
  I --> J[Client 熱詞替換／ITN／Win32 注入]
```

### 已查證的實作

| 位置 | 觀察 | 對新案的含義 |
|---|---|---|
| [common/lib.rs L24](https://github.com/DSDALAB/lcsy-asr-csinputmethod/blob/018777c41e929f17cee41ad25eae49625fe4f452/crates/common/src/lib.rs#L24) | start/end/ping/字典同步，final 只有 text 與 duration_ms | 保留 JSON 控制＋binary PCM 精神；增加 ID、時間軸與能力宣告 |
| [client/ws.rs](https://github.com/DSDALAB/lcsy-asr-csinputmethod/blob/018777c41e929f17cee41ad25eae49625fe4f452/crates/client/src/ws.rs) `recognize` | 一次送完整 PCM，送 end 後等 final，30 秒 timeout | 現有行為是分段請求，不是持續音訊增量解碼 |
| [client/vad.rs](https://github.com/DSDALAB/lcsy-asr-csinputmethod/blob/018777c41e929f17cee41ad25eae49625fe4f452/crates/client/src/vad.rs) `process_samples` | 20ms RMS、預緩衝、靜音切段、15 秒強制切片 | 短預緩衝值得保留；噪音環境需更可靠 VAD |
| [server/main.rs L397](https://github.com/DSDALAB/lcsy-asr-csinputmethod/blob/018777c41e929f17cee41ad25eae49625fe4f452/crates/server/src/main.rs#L397) | per-connection Vec 累積音訊；end 後 await 推論；此 handler 未設音訊累積上限 | 新版收音與推論分離；明訂容量、排程與 backpressure |
| [server/asr.rs L168](https://github.com/DSDALAB/lcsy-asr-csinputmethod/blob/018777c41e929f17cee41ad25eae49625fe4f452/crates/server/src/asr.rs#L168) | worker stdin/stdout，base64 輸入、Mutex 鎖住完整 request/response | 常駐 worker 可保留；多 WS 連線仍共用序列推論 |
| [worker.py L32](https://github.com/DSDALAB/lcsy-asr-csinputmethod/blob/018777c41e929f17cee41ad25eae49625fe4f452/server/worker.py#L32) | 逐行讀請求；另有 inference_ms，但 Rust 結果取 duration_ms | 新 API 必須區別音訊長度、排隊、推論與端到端延遲 |
| [asr_engine.py L140](https://github.com/DSDALAB/lcsy-asr-csinputmethod/blob/018777c41e929f17cee41ad25eae49625fe4f452/server/asr_engine.py#L140) | lock 保護推論，三種載入策略；例外轉成空字串 | 新版以型別化錯誤回報，不把引擎失敗視作「沒說話」 |
| [server/main.rs](https://github.com/DSDALAB/lcsy-asr-csinputmethod/blob/018777c41e929f17cee41ad25eae49625fe4f452/crates/server/src/main.rs) `main` | 嘗試真 Python worker，失敗會改用 MockAsrEngine | 新版 production 必須 fail closed；mock 僅明確測試模式 |

README 與 HANDOVER 描述的預設 engine、Python server 入口與目錄不完全一致；本次 clone 也沒有 README 指向的 `server/requirements.txt`。因此新案不能照搬啟動文件。索引的跨語言呼叫邊偶有錯誤配對，結論以實際函式內容為準。

### 保留、改造、捨棄

**保留：** client/server 分離、模型常駐、二進位音訊、短預緩衝、狀態回饋、專有詞支援。

**改造：** client 專用 VAD 變成 server 可共用的 endpointing；單次 final 協定變成帶 ID 的 segment 事件；worker 增加監督與容量控制；字典採 request/session 隔離，避免不同 client 互相改設定。

**捨棄：** Win7 相容層、Win32 GUI／文字注入、CUDA 運維假設、靜默 mock fallback、預設全域 ITN、無上限累積音訊。

## 指定 MLX 模型

模型卡指出它是 TEA-ASR-1.1 的 MLX 轉換，文字 decoder 為 4-bit、audio tower 線性層為 8-bit，其他層仍有浮點權重；頁面約 1.31GB 是權重規模，不是運行 RAM。模型卡提供 mlx-audio 的 quantization predicate override，並列明 MIT 與底層 Qwen attribution。實作前固定 revision 並檢查對應檔案。[Alkd 模型卡](https://huggingface.co/Alkd/TEA-ASR-1.1-MLX-4bit/tree/caee57a908b6d64be08a6462c7a21ececbd4d7cb)

上游 TEA-ASR 著重台灣華語、繁體中文與中英混用，建議中文場景指定 `language="Chinese"`。上游 benchmark 並非這個 MLX 量化版本在 Mac 的驗證，品質與速度都需重測。[TEA-ASR 原模型](https://huggingface.co/JacobLinCool/TEA-ASR-1.1)

固定 revision 的 [preprocessor_config.json](https://huggingface.co/Alkd/TEA-ASR-1.1-MLX-4bit/blob/caee57a908b6d64be08a6462c7a21ececbd4d7cb/preprocessor_config.json) 宣告 16kHz。服務 wire format 因而統一為 16kHz mono PCM s16le。

## mlx-audio v0.4.5 的實際限制

已閱讀 [Qwen3 ASR 實作](https://github.com/Blaizzy/mlx-audio/blob/04151c6abb74b886f879a4457ccdc96761f10102/mlx_audio/stt/models/qwen3_asr/qwen3_asr.py)：

- `model_quant_predicate` 排除 `audio_tower`；指定模型的 8-bit tower 必須處理這個差異。
- `generate` 接受完整 array／音檔、`language`、`system_prompt`、`max_tokens` 等；不能把其他後端的 `context=` 當成已支援。此實作會丟棄額外 kwargs，傳錯名稱可能無聲失效。
- `stream_transcribe` 先取得完整音訊再逐 token 輸出。這是輸出串流，不是會保留聲學狀態的增量輸入串流。
- 輸出 segments 是 chunk 邊界；stream token 時間是估算。兩者都不可當成精準 word alignment。
- `split_audio_into_chunks` 預設把短於1秒的音訊補零至1秒；server必須保留原始有效樣本數，不能把padding算進字幕終點或來源時間軸。
- `generate` 有長音訊切塊功能，但 server 仍須自行控制片段長度與排程；不要把整場會議交給一次 generate。
- `load_model` 預設 `strict=False`；本案使用 `strict=True` 並驗證權重完整性。

依 [pyproject.toml](https://github.com/Blaizzy/mlx-audio/blob/04151c6abb74b886f879a4457ccdc96761f10102/pyproject.toml)，基礎依賴已含 mlx-lm、transformers、SciPy、sounddevice 等。**不能宣稱選 MLX 就沒有 transformers 或大型 transitive dependencies。** 不安裝 `all`／`server` extras；以本案最小依賴組合另建 HTTP 層，並在 P0 記錄解析後體積與是否意外帶入 Torch。

## 平台與能力界線

[MLX 安裝文件](https://ml-explore.github.io/mlx/build/html/install.html) 列出 Apple Silicon、原生 Python >=3.10、macOS >=14。這只是 MLX 基線；本案暫選 macOS 15+、arm64 Python 3.12 為驗證目標，最終支援範圍以鎖定 wheels 實测為準。

| 能力 | 結論 |
|---|---|
| 台灣華語／中英混合轉錄 | 優先驗證主路徑 |
| 真正增量音訊 ASR | 本次未證實所選 MLX adapter 支援；v0.1 宣告 false |
| 逐字時間戳 | 需額外 forced alignment，v0.1 false |
| 翻譯 | 獨立 translator，非 ASR 自帶能力 |
| 說話者辨識／分離 | 未驗證；不以音訊來源 ID 假裝 speaker ID |
| 多路即時 | 取決於總音訊負载與實測，連線數不是答案 |

以上推導形成下一份文件的架構決策；效能數字全部留待 05 驗收。
