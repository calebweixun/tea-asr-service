# 標點補回評估（2026-10-05）

**結論：機制與資產完成，預設關閉（`punctuation_restore_enabled=false`，`TEA_ASR_PUNCTUATION=1` 開啟）。** 標點密度達標（8-bit 每 51.8 字一個標點 → 11.9 字，人工訂正答案是 12.5），延遲與 layout moves／duplicates 沒有退步；但 OBS replay 的「open-line row-limit 縮短」從 7 升到 43、final 改寫已顯示文字的次數 24→34（使用者的 `comma-min 8` 設定下），這是視覺行為，agent 無法驗收，所以不預設打開，等使用者在真實 OBS 上看過（建議同時把 comma-min 提高到 14 以上）再改預設。

## 資產

| 項目 | 值 |
|---|---|
| 模型 | FunASR CT-Transformer `iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch`，sherpa-onnx 轉 ONNX 並 int8 量化 |
| URL | https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2 |
| 版本 | release tag `punctuation-models`（檔名含 2024-04-12）；沒有 git revision，以 sha256 固定 |
| 大小 | 壓縮檔 64,717,756 B；`model.int8.onnx` 75,519,198 B（fp32 版 279 MB，未下載） |
| sha256 | 壓縮檔 `c0d5aa5f…28a6e1`（與 release 的 checksum.txt 相同）；`model.int8.onnx` `65a3fb9f…ed524b1` |
| 授權 | 上游 ModelScope 模型卡標示 Apache License 2.0；FunASR 程式碼 MIT。見 NOTICE |
| 參考實作 | k2-fsa/sherpa-onnx `offline-punctuation-ct-transformer-impl.h`、`offline-ct-transformer-model.cc`、`text-utils.cc`（SplitUtf8／MergeCharactersIntoWords）與模型包內 `test.py`，2026-10-05 讀取 |

輸入 `inputs` int32 [1,T]、`text_lengths` int32 [1]，輸出 `logits` [1,T,6]，類別 `<unk>|_|，|。|？|、`（模型不會產生「！」）。詞表與標點表在 ONNX metadata，一個檔案就是全部資產。

## 離線精準率／召回（`benchmarks/punctuation_eval.py`）

資料：`.soak/gold/answers/speakers-answers.json` 的 39 句（非 music）人工訂正答案，假設為 server 離線解碼。以非標點字元對齊後比較斷點位置（`，。？！、` 皆算斷點，句尾標點不計）。`min_gap=3`、`terminal=True`。

| 假設 | 條件 | 標點數 | 字／標點 | 精準率 | 召回 | F1 | 新插入標點的精準率 |
|---|---|---|---|---|---|---|---|
| 4-bit segment | 原樣 | 50 | 29.2 | 1.000 | 0.641 | 0.781 | |
| 4-bit segment | +補回 | 102 | 14.3 | 0.637 | 0.833 | 0.722 | 0.283（53 個） |
| 8-bit segment | 原樣 | 20 | 73.1 | 0.850 | 0.202 | 0.327 | |
| 8-bit segment | +補回 | 102 | 14.3 | 0.569 | 0.690 | 0.624 | 0.477（86 個） |

- 8-bit（本次串流用的模型）原樣幾乎不出標點，補回後召回 0.20→0.69、F1 0.33→0.62。4-bit 本來就有標點，補回把召回提高但 F1 下降（精準率 1.0→0.64）；逗號位置有主觀性，精準率是對單一份人工答案的嚴格比對（±1 字寬容後幾乎相同，所以不是差一格，而是多出來的逗號）。
- 隨機放同樣數量標點的期望精準率約為答案密度 8%，新插入的 28–48% 遠高於此。
- CER（cer_eval 正規化，忽略標點）補回前後完全相同：4-bit 0.12175／0.12175，8-bit 0.10876／0.10876；移除新插入標點後文字與原文逐字相同（`insert_only_text_identical=true`）。
- `min_gap`（新標點與其他標點至少相隔幾個 token）0／2／3／4 的 8-bit F1 為 0.607／0.614／0.624／0.616，取 3。
- 模型每次呼叫 p50 約 3–5 ms（1 thread，離線）。

## 尾端 margin 與 stable 的取捨（`benchmarks/punctuation_stable_sim.py`）

用舊 trace（church-600，288 段，4-bit）重播 partial 進 `StablePrefixTracker`，`pre_final` 是 final 前已提交的字數比例，`diverged` 是 final 沒有延伸已提交文字的段數：

| 設定 | pre_final | diverged | blocked partial |
|---|---|---|---|
| 無標點 | 0.648 | 107 | 1081 |
| 補回，margin 0 | 0.575 | 111 | 1292 |
| 補回，margin 4（只限制標點） | 0.550 | 122 | 1360 |
| 補回，margin 2＋stable 保留尾端 2 字（採用） | 0.579 | 99 | 1014 |
| 補回，margin 4＋stable 保留尾端 4 字 | 0.535 | 88 | 856 |

只加 margin 反而更糟：標點被延後插入，已提交的前綴之後被插入標點打破。所以 `stable` 只看「去掉尾端 N 字」的 partial（`hold_back_tail`），這樣已提交文字內的標點都已用 ≥N 字右側上下文決定過；partial 事件本身仍帶完整文字。N=2 在 church-300 trace 也是 diverged 比無標點少（102 vs 115）。

## 真實串流前後（8-bit、port 8461、隔離 HOME、`church-30m-59m.wav` 前 10 分鐘、end_silence 870、62 段，無 context／carry）

| 指標 | 關閉 | 開啟 margin 2（採用） | 開啟 margin 0 |
|---|---|---|---|
| 字／標點（含句尾） | 46.1 | 9.3 | 9.3 |
| 字／標點（不含句尾） | 51.8 | 11.9 | 11.9 |
| 沒有句中標點的 final | 69.4% | 8.1% | 8.1% |
| 最長無標點連續字數 中位／p90 | 34.5／55 | 15.5／21 | 15.5／21 |
| stable 提交間隔 中位／p95 | 0.63／5.36 s | 0.64／5.13 s | 0.89／8.30 s* |
| final 前已提交字數比例 | 0.569 | 0.547 | 0.474 |
| stable diverged | 32 | 28 | 27 |
| 首個 partial p95 | 1.20 s | 1.21 s | 2.83 s* |
| final 延遲 p95 | 1.03 s | 1.03 s | 2.46 s* |
| partial 改寫率 | 40.6% | 45.1% | 44.2% |
| OBS replay layout moves／duplicates | 0／0 | 0／0 | 0／0 |
| replay row-limit 縮短（comma-min 8／14／20） | 7／2／1 | 43／16／6 | 42 |
| replay final 改寫已顯示文字 | 24 | 34 | 34 |
| replay tail rewrite | 199 | 241 | 140 |

\* margin 0 那次的延遲明顯變差，但跑的時候機器上還有我的重播／統計程序與使用者的 8327 server，三次不是同一負載，不能歸因於 margin；margin 0 的 final 前提交比例（0.474）也較低，與離線模擬方向一致，所以採用 margin 2。
- replay 旗標：`--fade-delay-ms 3000 --fade-ms 200 --max-rows 3 --max-lines 3 --width 1800 --punct comma --comma-min 8 --quiet`。
- 標點成本（離線重播這 10 分鐘真實 partial／final 文字，1 thread）：partial 837 次 p50 1.4 ms／p95 5.8 ms／max 9.5 ms；final 59 次 p50 4.8 ms／p95 7.5 ms／max 9.3 ms。經 executor，不在事件迴圈執行。（server 內即時統計沒有另外量。）

## 未驗證

- OBS 畫面：row-limit 縮短與 final 改寫增加的實際觀感，以及 `comma-min` 該設多少。
- 其他講者、場地、4-bit 串流、有 context／carry 的串流；只有一個 10 分鐘窗口，每種設定跑一次，沒有重複與信賴區間。
- 標點正確性只以一位使用者的 39 句訂正答案評估；逗號位置沒有唯一解。
- 三次串流不是同一負載（見上）。
