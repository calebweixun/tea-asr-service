# 預覽節奏（preview cadence）量測報告

執行日期：2026-09-27。狀態：**已實作，預設 `preview_min_interval_ms=300`、`preview_min_audio_ms=300`、
`preview_load_factor=2`**（原本固定 800 ms／800 ms）。設計見 [07](../07-contextual-streaming.md)「輕量化與排程」，
契約見 [04](../04-api.md) 的 `preview_policy`。

## 要回答的問題

使用者用這個 server 跑 OBS 即時字幕，講者語速快：字幕出得晚，而且一次跳 4–5 個字。
可修訂預覽原本最多每 800 ms 跑一次、而且要累積 800 ms 新音訊；`transcript.stable`（LocalAgreement-2）
要兩版預覽一致才提交，所以已提交文字落後約 1–2 秒。離線 RTF 約 0.03（5 秒片段約 0.17 秒），
worker 大部分時間是閒的，節奏是保守而不是 GPU 不夠。

要找的是：把預覽間隔與新音訊門檻降到多少，提交延遲與每次跳出的字數能明顯下降，
同時單一 worker 不被預覽吃滿、final 不變慢、第二個 session 不被擠掉。

## 改了什麼

1. **可設定的門檻與負載保護。** 下一次預覽要同時滿足：自上次發布後有 `preview_min_audio_ms` 新音訊，
   而且距上次預覽**開始**至少 `max(preview_min_interval_ms, preview_load_factor × 上次預覽解碼時間)`。
   解碼時間＝worker 呼叫的實際 wall time，不含排隊。k=2 表示單一 session 的預覽最多占 worker 50%。
   間隔未到時用 timer 在門檻打開的那一刻重排，不必等 client 的下一個 frame。三個值是 server 設定
   （config.toml `[service]` 或 `TEA_ASR_PREVIEW_MIN_INTERVAL_MS`／`TEA_ASR_PREVIEW_MIN_AUDIO_MS`／
   `TEA_ASR_PREVIEW_LOAD_FACTOR`），範圍 100–5000 ms 與 0–10，超出拒絕啟動；client 不能要求更快。
2. **過期預覽不再排在 final 後面跑。** 排程本來就是 `interactive > realtime > preview`（final 先於預覽，跨 session
   共用同一個佇列）。但片段封口時，還在排隊的那次預覽會留在佇列裡，等 final 跑完後照樣占用 worker 一次
   （結果直接丟掉），而它無法中途打斷：這段時間本 session 的下一段、以及其他 session 的 final 都要等。
   現在封口時把它從佇列移除（`Scheduler.drop_stale()`），stop／commit 也不必等它跑完才送出 final。
   已經在 worker 上跑的那一次仍無法搶占，最多讓 final 多等一次預覽解碼（本量測預覽解碼最大 0.28 秒）。
3. `session.started.preview_policy.min_interval_ms`／`min_audio_ms` 回報實際生效的設定值（docs/06 約束 6）。
   wire 欄位沒有增減。

## 設定

- 機器：Apple M4 Pro、48 GB、macOS 27.0（26A428）；Python 3.12.11、MLX 0.32.2。
  模型 `Alkd/TEA-ASR-1.1-MLX-4bit` revision `caee57a908b6d64be08a6462c7a21ececbd4d7cb`，`HF_HUB_OFFLINE=1`。
- 同一台機器上另有使用者自己的 tea-asr server（port 8327，另一份模型）在跑，量測期間它的負載未知、無法控制。
- 音訊：`benchmarks/capture_event_trace.py build-wav` 產生的 103.8 秒 WAV（sha256 `94c547d1…2dc49`）：
  [Taiwan-Tongues zh-TW](https://huggingface.co/datasets/adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw)
  revision `ff0e8047…`、test 分片前 32 句朗讀，句間插入 400／700／1000／1500 ms 數位靜音。
  **是多位朗讀者的句子拼接，不是自然的單一講者快語速。**
- Session：continuous＋revisable＋`stable.agreement=2`、`segmentation.end_silence_ms=870`（使用者的設定），
  每 100 ms 送一個 frame、1 倍即時。每次量測都重新啟動 server（隔離 HOME、臨時 token、port 8399），
  先用一次 2 秒 HTTP 辨識暖機，暖機那次不計入 worker 時間。
- Baseline 是 `main`（c3829c9）的原始碼（`git archive c3829c9 src`），不是新程式碼設成 800/800。
  `fixed-N` 是新程式碼、門檻 N/N、`preview_load_factor=0`（關閉負載保護）；`adaptive-N-kK` 是 N/N＋k。
- Baseline 與 `adaptive-300-k2` 各跑 3 次、`fixed-300`／`fixed-400` 各 2 次，其餘 1 次。模型是確定性的，
  重跑之間只有時間抖動（partial 數、改寫數幾乎一樣）。

## 指標

由 `benchmarks/preview_cadence_eval.py` 計算。clip＝WAV 裡的一句，用 manifest 的樣本範圍與參考文字；
一個 clip 對應「final 樣本範圍涵蓋該句中點」的那一段（相鄰句靜音短於端點時會併在同一段）。
比對前去掉標點與空白。用兩個字而不是一個，因為單一個中文字太常重複，無法認出是哪一句。

| 代號 | 定義 |
|---|---|
| a0 首字 | `speech.started` → 該段第一個 partial（不論內容對不對） |
| a 句首 | 句子音訊開始 → 該段第一個含「句首兩個參考字」的 partial |
| b 提交 | 句子音訊結束 → 該段第一個含「句尾兩個參考字」的 `transcript.stable`；若 stable 從未含（辨識錯），改用該段 final |
| c 每次增加字數 | 每個 `transcript.stable` 比前一版多的字數；每個變長的 partial 多出的字數 |
| d worker 忙碌 | （預覽＋final 解碼時間）／第一個到最後一個 worker 呼叫的時間跨度；括號內只算預覽。由 `benchmarks/serve_timed.py` 量 |
| e final | 片段 `end_sample` → `transcript.final`（含等句尾靜音）；以及 `segment.queued` → final（節奏唯一能拖慢的部分） |
| f 改寫 | partial 不以該段已提交 stable 開頭的次數；最後 stable 為 `diverged` 的段數（共 25 段） |

時間單位 ms，格式「中位數 / p95」或「中位數 / p95 / 最大」。每次量測 30–32 個 clip、25 段 final。

## 結果：單一 session

| run | a0 首字 | a 句首 | b 提交 | c stable 增字 | c partial 增字 | d 忙碌（預覽） | 預覽解碼 | e final | e 排入→final | f 改寫 | diverged |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 188 / 426 | 747 / 1686 | 875 / 3737 | 3 / 7 / 11 | 3 / 6 / 7 | 0.16 (0.13) | 119 / 175 / 206 | 763 / 843 | 116 / 195 | 20/102 (20%) | 6 |
| baseline-r2 | 173 / 381 | 675 / 1695 | 866 / 3769 | 3 / 7 / 11 | 3 / 7 / 9 | 0.15 (0.12) | 117 / 164 / 196 | 774 / 829 | 117 / 167 | 24/103 (23%) | 7 |
| baseline-r3 | 176 / 495 | 806 / 1700 | 883 / 3830 | 3 / 7 / 11 | 3 / 7 / 9 | 0.16 (0.13) | 114 / 189 / 220 | 785 / 879 | 124 / 228 | 24/103 (23%) | 8 |
| fixed-500 | 85 / 107 | 911 / 1938 | 372 / 2406 | 2 / 6 / 10 | 3 / 5 / 6 | 0.22 (0.19) | 114 / 165 / 201 | 778 / 826 | 120 / 163 | 37/160 (23%) | 8 |
| fixed-400 | 79 / 125 | 847 / 1948 | 401 / 3782 | 2 / 4 / 10 | 2 / 4 / 8 | 0.25 (0.22) | 113 / 159 / 216 | 767 / 835 | 115 / 212 | 48/188 (26%) | 8 |
| fixed-400-r2 | 95 / 157 | 866 / 1985 | 401 / 3843 | 2 / 4 / 10 | 2 / 4 / 8 | 0.28 (0.25) | 127 / 210 / 234 | 796 / 867 | 143 / 189 | 48/187 (26%) | 8 |
| fixed-300 | 90 / 130 | 760 / 1936 | 122 / 3793 | 2 / 4 / 10 | 2 / 5 / 10 | 0.31 (0.28) | 116 / 194 / 253 | 786 / 876 | 138 / 230 | 58/218 (27%) | 9 |
| fixed-300-r2 | 75 / 136 | 750 / 1953 | 118 / 3782 | 2 / 4 / 10 | 2 / 5 / 10 | 0.30 (0.27) | 109 / 188 / 238 | 784 / 857 | 139 / 190 | 58/217 (27%) | 9 |
| **adaptive-300-k2** | 75 / 102 | 739 / 1951 | 106 / 3790 | 2 / 4 / 10 | 2 / 5 / 10 | 0.27 (0.24) | 102 / 149 / 191 | 765 / 822 | 121 / 151 | 58/220 (26%) | 9 |
| **adaptive-300-k2-r2** | 84 / 193 | 739 / 1942 | 100 / 3794 | 2 / 4 / 10 | 2 / 5 / 10 | 0.28 (0.25) | 106 / 164 / 279 | 774 / 871 | 129 / 197 | 58/216 (27%) | 9 |
| **adaptive-300-k2-r3** | 78 / 101 | 740 / 1948 | 127 / 3778 | 2 / 4 / 10 | 2 / 5 / 10 | 0.27 (0.24) | 105 / 150 / 179 | 768 / 831 | 115 / 169 | 58/219 (26%) | 9 |
| adaptive-300-k3 | 88 / 146 | 754 / 1937 | 186 / 3886 | 2 / 4 / 10 | 2 / 5 / 10 | 0.28 (0.25) | 106 / 180 / 231 | 786 / 883 | 132 / 220 | 55/211 (26%) | 9 |
| adaptive-200-k2（探索） | 80 / 96 | 668 / 1877 | 24 / 3831 | 2 / 4 / 8 | 2 / 4 / 10 | 0.36 (0.33) | 103 / 148 / 211 | 766 / 893 | 120 / 206 | 101/304 (33%) | 10 |

讀法：

- **提交延遲（b）是最大的改善**：中位數 0.87 秒 → 0.10–0.13 秒（300/k2 三次）。p95 沒有變（約 3.8 秒），
  因為那幾句（5–6 句）的句尾兩字 stable 從未提交，要等 final；併段時 final 在下一句講完後才出來，
  這受切段與 LocalAgreement 規則限制，不是節奏能解的。
- **每次跳出的字數（c）變小**：stable 每次增加 p95 7 → 4 字、中位數 3 → 2；partial p95 6–7 → 5。
  最大值 10–11 不變（final 收尾那次一口氣補完）。
- **首字（a0）快一倍**：中位數約 180 → 約 80 ms、p95 380–500 → 100–190 ms。
- **句首兩字（a）沒有變快**（中位數 0.67–0.81 → 0.74 秒）：預覽早出了，但模型要聽到足夠音訊才會吐出正確的
  字——早的預覽常是錯字（例：`池中` 之後才是 `遲遲`），併段的句子則要等約 1 秒模型才不再只重複上一句。
  這是模型本身的收斂速度。
- **worker 忙碌**：單一 session 0.16 → 0.27（預覽 0.13 → 0.24）；final 延遲不變（中位數 763–785 → 765–774 ms、
  排入→final 116–124 → 115–129 ms）。
- **代價：改寫變多**。partial 不以已提交 stable 開頭的比例 20–23% → 26–27%，diverged 段 6–8 → 9（共 25 段）。
  預覽越密，LocalAgreement 越常在兩版快照一致時提前提交、之後被 final 推翻；200 ms 時升到 33% 與 10 段。

## 預設值的選擇

**300 ms／300 ms／k=2。**

- 相比 400／500：提交延遲中位數 0.10 秒對 0.37–0.40 秒，每次增字相同或更小；忙碌差不多（0.27 對 0.22–0.28），
  改寫 26% 對 23–26%、diverged 9 對 8。多付一段 diverged，換提交延遲再少 0.3 秒。
- 相比 200：200 的提交再快約 0.08 秒，但改寫 33%、diverged 10，單一 session 已 0.36 忙碌，
  兩個 session 預估約 0.7，超過每 session ≤50% 的目標也壓縮 final 的餘裕。
- k=2 對 k=3：這批音訊的預覽解碼中位數約 0.1 秒，k=3 時 3×0.1＞0.3 經常壓過下限，提交延遲中位數 186 對 106 ms；
  k=2 只在解碼超過 150 ms（長片段）才生效。相比關閉（fixed-300），k=2 忙碌 0.27–0.28 對 0.30–0.31，
  其他指標相同；它的作用在長句與高負載，保證單一 session 的預覽不超過 worker 一半。
- final 延遲沒有變差（上表 e 欄、下節兩 session），符合「final 不得延後」。

## 兩個 session 同時

server 預設 `max_continuous_sessions=2`（`/v1/capabilities` 的 `limits` 也回 2），所以可以測。
第二個 session 晚 5 秒開始、送同一份音訊。

| run | session | a0 首字 | a 句首 | b 提交 | d 忙碌（預覽） | e final | e 排入→final |
|---|---|---|---|---|---|---|---|
| baseline | A | 174 / 501 | 785 / 1674 | 896 / 3766 | 0.15 (0.12) | 776 / 868 | 128 / 190 |
| baseline | B | 177 / 586 | 777 / 1669 | 888 / 3717 | 0.15 (0.12) | 783 / 853 | 123 / 222 |
| adaptive-300-k2 | A | 74 / 102 | 744 / 1928 | 108 / 3784 | 0.26 (0.23) | 765 / 825 | 117 / 158 |
| adaptive-300-k2 | B | 74 / 191 | 742 / 1920 | 105 / 3787 | 0.26 (0.23) | 787 / 846 | 132 / 212 |

worker 整體忙碌：baseline 0.29（預覽 0.23），adaptive-300-k2 **0.49**（預覽 0.44）。兩個 session 的 final
延遲與單 session、與 baseline 雙 session 相同；沒有 `segment.error`、`preview.status` 或 `error`。
兩個 session 各自拿到跟單獨跑時一樣的預覽節奏，沒有一邊被擠掉。

## OBS 外掛重播

`tea-live-subtitle`（bdd4665）的 `tests/replay/caption-replay`，預設使用者設定
（fade 1500 ms／200 ms、2 行、3 句、1800 px、顯示未確認文字）。

| run | 講話中淡出 | 尾端收回 | 未確認尾字被改 | 單一畫面最多新增字 | ≥8 新字的畫面 |
|---|---|---|---|---|---|
| baseline（3 次） | 0 / 0 / 0 | 0 / 0 / 0 | 27 / 27 / 26 | 10 / 10 / 10 | 3 / 4 / 5 |
| fixed-500 | 0 | 0 | 33 | 9 | 3 |
| fixed-400（2 次） | 0 / 0 | 0 / 0 | 44 / 44 | 9 / 9 | 3 / 3 |
| fixed-300（2 次） | 0 / 0 | 0 / 0 | 46 / 46 | 14 / 14 | 4 / 4 |
| adaptive-300-k2（3 次） | 0 / 0 / 0 | 0 / 0 / 0 | 46 / 46 / 46 | 14 / 14 / 14 | 4 / 4 / 4 |
| adaptive-300-k3 | 0 | 0 | 45 | 14 | 3 |
| adaptive-200-k2 | 0 | 0 | 57 | 14 | 5 |
| 兩 session baseline A / B | 0 / 0 | 0 / 0 | 22 / 23 | 10 / 10 | 2 / 4 |
| 兩 session adaptive-300-k2 A / B | 0 / 0 | 0 / 0 | 46 / 46 | 14 / 14 | 4 / 4 |

兩種節奏都沒有淡出與收回。「未確認尾字被改」幾乎翻倍，跟 partial 數量翻倍一致（每個 partial 的比例
26% → 21%），是畫面上灰色未確認字更常換字。最大一次 14 字出現在 42.9 秒：模型在某個快照幻覺出
`它會告訴您，您離成功還有多遠`，未確認尾字整段被換掉；baseline 剛好沒取到那個快照。這是模型的錯誤，
更密的預覽只是更容易撞到，也會更早被下一版換掉。已提交（stable）文字的跳動以上面 c 欄為準。

## 重現

路徑以 `$S` 表示暫存目錄；`$PY` 是有 dev 依賴的 Python 3.12（例如 `uv run python`）。

```sh
# 0. 音訊（快取的語料，不下載）
HF_HUB_OFFLINE=1 $PY benchmarks/capture_event_trace.py build-wav --out $S/traces/speech.wav

# 1. baseline 原始碼
mkdir -p $S/baseline && git archive c3829c9 src | tar -x -C $S/baseline

# 2. 每一次量測：隔離 HOME、port 8399、計時包裝（SRC 為 $S/baseline/src 或本 checkout 的 src）
export HF_HUB_OFFLINE=1 TEA_ASR_MODELS_DIR=<checkout>/models
TEA_ASR_PREVIEW_MIN_INTERVAL_MS=300 TEA_ASR_PREVIEW_MIN_AUDIO_MS=300 TEA_ASR_PREVIEW_LOAD_FACTOR=2 \
HOME=$S/home-run PYTHONPATH=$SRC TEA_TIMING_LOG=$S/traces/cadence-run.timing.jsonl \
  $PY benchmarks/serve_timed.py serve --port 8399 &
# 等 /readyz 回 200；POST 2 秒音訊到 /v1/transcriptions 暖機；清空 timing log
$PY benchmarks/capture_event_trace.py capture --wav $S/traces/speech.wav \
  --url ws://127.0.0.1:8399/v1/stream --token-file "$S/home-run/Library/Application Support/TEA ASR/token" \
  --end-silence-ms 870 --agreement 2 --out $S/traces/cadence-run.jsonl
# 兩 session：同一個 server，第二個 capture 晚 5 秒啟動，--out 分別 -a／-b
kill %1

# 3. 指標
$PY benchmarks/preview_cadence_eval.py --manifest $S/traces/speech.manifest.json \
  --run run=$S/traces/cadence-run.jsonl:$S/traces/cadence-run.timing.jsonl
#   兩 session：--run two=<a.jsonl>,<b.jsonl>:<timing.jsonl>

# 4. OBS 外掛重播（在外掛 repo 內編譯到暫存目錄，不修改該 repo）
cc  -std=c11   -c -Itests/stubs -Isrc src/caption-state.c -o $S/caption-state.o
c++ -std=c++17 -Itests/stubs -Isrc tests/replay/caption-replay.cpp $S/caption-state.o -o $S/caption-replay
$S/caption-replay $S/traces/cadence-run.jsonl --quiet
```

原始 trace、timing log 與彙整 `cadence-summary.json` 留在量測時的暫存目錄，未納入 repo。
量測時用的計時包裝是 `benchmarks/serve_timed.py` 的前一版（行為相同，只差 docstring、型別註記與 import 位置）。

## 未驗證

- 音訊是**多位朗讀者的句子拼接**，句間是數位靜音，不是自然的單一講者快語速；快語速下每秒字數更多，
  每次增字與改寫率可能不同。
- 只測了一台機器、一個模型、一種 frame 大小（100 ms）；OBS 外掛實際送的 frame 大小與網路抖動沒有測。
- 同機的使用者 server（8327）負載未知，可能影響解碼時間。
- 片段長度 1.6–5.7 秒，負載保護（解碼 >150 ms 才生效）在 10 秒以上長句的效果只有單元測試，沒有真實量測。
- 真實 OBS 畫面觀感（跳動、易讀性）只有 replay 模擬，沒有人眼確認。
