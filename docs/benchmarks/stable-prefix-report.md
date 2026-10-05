# 只增不改的穩定字幕流（LocalAgreement-n）量測報告

## 2026-10-05｜比較 key 與真實 partial trace replay

**設計：** LocalAgreement 仍使用 n=2，改以比較 key 判斷共同前綴：NFKC → casefold → NFKC，移除 Unicode 標點與空白，並將 `〇零一二三四五六七八九` 逐字折成阿拉伯數字。保留 unit-based 中文數字原樣，不猜測「十／百／千」的數值。新提交部分取自最新 hypothesis；比較區間內的標點只有在所有投票版本於同一 key 位置都含有相同 NFKC 標點序列時才保留。未投票一致的標點略過，不會卡住後續文字。key 邊界仍映射到每個 hypothesis 的 grapheme 邊界，並套用原有英數字詞切點安全檢查。尾端標點由投影自然留在 key 邊界之外，不再以它縮短文字共同前綴。punctuation restore 開啟時，輸入仍沿用 stream.py 的 2 個非標點字元尾端 holdback。

Final 若以相同比較 key 延伸已提交 key，收尾為 `final`：保留已顯示表面字形，從 final 的等價 key 邊界追加尾段。這避免大小寫、字寬、空白及標點差異造成 spurious `diverged`。若 key 不相容，維持 append-only 對齊收尾；`diverged_chars` 計算比較 key 字元，不把略過的標點與空白算成錯字。事件、欄位、sample clock、segment ID 與完整文字（非 offsets）契約不變。

**離線重播：** 以 `benchmarks/stable_trace_replay.py` 讀取 2026-10-05 三份服務端 JSONL；每段只取記錄的 `transcript.partial` 與 `transcript.final`，依事件順序餵給舊的 surface tracker 與新 tracker。punctuation-on trace 套用既有 2 字尾端 holdback。兩份 punctuation-off trace 另移除所有 Unicode 標點後重播。報告僅保存 aggregate 數字，沒有逐字稿文字。jump＝final event 表面字元數 − 最後 open stable 表面字元數；stall＝從首個 partial 到 final 之間，各次 stable 成長間隔的最大值；p90 用 nearest-rank；punctuation count 只數 final 到達前最後一份 open stable。`diverged_chars` 是各 tracker closing update 的總和：Before 沿用舊的表面字元計數，After 是新定義的比較 key 字元計數，因此單位改變也反映標點／格式差異不再算分歧，數值不可當成純逐字錯誤率比較。

| Trace | Replay | Segments | Jump p50 / p90 / max (chars) | Jump ≥20 | Stall p50 / p90 / max (s) | `diverged_chars` sum | Committed punctuation | Open ending in punctuation |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| OFF, 19 segments | Before | 19 | 3 / 57 / 59 | 3 | 2.33 / 11.07 / 11.85 | 144 | 5 | 0 |
| OFF, 19 segments | After | 19 | 3 / 57 / 59 | 3 | 2.03 / 11.07 / 11.85 | 83 | 5 | 0 |
| OFF, punctuation stripped | Before | 19 | 3 / 57 / 59 | 3 | 1.93 / 11.07 / 11.85 | 84 | 0 | 0 |
| OFF, punctuation stripped | After | 19 | 3 / 57 / 59 | 3 | 2.03 / 11.07 / 11.85 | 83 | 0 | 0 |
| OFF, short trace | Before | 6 | 11.5 / 41 / 41 | 2 | 2.79 / 9.61 / 9.61 | 18 | 0 | 0 |
| OFF, short trace | After | 6 | 11.5 / 41 / 41 | 2 | 2.79 / 9.61 / 9.61 | 18 | 0 | 0 |
| OFF short, punctuation stripped | Before | 6 | 10.5 / 41 / 41 | 1 | 2.79 / 9.61 / 9.61 | 18 | 0 | 0 |
| OFF short, punctuation stripped | After | 6 | 10.5 / 41 / 41 | 1 | 2.79 / 9.61 / 9.61 | 18 | 0 | 0 |
| ON, 26 segments | Before | 26 | 11.5 / 45 / 57 | 9 | 3.48 / 9.82 / 10.79 | 216 | 39 | 0 |
| ON, 26 segments | After | 26 | 8 / 42 / 57 | 5 | 1.56 / 7.48 / 10.79 | 112 | 43 | 0 |

標點補回開啟時，≥20 字的 final jump 從 9/26 降為 5/26；p50 11.5→8 字、stall p50 3.48→1.56 秒，max jump 仍為 57 字、max stall 仍為 10.79 秒。OFF 長 trace 的大跳沒有減少（3/19，max 59 字），表示首詞／內容分歧仍主導部分長 stall。所有 replay 版本都沒有以標點結尾的 open stable；比較 key 尾端不另提交標點。

**First-word escape 評估（未上線）：** T=3.0 s、最近 N=3 版、最多跳過 2 個比較 key 字元、之後需至少 2 個共同 key 字元，且候選在所有投票版都符合既有安全切點並以前綴相容已提交文字。三份 trace 共 51 段，0 段符合條件；因此沒有可提前提交的字元、沒有量到 latency gain，也沒有資料支持承擔錯誤鎖定的風險。沒有新增 server setting 或 escape 行為。

**範圍：** 這是既有 OBS session partial/final 的離線回放，不包含新模型推論。OBS 實際畫面、其他講者／場地，以及不同模型輸出分佈仍待使用者驗收。重跑時用相同 interpreter 執行 `PYTHONPATH=src python benchmarks/stable_trace_replay.py --trace <off.jsonl> --trace <short-off.jsonl> --punctuation-on <on.jsonl>`；工具只列 aggregate 指標，不列出 transcript。

執行日期：2026-09-24。狀態：**通過，已實作為 opt-in 的 `transcript.stable`（預設 n=2）。**
契約見 [04](../04-api.md)「只增不改的穩定字幕流」，設計見 [07](../07-contextual-streaming.md)「只增不改的穩定前綴」。

## 要回答的問題

`transcript.partial` 每次都把整段（最多 15 秒）重新辨識、整段替換，所以直接拿來當字幕會跳。
Confucius4-R2T2 的「不回改」其實是包裝層把已輸出的字鎖住（見 [R2T2 報告](r2t2-eval-report.md)），
不是模型能力；同樣的做法能不能套在現有模型上？用 Whisper-Streaming 的 **LocalAgreement-n**：
連續 n 版 partial 開頭相同的那段視為穩定、正式提交、之後永不更改。

## 設定

- 機器：MacBook Pro、Apple M4 Pro、48 GB、macOS 27.0。模型 `Alkd/TEA-ASR-1.1-MLX-4bit`
  revision `caee57a908b6d64be08a6462c7a21ececbd4d7cb`（生產用）。
- 語料：[adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw](https://huggingface.co/datasets/adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw)
  revision `ff0e8047bdce71c881c6d26eec7b2bbab6381ac1`，`test/test-000000.tar` **全部 1317 句**
  （平均 3.28 秒、共 72.0 分鐘），與 [R2T2 報告](r2t2-eval-report.md)、[P0 品質報告](p0-quality-report.md) 同一份。
- 重播（docs/07 的累積重辨識）：每句從 0 開始每 800 ms 取一次前綴重新辨識（= `PREVIEW_MIN_AUDIO_SAMPLES`），
  最後辨識整句當 final。共 4735 次預覽。文字比較前先照服務預設（`filter_pua=true`）濾掉私用區字元，
  所以量到的就是 client 實際會收到的 partial。
- 時間模型：一次涵蓋 [0, e) 的預覽在 `e + 推論耗時` 發布；final 在 `句長 + 0.9 秒端點靜音 + 推論耗時` 發布。
  不含跨 session 排隊。預覽推論 p95 0.135 秒。
- 「raw」＝純碼位 LocalAgreement；「safe」＝生產規則（`src/tea_asr/stable.py`）：切點必須是 grapheme cluster 邊界、
  不切在英數字詞中間、不以標點或空白結尾。

## 1. 閃爍率：現在的 partial 多常改掉已顯示的字

「改字」＝新版與前一版的共同前綴比前一版短（前一版有字被換掉或刪掉）。

| 1317 句 | 全部字元 | 忽略標點與空白 |
|---|---:|---:|
| partial→partial 轉換中改字的比例（3109 次） | **78.8%** | 44.7% |
| 最後一版 partial→final 改字的比例（1316 次） | 15.2% | 9.7% |
| 至少被改過一次字的句子 | **95.5%** | 71.5% |
| 每句平均被改掉的字數 | **6.92** | 5.04 |
| 每次改字平均改掉幾個字 | 3.44 | 4.37 |

大約一半的改字只是標點（截斷的快照常以 `。` 收尾，後文來了變成 `，`），但即使忽略標點，
每 10 次更新仍有 4～5 次會改掉已經顯示的字。這就是字幕會跳的原因。

## 2–3. 鎖定延遲與分歧率

| 1317 句 | n=2 raw | **n=2 safe** | n=3 raw | n=3 safe |
|---|---:|---:|---:|---:|
| 鎖定延遲 平均／p95（秒） | 0.99／1.64 | **1.03／1.70** | 1.73／2.41 | 1.74／2.42 |
| 　其中 final 前就提交的字 平均／p95 | 0.82／0.83 | **0.82／0.83** | 1.63／1.65 | 1.63／1.65 |
| 　對照：只顯示 final 平均／p95 | 2.30／4.05 | 2.30／4.04 | 2.28／4.04 | 2.28／4.04 |
| final 之前就提交的字比例 | 69.6% | 66.4% | 32.0% | 31.7% |
| **分歧率**（已提交文字與 final 不一致的句子） | 5.01% | **4.02%**（53 句） | 0.91% | 0.91%（12 句） |
| 　其中內容分歧（忽略標點仍不一致） | 3.42% | 3.19% | 0.76% | 0.76% |
| 分歧時錯的字數 平均／最多 | 2.98／8 | 3.25／8 | 3.50／7 | 3.33／6 |
| 已提交字中被 final 否定的比例 | 2.47% | 2.26% | 1.13% | 1.09% |
| 字幕流 CER（以最後的穩定文字計） | 5.18% | **4.96%** | 4.89% | 4.87% |
| 　對 final CER 4.92% 的差值 95% CI（百分點） | [+0.05, +0.45] | **[−0.15, +0.21]** | [−0.14, +0.05] | [−0.16, +0.02] |
| 分歧句中：字幕流 CER／final CER | 16.1%／11.6% | 12.9%／12.0% | 9.9%／13.2% | 8.3%／13.2% |

- 鎖定延遲＝一個字第一次出現在 partial（且與最後提交的前後文相同），到它被提交的時間差；
  在 final 才提交的字以 final 發布時間計。從沒在任何 partial 出現過的字（n=2 safe 540 字）不算，
  因為 partial 本身也沒顯示過它，穩定流沒有讓它更晚。
- CI 為逐句配對 bootstrap（2000 次、seed 0）。CER 依 docs/05 規則（不做繁簡轉換）。
- n=2 的鎖定延遲 ≈ 一個預覽間隔（0.8 秒），是這個方法的下限：第二版 partial 一到就提交。
- safe 規則比 raw 少 13 句分歧（5.01%→4.02%），主要是不再提交結尾標點；字幕 CER 從顯著變差
  （CI 整段 >0）變成與 final 無差異。

分歧的樣子（n=2 safe，`已提交` → final → 字幕流最後的文字）：

```text
區域的和平穩定        → 區域的和平、穩定與發展。    → 區域的和平穩定與發展。      （只差標點）
由本頻道語料          → 有本頻道語料編成的廢話大全。 → 由本頻道語料編成的廢話大全。 （已提交的才是對的）
網監一                → 房間一直有一個聲音。        → 網監一直有一個聲音。        （兩邊都錯，正解「坊間」）
終於今天              → 中意聽聽覺的。              → 終於今天覺的。              （已提交的對，final 錯）
```

## 判定門檻與結果

門檻是看到數據後才寫定的（這點要揭露），但每一條都錨在與 n 無關的外部基準：final 本身的 CER、final 有錯的句子比例、docs/07 的延遲目標。

| # | 門檻 | 理由 | n=2 safe | n=3 safe |
|---|---|---|---|---|
| T1 | 字幕流 CER − final CER 的 95% CI 上界 ≤ +0.5 個百分點 | 字幕上錯字收不回來，所以整體品質不能比「只放 final」差；比 docs/07 對 final 的 1 個百分點更嚴 | **+0.21 → 通過** | +0.02 → 通過 |
| T2 | 分歧率 ≤ 5% 句子，且被否定的已提交字 ≤ 3% | 無法更正的顯示錯誤頻率要遠低於 final 本身有錯的句子比例（24.7%）；最多每 20 行一次 | **4.02%／2.26% → 通過** | 0.91%／1.09% → 通過 |
| T3 | 鎖定延遲 p95 ≤ 2.0 秒，final 前提交的字 p95 ≤ 0.9 秒（一個預覽間隔） | docs/07 的首次可見 p95 目標是 1.5 秒；字幕晚到 3 秒以上就失去即時意義。也必須明顯快於只放 final（p95 4.04 秒） | **1.70／0.83 → 通過** | 2.42／1.65 → **不通過** |

**結論：n=2 safe 三項全過，做為預設；n=3 分歧更少但在這份短句語料上延遲超標，只作為 opt-in 選項。**
n=3 適合寧可晚也不要錯的場合；語料平均只有 3.3 秒、約 4 版 partial，n=3 常常等不到第三版就 final 了，
較長的句子上延遲比例會較低（未證實）。

## 分歧時的規則（依數據）

已上字幕的字收不回來。數據顯示：

- 分歧只有 4.0% 的句子，而且約 2 成只是標點；
- 分歧句裡 final **並不比較可靠**：n=2 時字幕流 12.9% vs final 12.0%，n=3 時字幕流 8.3% vs final 13.2%
  （已提交的反而比較對，與 [P2a 報告](p2a-preview-report.md) 的「final 把人名改壞」一致）。

所以規則是「不收回、補尾巴、明說分歧」：final 延伸了已提交文字 → 穩定流收尾成 final 原文（`state=final`）；
不延伸 → 保留已提交文字，接上 final 在對齊點之後的部分（以最小編輯距離對齊，切點落在 grapheme 邊界），
`state=diverged` 並附 `diverged_chars`。逐字稿、存檔、匯出與翻譯一律以 `transcript.final` 為準。

## 真實服務端到端（同一份語料的前 40 句）

把前 40 句用 1.5 秒靜音串成 194 秒的 WAV，以實際時間送進本分支的服務（另開 port，未動使用者的服務），
continuous＋revisable＋`stable.agreement=2`，`benchmarks/preview_eval.py --stable 2`：

| 項目 | 結果 |
|---|---:|
| 片段 | 39（VAD 切段；每段都有 final） |
| `transcript.stable` 事件 | 118 |
| 只增不改違規（新值不以舊值開頭、或 `state=final` 卻不等於 final） | **0** |
| 分歧 | 0 |
| final 與 final-only 模式是否一致 | **完全一致** |
| 首次可見（partial）p50／p95 | 0.86／0.91 秒 |

這只證明線上路徑與重播一致、沒有違反契約；40 句太少，不拿來估分歧率。

## 3 句串接的壓力測試：模型本身的限制，不作判定依據

為了看 10 秒級的長片段，也把連續 3 句串成一段（中間 300 ms 靜音，423 段、平均 10.3 秒）。
結果 **final CER 高達 52%**：模型在三段不同錄音（多半是不同講者）串起來的音訊上常常只轉錄最後一句
（例：前兩句「遲遲未定的原因」「整理裡面的聲音與字幕」在 final 裡消失）。改成無間隔或加低噪音結果相同
（前 90 句：45.2%／43.6%／44.9%，單句 5.7%），所以這是模型對多講者拼接音訊的行為，不是穩定前綴造成的。
在這份資料上分歧率 90%，但字幕流 CER（48.1%）反而**低於** final（52.2%，差值 CI [−5.1, −3.1] 個百分點）——
已提交的前半句被 final 丟掉了。它不代表真實的同講者長句，所以不列入判定；長句行為列為未證實。

## 重跑

```bash
# 1. 重播辨識（約 23 分鐘；模型與語料都在本機 cache）
TEA_ASR_MODELS_DIR=<models 目錄> uv run python benchmarks/stable_prefix_eval.py collect \
  --out benchmarks/results/stable_replay.json
# 2. 分析（秒級；所有 n／規則組合都由同一份重播算出）
uv run python benchmarks/stable_prefix_eval.py analyze benchmarks/results/stable_replay.json \
  --out benchmarks/results/stable_summary.json
# 3. 真實服務端到端（服務需 revisable 預覽，預設開）
uv run python benchmarks/preview_eval.py --wav <音訊>.wav --stable 2
```

明細 JSON 在本機 ignored 的 `benchmarks/results/`。
