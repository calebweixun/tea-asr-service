# 06｜給 Sol 與其他實作模型的交接

> 原始交付為文件。**2026-09-19更新：** P0–P3與P2a已完成並實測；P2一小時soak與N=1..4併發容量測試已完成，進度表見 [README](../README.md#實作進度)。
> 以下各階段的完成條件仍然有效，勾選狀態寫在每節開頭。

**2026-10-03 singing detection 狀態：** 改用 YAMNet（ONNX、Apache-2.0，釘版於 `models.lock.json`，`tea-asr model-prepare` 下載）；`singing.py`（特徵、logistic、session 遲滯、片段決策）與 `singing_session.py`（背景 executor，不阻塞收音）取代先前失敗的 DSP heuristic。`segment.audio_class` 與 `transcript.final.audio_class` 預設開啟，但只有 YAMNet 資產載入且 hash 符合時才送事件與宣告 `features.singing_detection`（約束 6）。Held-out 留一檔：speech 視窗誤判 0.0%（兩組 speech）、歌唱片段首判 83.1%／最終 94.9%、29 分鐘講道 0 誤判、首判 1.5 秒，達成目標；證據只有 3 首同一樂團的歌、1 段背景音樂下講話與 1 場講道，**真實 OBS 與其他敬拜團／場地未驗證**，詳見 [評估報告](benchmarks/singing-eval-report.md)。實作完成與實機驗收分開記錄：後者尚待使用者在真實 OBS 上確認。

**2026-10-04 整場主日 SRT 評估（church-eval）：** 用使用者人工訂正的三場主日（各約 2 小時）量離線 CER、串流字幕與歌唱偵測，工具見 [docs/09](09-testing-guide.md)，數字與決策見 [報告](benchmarks/church-eval-2026-10.md)。結論：replacement 顯著有益（-0.10 pp）、prompt 在 4-bit 無效、carry 3 s 離線不顯著／串流略好、870 ms 句尾靜音優於 600 ms；**歌唱偵測的誤藏率約每個有字幕小時 3.1 次（目標近 0，未達成，門檻與遲滯無法在不損失偵測的情況下消除）**。串流延遲是兩個併發 session 下的上限（單 session 重跑 p95 0.7–1.8 秒）；真實 OBS 畫面未驗收。

**2026-10-04｜ASR 模型變體（server）：** 預設仍是釘版 `tea-1.1-mlx-4bit`；只有使用者在 `[service].asr_model` 或 `TEA_ASR_MODEL` 明確選 `tea-1.1-mlx-8bit` 才使用 8-bit。8-bit 是 `JacobLinCool/TEA-ASR-1.1` revision `bda08df76d4fd6b487b4a1dd7f0bddf8541696f8` 的本機 MLX 衍生檔，11 個檔案以 SHA-256 固定於 `models.lock.json`；`model-prepare --variant 8bit` 只驗證本機副本。缺檔或 hash 不符時回報 `model_unavailable`、記錄錯誤並在 doctor 顯示，不會載入 4-bit。授權鏈為 TEA-ASR-1.1 MIT 衍生檔，並保留 Qwen3-ASR-1.7B Apache-2.0 通知，細節見 `NOTICE`。

離線人工訂正 SRT 評估中，8-bit + carry 3 s 相對 4-bit + carry 3 s 的 CER 差為 **−0.27 pp [−0.44, −0.10]**，95% CI 不含 0；模型檔大小增加 **92%**。這是三場同一教會服務的離線結果，不代表即時串流成本或其他場地的品質。真實 server 10 分鐘對照尚待 coordinator 執行 [`/Users/c2leb/Codes/tea-asr-service/.soak/run-8bit-check.sh`](/Users/c2leb/Codes/tea-asr-service/.soak/run-8bit-check.sh)：載入時間、worker RSS、preview decode p50/p95、worker busy、final latency p95 與首 6 分鐘 CER **待填**；完成前不得宣稱這些即時指標已驗收。

**2026-10-05｜標點補回（server）：** 新增 `punctuation.py`：FunASR CT-Transformer（sherpa-onnx int8 ONNX、Apache-2.0、壓縮檔與模型 sha256 釘在 `models.lock.json`，`model-prepare` 選用下載），onnxruntime CPU 1 thread、專用 executor，只插入 `，。？、`、不改其他字元，partial 與 final 在 stable 之前處理（partial 尾端 2 字不插、stable 同樣保留尾端 2 字），`raw_text` 不變、`warnings` 加 `punctuation_restored`；`features.punctuation_restore` 只在資產載入且 hash 相符時宣告（約束 6）。設定 `punctuation_restore_enabled`／`TEA_ASR_PUNCTUATION=1`，**預設關閉**。8-bit 10 分鐘串流：每 52 字一個標點 → 12 字（人工答案 12.5）、無標點段 69%→8%、最長無標點連續字 34→15（中位）、stable 提交與 final 延遲持平、layout moves／duplicates 0；代價是 partial 改寫率 +4.5 pp、OBS replay 的 row-limit 縮短 7→43（`comma-min 8`）與 final 改寫已顯示文字 24→34。離線 39 句召回 0.20→0.69、F1 0.33→0.62，CER 不變。見 [評估報告](benchmarks/punctuation-report.md)。**OBS 實際畫面、其他講者與場地、4-bit 串流未驗收；使用者看過 OBS 前不改預設。**

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

**狀態：已通過。** 報告見 [benchmarks/p0-report.md](benchmarks/p0-report.md)；指定checkpoint的效能與品質驗證均已完成。

**完成條件：** 相容層能載入指定checkpoint；至少中文／混英／短詞／靜音有實測；報告含memory、RTF、token限制與版本；models.lock與uv.lock可重現。若未有可用硬體或音訊，交付可執行spike與明確未驗證狀態，不能標P0通過。

## P1｜短音訊走完整API

**前置：** P0確認後端；無硬體時只可先做標明mock的契約工作。

**工作：** 設定、bearer驗證、CLI serve/doctor/status/`tea-asr model-prepare`、supervisor＋IPC、HTTP transcription、health/ready/capabilities、typed errors、單模型與資源上限。模型與 runtime 準備由 server 明確擁有；Mac GUI 僅可呼叫、引導或診斷 `tea-asr model-prepare`，不得靜默下載模型或自行建立 ASR runtime。Pydantic生成OpenAPI，開始WS event schema。只做PCM與WAV CLI轉換，暫不安裝FFmpeg。

**狀態：已完成。** `tea-asr transcribe` 經HTTP取得真結果；worker以受監督子程序隔離、stdout只走IPC；bounded scheduler提供真實 `queue_ms` 與 `queue_full`；typed error envelope、Pydantic wire models與 [docs/api/](api/) 的OpenAPI／WS schema皆已產生並由測試檢查。

**完成條件：** 一個CLI client可送短音訊並取得真結果；worker推論時health仍回應；卡死能回收；缺模型不假裝成功；錯格式／超body／超queue測試通過；worker stdout無log污染。

建議提交分成：domain與schemas、worker adapter、HTTP＋CLI。不要在API函式直接呼叫同步generate。

## P2｜即時音訊與連續轉錄

**工作：** WS狀態機、binary seq header、sample clock、utterance commit、continuous VAD、排程、公平性、flow window、取消與slow reader。建立 `stream_wav.py` 以真實時間送frame，不需先取得麥克風權限。

**狀態：已實作，長跑與併發容量測試完成。** WS狀態機、binary seq header、sample clock、flow window、commit/stop/cancel、audio.ack、segment終局事件、bounded outgoing queue與慢client偵測已完成。Silero VAD以ONNX Runtime接上（每session獨立recurrent state，不引進Torch），`models.lock.json` 已固定 revision 與 sha256。continuous profile由VAD切段，pre-roll 600 ms、句尾靜音 500 ms（revisable 900 ms）、最短語音 160 ms、12秒上限＋2秒grace（校準依據見 [P2切段報告](benchmarks/p2-segmentation-report.md)）；片段經有限佇列交給單一consumer依序處理，推論不阻塞收音。
**2026-09-27：** continuous session 可在 `session.start.segmentation.end_silence_ms`（300–3000 ms）指定句尾靜音，省略時維持上述預設；`preview_policy.endpoint_silence_ms` 回報實際生效值，`capabilities.features.segmentation_control` 只在 continuous 可用時宣告範圍（docs/04「切段控制」）。只有句尾靜音可調，pre-roll、最短語音、上限與切分邏輯不變；500／900 以外的值沒有校準過。
**一小時長跑已通過**（386段0錯誤、延遲p95 0.57秒、記憶體平穩，見 [長跑報告](benchmarks/p2-soak-report.md)），VAD參數也已用真實口語校準。
**併發容量：** 已測試 N=1..4；預設 `max_continuous_sessions=2`，以實測結果作為 continuous 的預設上限。

**完成條件：** 按sample順序產生不可變final；ASR推論中仍能收音；一小時不持續增加RAM／lag；停止時flush；每個已排片段都有終局；profile與能力宣告符合實際。根據併發實測設定max continuous sessions。

**v0.1可發行條件：** P0–P2通过；README標明ephemeral、無resume、無精準timestamps、無翻譯。發布測試用wheel或可重現uv安裝說明；不要稱為完整會議產品。

## P2a｜串流預覽與上下文修訂

**前置：** P2基線可靠；P0已有preview成本報告。這是正式產品需求，不再只列為可選preview。

**工作：** 閱讀 [07](07-contextual-streaming.md)；實作累積音訊快照、preview排程與預算、最新待跑任務合併、900ms端點grace、固定segment ID、partial完整替換與revision、final優先、負載降級。依04擴充wire 1.1；reference client能在同一row修訂文字。前文prompt独立實驗，不阻擋同片段後文修正。

**狀態：驗收通過，預設開啟。** 量測見 [P2a 報告](benchmarks/p2a-preview-report.md)：首次可見延遲 p95 0.91 秒、
final 與 final-only 模式完全一致、混合負載下 23/23 HTTP 辨識成功且 10 段 final 全數產生。
`TEA_ASR_REVISABLE_PREVIEW=0` 或 config.toml 可關閉，關閉時 revisable 請求回 `unsupported_option` 而非靜默降級。
未涵蓋：單一語者與單一機器、預覽降級路徑未在真實過載下觸發。
**2026-09-28｜重複輸出 guard：**partial/final 的 `text` 預設最多保留三個連續重複單位；單字元與 2–6 字元單位可分別用 `repetition_single_char_limit`／`repetition_multi_char_limit` 及對應的 `TEA_ASR_REPETITION_*_LIMIT` 環境變數調整。當時含十進位數字或 CJK 數字字元的重複單位會整段保留。ASCII 字母單位只在兩側都沒有 ASCII 字母時修剪。final 的 `raw_text` 保持原樣並以 `repetition_trimmed` 警告標記；INFO log 只記 segment index、類型、單位長度與移除字元數。trim 後 partial 若短於已提交 stable 前綴，tracker 會保留原前綴、不發縮短的 stable 更新。

**2026-09-27：** 預覽節奏改為 server 設定 `preview_min_audio_ms`／`preview_min_interval_ms`（預設 300／300，原固定 800／800）加負載保護 `preview_load_factor`（預設 2，間隔至少 2×上次解碼時間）；封口時排隊中的預覽直接從 scheduler 移除，不再排在 final 後面跑。`preview_policy` 回報實際值，wire 欄位不變。實測（[預覽節奏報告](benchmarks/preview-cadence-report.md)）stable 提交延遲中位數 0.87→0.10 秒、兩 session 合計 worker 忙碌 0.49、final 延遲不變；partial 改寫率 20–23%→26–27%。語料是拼接的朗讀句，自然快語速與真實 OBS 畫面未驗證。

**已知限制（stable diverged）：** continuous 預覽快照涵蓋最新分析過的 VAD window，包含句尾 grace 期間的靜音；final 片段則依最後語音位置只保留 `tail_ms`。因此預覽確實可能看見比 final 範圍更多的音訊。這符合 partial 的暫定語意：長度隨即時快照前進，final 仍是唯一正式稿。當 length-proportional 假 worker 的預覽文字因此比 final 長，穩定前綴保留已提交內容並以 `diverged` 收尾是正確結果。`tests/unit/test_segmenter.py::test_live_preview_may_include_silence_trimmed_from_the_final_range` 確認此範圍差；`tests/unit/test_scheduler.py::test_next_worker_call_starts_after_finished_result_is_handled` 確認 PR #11 沒有把下一次 worker 呼叫移到前一結果處理之前。真實音訊上的這類差異尚未單獨量測。
**2026-09-28｜Recognition hints：** server dictionaries、profiles 與 deterministic replacements 由預設關閉的 `TEA_ASR_CONTEXT_HINTS=1` 啟用；replacement 保留 `raw_text`，stable 遇到已提交邊界時保持 append-only。`GET /v1/dictionaries` 會逐檔回報錯誤字典，不影響有效項目，並以 `context.dictionary_invalid` WARNING 記錄 name/reason；`session.start` 仍拒絕無效 profile。Domain/hotwords prompt 分離為 `TEA_ASR_CONTEXT_PROMPT=1`，且需 hints 已啟用；prompt 預設關閉且維持 experimental。`session.started.context` 不含 prompt token count；backend 每次 request 回報的數量只寫 server log。4段真實講道音訊 smoke 未見 prompt 改善（約多150 tokens；一處聖經→聖家、一處失去標點，住棚節變體未修正），已審閱的 deterministic replacement 是建議工具。完整 CER、數字／否定詞與錯誤替換風險仍未評估。`use_previous_finals` 延後。

**2026-10-03｜Final 左上下文 carry：** 已實作 server 端 final-only PCM 前綴與共用 overlap stripper；無可信重疊時保守重跑 segment-only，`raw_text` 保留實際採用那次解碼的原文，warning 為 `carry_overlap_stripped`／`carry_overlap_uncertain`。preview 不帶前文，避免多次推論都增加 L 秒，stable 仍逐段 append-only。答案集轉段間隔在 1.5 秒內：speakers 35/39、music-8900 15/20。離線 MLX 評估尚未執行；`carry_context_s` 預設 **0（關閉）**，不得在未驗收 speakers 的 paired bootstrap CI、music CER 與 duplication count 前打開。Coordinator 執行 `/Users/c2leb/Codes/tea-asr-service/.soak/gold/run-carry-eval.sh`；方法見 [07](07-contextual-streaming.md)。

**2026-10-04｜數字迴圈 guard：**含數字或 CJK 數字的 1–6 字元單位，連續超過 `[service].repetition_numeral_loop_limit`（預設 6）後會保留三份；環境變數為 `TEA_ASR_REPETITION_NUMERAL_LOOP_LIMIT`。單一十進位數字需至少連續 10 次才修剪，且單位緊鄰其他十進位／CJK 數字時保留，以免切斷長數字。數字候選規則移至 dict miner review 的 `Number formatting (style)` 區，不會寫進安全 TOML；一般單詞中偶然含一個中文數字字元的候選仍可保留。2026-10-04 離線掃描 OBS trace 與 traces 目錄的 13 份 JSONL、共 19,944 個文字欄位，沒有新增數字迴圈修剪；共重現 42 個一般重複修剪事件、移除 6,670 字元。這是舊輸出的離線分析，不代表新版 server 的真實模型品質已驗收。

**2026-10-04｜Dictionary editing API：**新增 authenticated list/detail、PUT create/update、soft-delete 與 saved/draft preview。寫入套用 loader 同一組欄位長度／數量限制與重複來源檢查，使用 base revision 衝突保護、canonical TOML、原子替換與每名字典最近 30 份 `.history`；非 loopback 編輯需另外設定 `dictionary_remote_edit=true`，預設關閉。session start 凍結字典快照，編輯只影響下一個 session。API schema 與具體驗收結果記錄於 [04 API 契約](04-api.md) 及 [10 session handoff](10-session-handoff.md)。

**2026-10-05｜Mac app 字典頁：**側欄新增「字典」頁（AppKit，build-once + update）：清單（數量、更新時間、格式錯誤標記）、新增／複製／重新命名（先 PUT 新名再 DELETE 舊名）／刪除（可從 `.history` 救回）、對照表（排序、搜尋、行內驗證、server 422 標到對應列、多行貼上、TOML 匯入／匯出）、用未儲存草稿的測試區，以及帶 `base_revision` 的儲存（409 可覆寫／重新載入；新增用 `base_revision: null`，不會覆蓋同名）。已用真實 server（FakeSupervisor、隔離 HOME、port 8452）跑完 create→edit→preview→save→reload→409→422→rename→delete 並核對 canonical TOML 與 `.history`；`swift test` 全綠。**實機點擊操作（儲存格編輯、⌘V/⌘S、各對話框）與 OBS 端生效時機未經使用者驗收。**

**完成條件：** 能呈現「先出字→後文修正→定稿」；partial不重複append、不改已final內容；重跑總RTF與品質、延遲符合07或有明確未達標報告；preview超載不阻塞收音與正式排程。測試同音詞、數字、否定詞、中英混用與cancel/final競態。

**v0.1.1可發行條件：** P2a驗收通過才宣告partial_transcripts=true。未達標可交付研究與改善方案，但不能把需求標成完成或靜默移除。保留final-only模式；P4再驗證durable_revisable。

## P3｜使用體驗與本機服務管理

**工作：** 安裝流程、模型下載進度、設定路徑、診斷訊息、idle unload/keep_warm、rotation、singleton、LaunchAgent install/uninstall、sleep/wake。

**狀態：已完成並實測。** `tea-asr service install/uninstall/status` 以絕對路徑建立 LaunchAgent，
只有明確 install 才會動登入設定；`KeepAlive` 僅在 crash 時重啟，避免「已有實例」的正常退出被無限重啟。
singleton 採 flock lock file ＋ port 檢查，第二份實例會被拒絕並回報持有者的 PID 與 port（已實測 LaunchAgent
與手動同時啟動只會有一份模型）。閒置逾時卸載 worker 後 `model_state=idle_unloaded`，第一個請求觸發重新載入
並回 503 `model_loading`（已實測，重新載入 1.7 秒、worker generation 遞增）。設定走 TOML，未知欄位直接報錯。
log 為 JSON lines 並輪替，明確過濾 token、PCM 與逐字稿。關閉時停止收件並最多 drain 30 秒。
**2026-09-27：** `/v1/stream` 每條 session 寫診斷日誌（docs/04「W11」）：lifecycle 行、每 5 秒一行音訊／VAD heartbeat（收件數與間隔、RMS／峰值 dBFS、VAD 最大／平均機率、segmenter 狀態、預覽帳、worker 忙碌比例），以及斷流、太小聲、有聲音但 VAD 判定非語音、排隊過久四種 WARNING（每種每 30 秒最多一行）。只記字數不記文字，不額外呼叫模型；INFO 約 1 MB／小時／session。另有預設關閉的除錯錄音 `debug_capture_audio`（滾動 WAV、有上限），開啟時會落音訊，是 #7 的明確例外，只供使用者自己排查。已用真實模型在測試 port 驗證四種情況可區分；在使用者真實 OBS 串流上的效果未驗證。
**2026-09-28：** 修正 worker IPC 失去同步：session 在預覽推論中途關閉（OBS 改設定後重連）時，被取消的預覽沒讀走自己的 response，之後每個請求都讀到上一個的 response，整個 process 永久回 `invalid_ipc`（實機 heartbeat `preview_failed=16 preview_published=0`）。現在一次寫入＋讀回不受 caller 取消影響（shield，鎖持有到讀完），scheduler 同步到真正結束才放行；另加防線：ID 不符或 frame 壞掉就記 `worker.ipc_desync`、重啟 worker、只讓當下請求失敗。翻譯 worker 同樣補上防線（它原本就有 shield）。已用真實模型在測試 port 以「中途硬斷 A、立刻開 B」重現舊版 bug 並驗證新版不再發生；在使用者真實 OBS 重連流程上未驗證。
**2026-09-28 follow-up：** supervisor 將並行 `start()` 共用同一個內部啟動 task；`stop()` 只取消並等待 restart／內部啟動 task，並清理由 spawn 中途返回的子程序。被 stop supersede 的 `start()` caller 收到 `model_unavailable`，不會被取消；caller 自己被外部取消時仍收到 `CancelledError`，最後一位 caller 離開會取消並回收尚未完成的 spawn。這避免 wake、reload 與 restart 重複啟動或在 shutdown 留下 pipe transport。推論 timeout 立即 kill hung worker，正常 stop/unload 仍保留 graceful wait。單元測試驗證 lifecycle 與 timeout；真實模型 timeout／睡眠喚醒流程尚未實機驗證。

**2026-09-28｜real-audio live-subtitle soak harness：** 新增 `benchmarks/soak_real_audio.py` 的 extract/capture/analyze/replay/report，`benchmarks/replay_regression.py` 對保存的 trace 重跑 plugin caption state machine，並加合成 trace 的 metrics／重複字串測試。Replay 會在 `.soak/build/` 編譯 OBS plugin 測試工具；音訊、trace、session log sidecar 與 review 均留在 `.soak/`，metrics report 不包含逐字稿。合成 trace 的 extract 前置、replay、analyze、report、threshold PASS/FAIL 與 regression replay 已執行；live fake-backend capture 嘗試 bind `127.0.0.1:8422` 時收到 `Operation not permitted`，沒有換 port 重試。**真實模型的 300／600 ms 兩次完整 soak 尚未執行，真實音訊品質與 OBS 畫面尚未驗收**；外部可執行命令見 [09 測試指南](09-testing-guide.md#六真實錄音字幕-soak)。
**2026-09-28｜soak duplication reclassification：**既有 church trace 重新跑 `analyze`／`report`，church-300 的 overall 為 plugin/model/speech 1/1/7，church-600 為 1/3/7；只有 plugin-origin 納入 hard threshold。離線 repetition guard 掃描對 church-300 有4個 trimmed events（3個單位在 final 出現至少兩次），church-600 有22個（20個單位在 final 出現至少兩次）。這些是既有 trace 的離線分析，不代表新版 server 的即時模型品質已驗收；完整 metrics 見 [real-audio soak 報告](benchmarks/soak-real-audio-2026-09-28.md)。
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

**P5a（已有可用版本，見 [clients/macos](../clients/macos/)）：** Swift選單列聽寫client，以驗證真正日常使用的延遲、短詞、焦點與剪貼簿行為。採AVAudioEngine收音及可靠resampling；partial在自有浮動視窗更新，final才貼入。不要為了顯示partial而回刪使用者已打的字。模型與 runtime 準備由 server 明確擁有；Mac GUI 只能呼叫、引導或診斷 `tea-asr model-prepare`，不得靜默下載模型，也不得自行建立另一套 ASR runtime。

**P5a驗收條件：** 可設定輸入裝置與聲道；可設定熱鍵並支援 push-to-talk；開始／停止回饋可選；partial/status overlay 必須是不啟用其他app、不搶焦點的浮動視窗。

四項的實作與單元測試都已完成（收音裝置／聲道、可設定熱鍵與 push-to-talk、可選的開始／停止提示音、`NSPanel` 非啟用 overlay，以及 push-to-talk 才要求的輸入監控權限）；`swift build` 與 65 個 client 測試通過。**尚未實機驗收**：overlay 是否真的不搶前景 app 焦點、push-to-talk 跨 app 的 key-up、熱鍵與選單 key equivalent 是否重複觸發，都只有 policy 層測試，沒有真機證據。另有兩個已知落差待處理：睡眠取消綁在 `sessionDidResignActiveNotification` 而非 `willSleepNotification`，以及 push-to-talk 漏收 key-up 時沒有逾時保護。

**P5b：** InputMethodKit輸入法整合，把partial呈現在自己持有的marked text／組字區，final才commit，交付游標處直接修訂體驗。涵蓋組字生命週期、使用者編輯、焦點變更、取消與安全輸入；實際app相容測試見07。一般選單列app不能直接取代此層。P5a與P5b分開交付，server協定共用。

若先做OBS，先bridge＋text source概念驗證，保留audio callback不阻塞的結構，再做官方plugin。若先做影片，完成編修／翻譯provider設定；若先做會議，先做可見收音狀態、spool與權限流程。

server repo只放reference clients与protocol測試；正式Swift app／OBS plugin可另repo，避免server release綁住多個平台編譯。

## 延後功能的進入條件

| 功能 | 開始之前要有的證據 |
|---|---|
| 跨句全文校訂／可選LLM潤飾 | P2a同片段修訂完成，另定document revision與原稿保存；不可回改ASR final |
| Recognition hints（domain、hotwords、replacement） | **機制已實作，預設關閉**；含 authenticated 字典讀寫與預覽 API，寫入預設限 loopback，遠端編輯需 `dictionary_remote_edit=true`。審閱後的 deterministic replacements 是建議工具。Model prompt 由 `TEA_ASR_CONTEXT_PROMPT=1` 分開啟用且需 hints 已開、預設關閉並 experimental；目標語料 CER、數字、否定詞、prompt echo 與 replacement 誤傷仍待評估 |
| 翻譯 | 來源／目標語言、使用者是否接受雲端、獨立provider／保留原稿。**2026-09-24 已接入（opt-in，預設關閉）**：`netease-youdao/Confucius4-T3PO` 4-bit，獨立 worker 子程序，**只提供 zh→en**（en→zh 回譯 20 句有 18 句含簡體字，不宣告）。只翻 `transcript.final`，譯文走新事件（`translation.started`／`.segment`／`.error`），既有事件與原稿不動。開啟時 ASR final 延遲 p95 約多 0.2–0.3 s（GPU 爭用）。翻譯品質尚無量化分數（缺平行語料），見 `docs/benchmarks/t3po-eval-report.md` |
| Forced alignment | 後端相容性、額外模型memory與對齊品質 |
| Diarization | 多人會議需求、額外模型與重疊發話處理，不以來源軌代替 |
| LAN | 明確需求、TLS／授權／rate limit；不得只改bind至0.0.0.0。**W9已實作，決策記錄：** 明確需求＝macOS client／OBS外掛／其他本機轉錄工具，皆使用者自有裝置；**刻意不做TLS**——使用者在被告知取捨後決定，加密與身分交給WireGuard／Tailscale隧道層，應用層不重複做憑證管理，這是明確取捨而非疏漏，連線與token因此仍是明文，僅限受信任LAN／Tailscale、不得暴露公開網路；授權＝token rotation／revocation（不做expiration／per-token scope，理由見docs/04-api.md）；rate limit＝每來源位址認證失敗次數視窗；bind＝`allow_lan`預設False，未明確opt-in時非本機host一律拒絕啟動，不是只改host；Host／Origin allowlist在LAN模式下限縮為私有網段＋Tailscale CGNAT＋明確列出的額外主機名，不是任意Host都收；細節見docs/04-api.md「W9｜LAN／Tailscale 模式」。 |
| Swift／Rust後端 | P0/P2 profiling指出可量化收益與指定checkpoint品質一致性 |
| 使用者免Python安裝 | clean-Mac package驗證、簽章公證、Metal資產、更新回滾 |

## 開發中不可破壞的約束

1. Production載入失敗不能改用FakeBackend或另一個ASR變體；測試fake必須明確開啟並在status標示。被明確選取的模型缺失或pin不符時回報 `model_unavailable`、寫入錯誤日誌並由doctor顯示。（worker 死亡後依 1/2/4 秒退避重啟，60 秒內最多 3 次，超過即 failed；`model_incompatible` 屬不可重試，不進重啟迴圈。）
2. 每機一個服務instance、一個MLX worker、一份**ASR**模型；不能開多Uvicorn workers。（2026-09-24 修訂：使用者明確同意在 ASR 模型之外，允許**一個**獨立的翻譯 provider 同時常駐，用於同步口譯管線。ASR 仍只能有一份，翻譯 provider 也只能有一份，兩者不得共用或互相替代；翻譯 provider 的記憶體、佇列與逾時同樣受第 4 條約束。）
3. 不在async route、WS receive loop或OBS audio callback阻塞推論。
4. 每層buffer、queue、spool、timeout有上限與可見失敗。
5. Wire sample clock、segment ID、事件終局不隨文字後處理改寫。
6. 模型不等於translator、aligner或diarizer；capabilities只宣告實際驗證能力。
7. ephemeral預設不落音訊／逐字稿；durable ACK必須對應已落盤資料。
8. 版本／來源／benchmark可重現；效能目標不能寫成既有成果。
9. 先完成一個垂直切片，再擴展模組；不要一次生成全部未實作框架。

## 可直接交給實作模型的提示

```text
請在此repo延續TEA ASR Service；不要從P0重新開始，先閱讀本文件的目前狀態與P4／P5當前優先事項。

先閱讀README.md、docs/02-research.md、docs/03-architecture.md、
docs/05-validation.md、docs/06-handoff.md與docs/07-contextual-streaming.md；
API實作時以docs/04-api.md為準。
目前P0–P3與P2a已完成並實測；server與效能驗證結果以本repo的實作、README及`docs/benchmarks/`報告為準。ASR預設仍為釘版4-bit，也可明確選用本機釘版8-bit；8-bit的即時server成本仍待10分鐘真模型對照。P4尚未開始，P5a Mac client 的驗收項目已實作並通過單元測試但尚未實機驗收，P5b尚未實作。

使用Apple Silicon原生Python 3.12、uv與MLX；預設ASR模型為
Alkd/TEA-ASR-1.1-MLX-4bit，固定文件中revision；只有使用者明確設定
`tea-1.1-mlx-8bit` 才載入本機8-bit衍生檔。
如需修改後端，先閱讀既有P0／P2／P2a報告與測試，僅針對當前工作重跑必要驗證；不要重新建立已完成的P0 spike。
新的工作優先完成P4長檔案／保存／恢復，或依明確任務完成P5a Mac client驗收；按本文件各節的完成條件與未驗證限制逐步交付。
P2a是正式的邊說邊修訂需求，使用partial完整替換與不可變final，
每個服務只載入一份ASR模型；選定模型缺失或pin不符就停止，絕不靜默替換。Mac client分浮動預覽P5a與IME組字P5b。

保留單模型、bounded queue、worker隔離、typed errors、來源sample clock。
不得靜默替換模型、fallback mock、宣稱未測的串流／翻譯／時間戳能力。
使用codebase-memory-mcp探索已存在程式；有新程式時更新索引。
若實機驗證無法執行，完成可執行的spike與測試，清楚列出阻礙與未驗證項目。
每次交付說明已實作、已測、未測與下一階段；同步更新文件中的完成狀態。
```
