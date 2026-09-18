# 06｜給 Sol 與其他實作模型的交接

> 原始交付為文件。**2026-09-19更新：** P0已在M4 Pro實測（效能通過、品質未通過），P1與P2的utterance切片已實作並在真模型上驗證，進度表見 [README](../README.md#實作進度)。
> 以下各階段的完成條件仍然有效，勾選狀態寫在每節開頭。

## 開發方式

採單一repo、Python package、逐階段垂直切片。每個階段都交付可重現結果，前一個關卡沒通過就不要把後續功能當成完成。使用專案的codebase-memory-mcp做程式探索；尚無程式時不需要硬建空索引，有程式後建立／更新索引。

規格優先順序：使用者最新明確指示 → 04 wire契約 → 03架構 → 本開發次序。相容性與效能新證據可推翻候選選型，但需更新研究與ADR，不能實作中悄悄改協定或模型。

每個PR／交付聚焦一個可驗證結果，列出：完成項目、執行過的命令與结果、真模型是否測過、尚未驗證的限制、下一步。不以大量mock或UI畫面掩蓋推論未通。

## 預定目錄

以下目錄為目標形狀，現在不應一次建立所有空殼：

```text
tea-asr-service/
  README.md
  pyproject.toml
  uv.lock
  models.lock.json
  src/tea_asr/
    cli.py
    config.py
    api/                  # HTTP / WebSocket 與 Pydantic wire models
    domain/               # AudioSegment / Result / Errors
    audio/                # PCM驗證、sample clock、VAD、segmenter
    sessions/             # 生命週期、flow window、event writer
    scheduler.py
    worker/               # supervisor、binary IPC、worker entry
    backends/             # TeaMlxBackend、僅測試用FakeBackend
    storage/              # P4才建立SQLite / spool / checkpoint
    exports/              # P4才建立SRT / VTT / JSON
  tests/
    unit/
    integration/
    hardware/             # 需本機模型，不隨一般CI自動下載
    fixtures/             # 小型合成音訊／取得授權的短樣本
  examples/
    transcribe_pcm.py
    stream_wav.py
  benchmarks/
  docs/
    benchmarks/
    api/                  # P1生成OpenAPI與WS JSON Schema
```

## P0｜證明指定模型可用

**輸入：** 02研究、05驗證方法。

**工作：** 建立最小pyproject與spike；固定snapshot、載入修正、strict validation、離線推論、量測、VAD adapter可行性。另依07量測累積音訊重辨識與後文修訂，提早驗證P2a可行性。只為必要程式加入測試，不先寫API／GUI／完整服務管理。

**狀態：效能通過、品質未通過。** 報告見 [benchmarks/p0-report.md](benchmarks/p0-report.md)；私用區字元leak未解，真實語料corpus尚未建立。

**完成條件：** 相容層能載入指定checkpoint；至少中文／混英／短詞／靜音有實測；報告含memory、RTF、token限制與版本；models.lock與uv.lock可重現。若未有可用硬體或音訊，交付可執行spike與明確未驗證狀態，不能標P0通過。

## P1｜短音訊走完整API

**前置：** P0確認後端；無硬體時只可先做標明mock的契約工作。

**工作：** 設定、bearer驗證、CLI serve/doctor/status/model prepare、supervisor＋IPC、HTTP transcription、health/ready/capabilities、typed errors、單模型與資源上限。Pydantic生成OpenAPI，開始WS event schema。只做PCM與WAV CLI轉換，暫不安裝FFmpeg。

**狀態：已完成。** `tea-asr transcribe` 經HTTP取得真結果；worker以受監督子程序隔離、stdout只走IPC；bounded scheduler提供真實 `queue_ms` 與 `queue_full`；typed error envelope、Pydantic wire models與 [docs/api/](api/) 的OpenAPI／WS schema皆已產生並由測試檢查。

**完成條件：** 一個CLI client可送短音訊並取得真結果；worker推論時health仍回應；卡死能回收；缺模型不假裝成功；錯格式／超body／超queue測試通過；worker stdout無log污染。

建議提交分成：domain與schemas、worker adapter、HTTP＋CLI。不要在API函式直接呼叫同步generate。

## P2｜即時音訊與連續轉錄

**工作：** WS狀態機、binary seq header、sample clock、utterance commit、continuous VAD、排程、公平性、flow window、取消與slow reader。建立 `stream_wav.py` 以真實時間送frame，不需先取得麥克風權限。

**狀態：已實作，長跑未測。** WS狀態機、binary seq header、sample clock、flow window、commit/stop/cancel、audio.ack、segment終局事件、bounded outgoing queue與慢client偵測已完成。Silero VAD以ONNX Runtime接上（每session獨立recurrent state，不引進Torch），`models.lock.json` 已固定 revision 與 sha256。continuous profile由VAD切段，pre-roll 200 ms、句尾靜音 500 ms（revisable 900 ms）、最短語音 160 ms、8秒hard split；片段經有限佇列交給單一consumer依序處理，推論不阻塞收音。
**尚未完成：** 一小時長跑的RAM與lag曲線、多路容量實測、以真實語料校準VAD參數；`max_continuous_sessions=1` 是依文件設定而非實測結果。

**完成條件：** 按sample順序產生不可變final；ASR推論中仍能收音；一小時不持續增加RAM／lag；停止時flush；每個已排片段都有終局；profile與能力宣告符合實際。根據單路實測設定max continuous sessions。

**v0.1可發行條件：** P0–P2通过；README標明ephemeral、無resume、無精準timestamps、無翻譯。發布測試用wheel或可重現uv安裝說明；不要稱為完整會議產品。

## P2a｜串流預覽與上下文修訂

**前置：** P2基線可靠；P0已有preview成本報告。這是正式產品需求，不再只列為可選preview。

**工作：** 閱讀 [07](07-contextual-streaming.md)；實作累積音訊快照、preview排程與預算、最新待跑任務合併、900ms端點grace、固定segment ID、partial完整替換與revision、final優先、負載降級。依04擴充wire 1.1；reference client能在同一row修訂文字。前文prompt独立實驗，不阻擋同片段後文修正。

**狀態：驗收通過，預設開啟。** 量測見 [P2a 報告](benchmarks/p2a-preview-report.md)：首次可見延遲 p95 0.91 秒、
final 與 final-only 模式完全一致、混合負載下 23/23 HTTP 辨識成功且 10 段 final 全數產生。
`TEA_ASR_REVISABLE_PREVIEW=0` 或 config.toml 可關閉，關閉時 revisable 請求回 `unsupported_option` 而非靜默降級。
未涵蓋：單一語者與單一機器、預覽降級路徑未在真實過載下觸發。

**完成條件：** 能呈現「先出字→後文修正→定稿」；partial不重複append、不改已final內容；重跑總RTF與品質、延遲符合07或有明確未達標報告；preview超載不阻塞收音與正式排程。測試同音詞、數字、否定詞、中英混用與cancel/final競態。

**v0.1.1可發行條件：** P2a驗收通過才宣告partial_transcripts=true。未達標可交付研究與改善方案，但不能把需求標成完成或靜默移除。保留final-only模式；P4再驗證durable_revisable。

## P3｜使用體驗與本機服務管理

**工作：** 安裝流程、模型下載進度、設定路徑、診斷訊息、idle unload/keep_warm、rotation、singleton、LaunchAgent install/uninstall、sleep/wake。

**狀態：大致完成，sleep/wake未實測。** `tea-asr service install/uninstall/status` 以絕對路徑建立 LaunchAgent，
只有明確 install 才會動登入設定；`KeepAlive` 僅在 crash 時重啟，避免「已有實例」的正常退出被無限重啟。
singleton 採 flock lock file ＋ port 檢查，第二份實例會被拒絕並回報持有者的 PID 與 port（已實測 LaunchAgent
與手動同時啟動只會有一份模型）。閒置逾時卸載 worker 後 `model_state=idle_unloaded`，第一個請求觸發重新載入
並回 503 `model_loading`（已實測，重新載入 1.7 秒、worker generation 遞增）。設定走 TOML，未知欄位直接報錯。
log 為 JSON lines 並輪替，明確過濾 token、PCM 與逐字稿。關閉時停止收件並最多 drain 30 秒。
睡眠偵測比較 wall clock 與 monotonic clock 的差距（Darwin 的 monotonic 在睡眠期間不前進），
不必為此引進 pyobjc。喚醒後對進行中的 session 送 `timeline_gap` 並 close 1012——v0.1 沒有 resume，
把缺口兩側的音訊接在同一個 sample clock 上是說謊；接著用一次真實推論探測 worker，
失敗才重啟，而不是假設它還活著。

**完成條件：** 從新環境照文件可完成prepare→serve→client轉錄；退出／卸載不留worker；手動與登入啟動不重複；離線重啟可辨識。service命令不得靜默變更使用者登入設定，只有明確執行install才建立agent。

## P4｜長檔案、保存與恢復

**工作：** SQLite migrations/WAL、單writer、spool、durable watermarks、resume/event replay、batch upload、checkpoint、取消、刪除、配額、TTL、JSON/SRT/VTT。整合P2a的segment ID/revision high-water mark、resume清除舊partial與重建；未驗收前durable_revisable=false。固定recording manifest以記錄跨session gap與來源offset；JSON schema納入docs/api。

**完成條件：** 30分鐘影片與60分鐘會議實測；crash／disk full／重送不造成假ACK或重複final；重啟jobs從checkpoint續跑；字幕sample clock與原媒體時間一致；持久化行為清楚可關閉。

**v0.2可發行條件：** P3/P4完成並通過05故障測試，才將durable_sessions、batch_jobs設true。

## P5｜挑一種client整合

**P5a（已有可用版本，見 [clients/macos](../clients/macos/)）：** Swift選單列聽寫client，以驗證真正日常使用的延遲、短詞、焦點與剪貼簿行為。採AVAudioEngine收音及可靠resampling；partial在自有浮動視窗更新，final才貼入。不要為了顯示partial而回刪使用者已打的字。

**P5b：** InputMethodKit輸入法整合，把partial呈現在自己持有的marked text／組字區，final才commit，交付游標處直接修訂體驗。涵蓋組字生命週期、使用者編輯、焦點變更、取消與安全輸入；實際app相容測試見07。一般選單列app不能直接取代此層。P5a與P5b分開交付，server協定共用。

若先做OBS，先bridge＋text source概念驗證，保留audio callback不阻塞的結構，再做官方plugin。若先做影片，完成編修／翻譯provider設定；若先做會議，先做可見收音狀態、spool與權限流程。

server repo只放reference clients与protocol測試；正式Swift app／OBS plugin可另repo，避免server release綁住多個平台編譯。

## 延後功能的進入條件

| 功能 | 開始之前要有的證據 |
|---|---|
| 跨句全文校訂／可選LLM潤飾 | P2a同片段修訂完成，另定document revision與原稿保存；不可回改ASR final |
| 熱詞 | 固定模型system_prompt實測；未支援時要拒絕而非忽略 |
| 翻譯 | 來源／目標語言、使用者是否接受雲端、獨立provider／保留原稿 |
| Forced alignment | 後端相容性、額外模型memory與對齊品質 |
| Diarization | 多人會議需求、額外模型與重疊發話處理，不以來源軌代替 |
| LAN | 明確需求、TLS／授權／rate limit；不得只改bind至0.0.0.0 |
| Swift／Rust後端 | P0/P2 profiling指出可量化收益與指定checkpoint品質一致性 |
| 使用者免Python安裝 | clean-Mac package驗證、簽章公證、Metal資產、更新回滾 |

## 開發中不可破壞的約束

1. Production載入失敗不能改用FakeBackend；測試fake必須明確開啟並在status標示。
2. 每機一個服務instance、一個MLX worker、一份模型；不能開多Uvicorn workers。
3. 不在async route、WS receive loop或OBS audio callback阻塞推論。
4. 每層buffer、queue、spool、timeout有上限與可見失敗。
5. Wire sample clock、segment ID、事件終局不隨文字後處理改寫。
6. 模型不等於translator、aligner或diarizer；capabilities只宣告實際驗證能力。
7. ephemeral預設不落音訊／逐字稿；durable ACK必須對應已落盤資料。
8. 版本／來源／benchmark可重現；效能目標不能寫成既有成果。
9. 先完成一個垂直切片，再擴展模組；不要一次生成全部未實作框架。

## 可直接交給實作模型的提示

```text
請在此repo實作TEA ASR Service，先完成P0模型可行性驗證。

先閱讀README.md、docs/02-research.md、docs/03-architecture.md、
docs/05-validation.md、docs/06-handoff.md與docs/07-contextual-streaming.md；
API實作時以docs/04-api.md為準。
這些是設計規格，尚未有server或效能測試結果。

使用Apple Silicon原生Python 3.12、uv與MLX；優先模型為
Alkd/TEA-ASR-1.1-MLX-4bit，固定文件中revision。
先驗證mlx-audio v0.4.5的混合量化相容層、strict load、真實音訊輸出、
離線運行、RAM、RTF與依賴體積，產出可重現report與lock files。
P0也需量測累積音訊重辨識的總成本與後文修訂效果。
P0通過後按P1→P2→P2a逐步實作；本輪若只要求P0，就停在P0交付。
P2a是正式的邊說邊修訂需求，使用partial完整替換與不可變final，
先保留單一ASR模型，不預設增加常駐LLM。Mac client分浮動預覽P5a與IME組字P5b。

保留單模型、bounded queue、worker隔離、typed errors、來源sample clock。
不得靜默替換模型、fallback mock、宣稱未測的串流／翻譯／時間戳能力。
使用codebase-memory-mcp探索已存在程式；有新程式時更新索引。
若實機驗證無法執行，完成可執行的spike與測試，清楚列出阻礙與未驗證項目。
每次交付說明已實作、已測、未測與下一階段；同步更新文件中的完成狀態。
```
