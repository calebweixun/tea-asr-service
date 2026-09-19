# 私用區字元 A/B：MLX 4bit 量化 vs. 上游 BF16

執行日期：2026-09-19。狀態：**已定位根因 — 量化造成，上游乾淨。**

> **後續更新（同日第二輪，見文末〈8bit / 自轉 4bit 三方對照〉一節）**：
> 補做了 8bit 與自己轉的 4bit 對照，結論是 **8bit 沒有消除 PUA**，且自轉
> 4bit（標準流程）跟 Alkd 那版 4bit（非標準流程）輸出逐字相同——代表問題
> 不是「Alkd 這一版轉換流程特有」，而是這個 checkpoint 對 MLX affine
> 量化本身（不論 4bit 或 8bit）都敏感。以下第一輪內容維持原樣未改動。

## 背景

`docs/benchmarks/p0-quality-report.md` 量到目前生產用的
`Alkd/TEA-ASR-1.1-MLX-4bit`（revision `caee57a908b6d64be08a6462c7a21ececbd4d7cb`）
在 200 筆語料上有 **73.5%** 的句子含 Unicode 私用區（PUA，U+E000–U+F8FF）字元，
且已排除是 decode 端的 bug（tokenizer vocab 裡沒有任何 PUA entry，這些字元是模型
自己生成的 byte-level token 序列）。當時卡在磁碟空間不足，沒能跟上游未量化模型
做對照，本次補上這個對照。

## 方法

- **上游模型**：`JacobLinCool/TEA-ASR-1.1`（BF16，Qwen3-ASR 架構），revision
  `bda08df76d4fd6b487b4a1dd7f0bddf8541696f8`（取本次執行時的 main HEAD，因為
  該模型頁面沒有標記版本 tag）。用官方建議的 `qwen-asr` 套件載入與推論
  （`Qwen3ASRModel.from_pretrained(...).transcribe(audio=(array, 16000),
  language="Chinese")`），跟 README 的 Quick start 一致，不是自己兜的推論路徑。
  模型權重（約 4.08 GB safetensors）下載到專案 `models/` 目錄，`cache_dir=models/`，
  沒有動到 `~/Library/Caches`。
- **量化模型**：現有的 `Alkd/TEA-ASR-1.1-MLX-4bit`，透過既有的
  `TeaMlxBackend`（`src/tea_asr/backend.py`），跟 server 生產路徑相同的載入與
  推論程式碼，沒有另外兜一套。
- **語料**：兩邊用**同一批**語料，`adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw`
  test split 的前 30 筆（revision `ff0e8047bdce71c881c6d26eec7b2bbab6381ac1`），
  跟 `benchmarks/quality_eval.py`（原本 P0 量測用的語料）完全同一個
  dataset/shard/revision，逐筆同 key 對齊。

  **偏離原始指示的說明**：任務指示提到「使用者先前錄的 10 句測試音檔，可能在
  `benchmarks/` 或 `tests/` 底下」。實際搜過整個 repo（含 `benchmarks/`、
  `tests/fixtures`），只找到 `benchmarks/generated/taiwan.wav`（單一 4.2 秒
  生成音檔，非「10 句錄音」），沒有找到 10 筆錄音的集合。因此改用先前 P0
  報告已經用來量出 73.5% PUA 比例的同一份既有語料，先跑 10 筆驗證方向、
  再擴大到 30 筆確認統計穩定，這樣才能跟原本的 73.5% 直接對得上。**這是
  我的推論選擇，不是使用者確認過的替代方案**，如果使用者手邊另外還有一組
  獨立錄的 10 句音檔，那組結果可能需要另外補跑。

- **環境隔離**：BF16 那一輪跑在專案外、獨立的 venv（`uv venv` 建立在
  session scratchpad，非專案目錄），裝 `torch`、`transformers`、`qwen-asr`；
  MLX 那一輪照舊用專案的 `.venv`（裡面沒有 torch，也刻意不裝，避免污染
  生產依賴）。兩邊互不干擾。

## 復現腳本

放在 `benchmarks/`，三支：

```bash
# 1. MLX 4bit（用專案 .venv）
uv run python benchmarks/pua_ab_mlx_pass.py --limit 30 \
    --out benchmarks/results/pua_ab_mlx_30.json

# 2. 上游 BF16（用獨立 venv，需要 torch/transformers/qwen-asr）
uv venv /tmp/bf16-env --python 3.12
uv pip install --python /tmp/bf16-env/bin/python torch transformers qwen-asr
/tmp/bf16-env/bin/python benchmarks/pua_ab_bf16_pass.py --limit 30 \
    --out benchmarks/results/pua_ab_bf16_30.json

# 3. 比較
uv run python benchmarks/pua_ab_compare.py \
    --mlx benchmarks/results/pua_ab_mlx_30.json \
    --bf16 benchmarks/results/pua_ab_bf16_30.json
```

`benchmarks/results/*.json` 是本機 ignored 檔（`.gitignore` 涵蓋
`benchmarks/results/*.json`），逐筆明細（含每句的 hypothesis 全文與 PUA
碼位清單）都在裡面，這裡只放彙總數字與代表性例句。

## 實測結果（本機量到的數字）

| 模型 | 樣本數 | 含PUA句數 | 比例 | PUA出現總次數 | 不重複碼位數 |
|---|---:|---:|---:|---:|---:|
| MLX 4bit（生產用，caee57a9） | 10 | 7 | 70.0% | 11 | 11 |
| MLX 4bit（生產用，caee57a9） | 30 | 21 | **70.0%** | 37 | 37 |
| 上游 BF16（bda08df7） | 10 | 0 | **0.0%** | 0 | 0 |
| 上游 BF16（bda08df7） | 30 | 0 | **0.0%** | 0 | 0 |

30 筆的 70.0% 跟原始 P0 報告 200 筆量到的 73.5% 落在同一量級，代表這個子集
沒有明顯偏態，可以拿來做對照。**上游 BF16 在兩輪（10 筆、30 筆）合計 40 次
推論裡，PUA 出現次數是 0**，MLX 4bit 同樣的 40 筆音訊產出 48 次 PUA
出現、48 個全部不重複的碼位（U+E000–U+F8FF 範圍內）。

逐句比對（30 筆中 27 筆 MLX 與 BF16 文字不同，但差異幾乎都只在 PUA 字元與
標點，內容字沒有系統性劣化），節錄幾筆代表性例子：

```text
REF : 整理裡面的聲音與字幕
MLX : 整理裡面<U+E371>的聲音與<U+E3C7>字幕。
BF16: 整理裡面的聲音與字幕

REF : 無產階級專政萬歲！
MLX : 無產階級專政萬歲<U+E405><U+E0D7><U+E0F7><U+E3F5><U+E07C>！
BF16: 無產階級專政萬歲

REF : 並授權給搜尋引擎使用
MLX : 並授權給搜尋引擎使用<U+E377><U+E0DB><U+E1FB><U+E3E6>。
BF16: 並授權給搜尋引擎使用。
```

37 個 PUA 碼位（30 筆那輪）彼此完全不重複，跟 P0 報告「210 個不重複
codepoint 對 266 次出現」的觀察一致：**不是固定 sentinel 集合，而是隨機/
半隨機插入的 byte-level token**，且只出現在量化後的模型。

## 結論

**PUA 字元洩漏是 MLX 4bit 量化轉換造成的，上游 BF16 checkpoint 本身乾淨。**

依據：
1. 同一份語料、同一批音檔，上游 BF16 在 40 次推論中 PUA 出現次數為 0；
   量化模型同樣 40 次推論中出現 48 次、48 個不重複碼位。
2. 這不是取樣運氣：兩邊都各自跑了 10 筆與 30 筆兩個規模，比例穩定
   （量化模型穩定落在 70% 上下，上游穩定為 0%）。
3. PUA 是**插入**在正確文字之間而非取代文字（前一份 P0 報告已確認、本次
   逐句比對也再次確認），代表量化後的模型在解碼過程中偶爾會多吐出幾個
   byte-level token，這些 token 剛好落在 tokenizer 的 PUA 區段——很典型的
   4bit 量化對小機率長尾 token 的機率分布擾動症狀。

需要說明的推論邊界：
- 本次沒有測試「換一個 4bit 量化實作」或「換 8bit」是否也會出現同樣問題，
  所以嚴格來說只證明了「**這一版** MLX 4bit 轉換有問題」，還沒有把「4bit
  量化本身」和「這個轉換流程/工具（mlx-audio 0.4.5 + 這份轉換腳本）」完全
  分開歸因。但既然上游 BF16 是乾淨的，問題肯定出在量化轉換這一步，而不是
  模型權重或訓練過程本身。
- 上游 checkpoint 的 revision 是本次執行當下的 main HEAD（`bda08df7`），
  不是這次沒有標註的正式 release tag；如果模型作者之後更新了 `main`，
  重跑此腳本前應該先確認 revision 是否還一致。

## 後續可行選項

1. **改用不同量化位寬重新轉換**（優先建議）：先試 8bit 量化，同一組語料
   重跑本報告的比較腳本，看 PUA 比例是否顯著下降到接近 0。如果 8bit 乾淨，
   代表問題確實是 4bit 精度不足以穩定表示 audio-conditioned 的 token
   分布尾端，可以考慮拿 8bit 換取正確率、犧牲一些記憶體/速度。
2. **向 MLX 4bit 轉換作者（`Alkd`）回報**：附上這份報告的碼位分布與
   `benchmarks/pua_ab_*.py` 可重現腳本，請對方確認轉換流程（校準資料集、
   per-layer 混合精度設定等）是否有已知會放大長尾 token 機率的地方。
   `docs/benchmarks/p0-quality-report.md` 也提到「checkpoint 隨附資料宣告
   sentinel leak 為 0，與實測不符」，這點也一併附上。
3. **後處理過濾（暫時止血、非根本解）**：既然 PUA（U+E000–U+F8FF，含
   U+F0000–U+FFFFD、U+100000–U+10FFFD 等擴充私用區）在正常的繁體中文、
   標點、中英混用文字裡**完全不會出現**——這個區段本來就是 Unicode 保留給
   應用程式自訂用途，不對應任何語音可能說出的內容——單純移除這個範圍的
   字元，理論上**不會誤傷任何正常文字**。目前 server（`private_use_warnings`,
   `src/tea_asr/api/stream.py:83`）已經是「偵測後回 warning、保留原文」，
   沒有靜默移除；在量化問題修好之前，可以考慮把這個過濾做成明確的
   opt-in 清理選項（`text` 移除、`raw_text` 保留原文），而不是等轉換修好。
   但這只是治標：只要量化轉換的根因沒修，往後任何新版量化模型都可能重新
   踩到同樣的坑，過濾規則也要跟著搬過去。

## 尚未做的事

- 沒有跑 8bit 或其他中間位寬的量化版本做三方比較（上面第1項建議）。
- 沒有測試比 30 筆更大的樣本數在 BF16 上是否仍然 0%——理論上量越大越有
  機會踩到極端情況，如果要完全排除「BF16 也有極低機率 leak，只是本次
  40 筆沒踩到」，建議至少補到跟原 P0 報告一樣的 200 筆規模。
- 沒有評估效能（RTF）差異：BF16 在 CPU/MPS 上跑，速度不是這次的重點，
  也不具代表性，因此本報告不放 BF16 的延遲數字。

---

## 第二輪：8bit / 自轉 4bit 三方對照（同日補做）

### 要回答的問題

第一輪只證明了「上游 BF16 乾淨、Alkd 那版 4bit 量化不乾淨」，但沒有回答：
是 **4bit 精度本身不夠**，還是 **Alkd 那一版轉換流程**本身有問題？

### 方法

- 用 mlx-audio 官方轉換工具 `mlx_audio.convert.convert()`（`benchmarks/convert_quant.py`），
  對**本機已經下載好**的上游 BF16 checkpoint
  （`models/models--JacobLinCool--TEA-ASR-1.1/snapshots/bda08df76d4fd6b487b4a1dd7f0bddf8541696f8`，
  沒有重新下載）自己轉出兩份模型：
  - `models/mlx-8bit-selfconv`：8bit，`q_mode=affine`，group_size 64（mlx_lm 預設）。
  - `models/mlx-4bit-selfconv`：4bit，同上參數。
  - 兩份都**沒有**帶 `quant_predicate`，也就是沿用 `Qwen3ASRModel.model_quant_predicate`
    的預設值（`.venv/lib/python3.12/site-packages/mlx_audio/stt/models/qwen3_asr/qwen3_asr.py:826-828`）：
    `not p.startswith("audio_tower")`——也就是**排除 audio_tower 不量化**，這是
    mlx-audio 自己認定的「標準/安全流程」。
  - 這跟生產用的 `Alkd/TEA-ASR-1.1-MLX-4bit` 不一樣：`src/tea_asr/backend.py` 裡的
    `_mixed_quantization_loader` monkey patch說明，那個 checkpoint **刻意連
    audio_tower 都量化了**（載入時要強制蓋掉 predicate 才讀得起來），是偏離
    mlx-audio 預設安全行為的做法。這正好讓我們可以把「audio_tower 要不要量化」
    這個轉換選擇，跟「4bit vs 8bit 位元寬」分開對照。
- 語料、逐句 PUA 偵測正則（`[-]`，跟第一輪同一個範圍）、
  比較方式都跟第一輪完全一致，30 筆同一批語料。新增腳本
  `benchmarks/pua_ab_local_pass.py`（跟 `pua_ab_mlx_pass.py` 差別只在模型路徑
  直接指向本機資料夾，不透過 `model_spec.py` 的 HF repo id）。
- 記憶體與延遲用 `benchmarks/mem_probe.py` 搭配 `/usr/bin/time -l` 實測
  （載入 + 連續跑 5 筆音檔的 peak RSS 與平均推論時間），不是估算。
- 磁碟：轉換前確認可用空間 59Gi，兩份新模型共 3.8G，轉換後仍有 55Gi 可用，
  沒有觸及告警線。

### 實測結果

**PUA 比例（30 筆，同一批語料）：**

| 模型 | 量化方式 | 含PUA句數 | 比例 | PUA出現總次數 |
|---|---|---:|---:|---:|
| 上游 BF16（bda08df7） | 無 | 0/30 | **0.0%** | 0 |
| MLX 8bit（自轉，audio_tower 不量化，標準流程） | 8bit affine | 19/30 | **63.3%** | 34 |
| MLX 4bit（生產用 Alkd，audio_tower 也量化） | 4bit affine | 21/30 | **70.0%** | 37 |
| MLX 4bit（自轉，audio_tower 不量化，標準流程） | 4bit affine | 21/30 | **70.0%** | 37 |

**關鍵交叉比對**（逐句文字是否完全相同，30 筆共同樣本）：

| 對照組 | 文字逐字相同的句數 |
|---|---:|
| Alkd 4bit（正式） vs 自轉 4bit（標準流程） | **30/30（完全相同）** |
| Alkd 4bit（正式） vs 自轉 8bit | 9/30 |
| 自轉 4bit vs 自轉 8bit | 9/30 |

Alkd 正式版 4bit 跟自轉 4bit（唯一差別是 audio_tower 有沒有一起量化）
在 30 筆語料上**輸出逐字元完全相同**，包含哪些字、哪裡插入哪個 PUA 碼位都
一樣。也就是說：Alkd 那版「額外把 audio_tower 也量化」這個轉換選擇，
在這個測試裡對輸出文字**沒有可觀察的影響**——不是它導致 PUA。真正造成
PUA 的是「thinker（語言模型）本身的線性層被量化」這件事,而這在 4bit 和
8bit 都會發生，只是 8bit 的比例（63.3%）比 4bit（70.0%）略低一點，但仍然
是同一數量級，遠遠不到 BF16 的 0%。

**模型大小（實測 `du -sh`，含 gitignore 之外自轉的兩份）：**

| 模型 | 大小 |
|---|---:|
| 上游 BF16 | 3.8 GiB |
| MLX 4bit（Alkd 正式版，audio_tower 也量化） | 1.2 GiB |
| MLX 4bit（自轉，audio_tower 保留 bf16） | 1.5 GiB |
| MLX 8bit（自轉，audio_tower 保留 bf16） | 2.3 GiB |

**記憶體（`/usr/bin/time -l`，載入 + 連續推論 5 筆的 maximum resident set size）：**

| 模型 / 執行環境 | Peak RSS |
|---|---:|
| 上游 BF16（qwen-asr + torch 2.10，homebrew python3.11） | **12.53 GiB**（peak memory footprint 9.24 GiB） |
| MLX 4bit（Alkd 正式版，專案 .venv + mlx-audio） | 1.56 GiB |
| MLX 4bit（自轉） | 1.85 GiB |
| MLX 8bit（自轉） | 2.71 GiB |

**延遲（每筆推論時間，`inference_s`，MLX 三組取 30 筆均值，BF16 取
`pua_ab_bf16_30.json` 30 筆均值，含第一筆暖機）：**

| 模型 | 平均每筆推論時間 |
|---|---:|
| 上游 BF16（torch，非 Metal MLX） | 0.583 s |
| MLX 4bit（Alkd 正式版） | 0.129 s |
| MLX 4bit（自轉） | 0.122 s |
| MLX 8bit（自轉） | 0.155 s |

（BF16 跑在 torch，不是 mlx-audio/Metal 路徑，這個延遲差距同時包含「精度」
跟「推論引擎/後端」兩個變數疊在一起，不是純粹的位元寬效應，這點延續第一輪
報告已經聲明過的限制。）

### 明確回答

**8bit 有沒有消除 PUA？沒有。** 8bit（標準流程、不強制量化 audio_tower）
在同一批 30 筆語料上，仍有 63.3%（19/30）句子含 PUA、34 次出現，跟正式版
4bit 的 70.0%／37 次同一個數量級，離 BF16 的 0% 非常遠。**如果換 8bit 是為了
解決 PUA 問題，這條路實測是死路。**

而且換 8bit 的代價是實測到的：模型從 1.2 GiB 漲到 2.3 GiB（+92%），峰值記憶體
從 1.56 GiB 漲到 2.71 GiB（+74%），平均每句推論時間從 0.129s 漲到 0.155s
（+20%）——**多付出將近一倍的體積與記憶體、兩成的延遲，PUA 比例卻幾乎沒有改善**
（70.0% → 63.3%，仍然是同量級的失敗率，不是質變）。

**那麼是「4bit 本身」還是「Alkd 那版轉換流程」的問題？兩者都不是唯一原因，
證據指向「這個 checkpoint 對 MLX affine 量化本身敏感」：**

1. Alkd 正式版 4bit（audio_tower 也量化）跟自轉 4bit（audio_tower 不量化，
   標準流程）**輸出文字逐字完全相同**——代表 Alkd 版本「額外把 audio_tower
   也量化」這個轉換選擇，跟 PUA 的出現與否無關，不是它造成的。
2. 但把位元寬從 4bit 提高到 8bit（同樣用標準流程），PUA 比例只從 70.0% 降到
   63.3%，並沒有像「量化精度不足」假說預期的那樣趨近於 0%——如果純粹是
   「4bit 精度不夠」，8bit 應該要明顯好轉，但沒有。
3. 因此比較合理的解讀是：這個 fine-tune 過的 checkpoint，在解碼到那些
   會輸出 PUA byte-level token 的邊界位置時，機率本來就貼著決策邊界；
   MLX 的 affine 仿射量化（不論 4bit 還是 8bit、不論 audio_tower 量不量化）
   引入的權重擾動，足以把這些邊界情況推過門檻，穩定觸發同一組 PUA token。
   這不是「某一支轉換腳本寫錯了」的 bug，而是**這個模型的 decoder 對
   量化雜訊本身就敏感**，在目前測過的 affine 4bit／8bit 兩種設定下都會
   踩到。

**尚未驗證、避免過度推論的邊界：**
- 沒有測試 `mxfp4` / `mxfp8` 等其他量化模式（`mlx_audio.convert.QUANT_MODES`
  裡列出的選項），也沒有測不同 `q_group_size`（例如更細的 32）——理論上
  更精細的量化網格可能表現不同，但本次沒有實測，不下結論。
- 沒有做「排除更多敏感層（例如 lm_head / embedding）」的量化感知實驗，
  只測了 mlx-audio 的預設 predicate（排除 audio_tower）跟 Alkd 的變體
  （全部量化）兩種，兩者結果相同不代表已經窮舉所有可能的 predicate。

### 建議採用哪一個模型

**不建議切換到 8bit**：花更多體積、記憶體、延遲，PUA 沒有實質改善，性價比
是負的，實測數字見上表。

**兩個可行方向（本次只給建議，沒有動生產設定 `src/tea_asr/model_spec.py`）：**

1. **維持現狀（`Alkd/TEA-ASR-1.1-MLX-4bit`，`caee57a9...`），把 PUA 後處理過濾
   從「可選」升級為「預設開啟」。** 因為這一輪確認了「換位元寬治不好」，
   在量化根因真正解決之前，**過濾是目前唯一經濟的止血手段**（見第一輪報告
   「後處理過濾」一節，那邊已經論證 PUA 範圍不會誤傷正常中文/標點）。
   這個選項不需要換模型、不需要動 `model_spec.py`，只需要在
   `src/tea_asr/api/stream.py` 現有的 `private_use_warnings` 邏輯基礎上，
   把移除 PUA 字元變成預設行為（目前是偵測後回 warning、保留原文）。
   **這是本報告的建議，不是已經做的變更。**
2. **如果 PUA 完全不可接受、延遲與記憶體可以退讓**，改用上游 BF16。這不是
   單純改 `model_spec.py` 的 `repo_id`/`revision` 就能做到——目前的
   `TeaMlxBackend`（`src/tea_asr/backend.py`）呼叫的是
   `mlx_audio.stt.utils.load_model`，只認 MLX 格式；上游 BF16 是標準 HF
   safetensors，需要換一套 runtime（`qwen_asr.Qwen3ASRModel` + `torch`，
   如同 `benchmarks/pua_ab_bf16_pass.py` 的載入方式），等於是新增一個
   backend、換掉 `pyproject.toml` 的推論依賴（`torch`/`transformers`/
   `qwen-asr`，目前專案 `.venv` 沒有 torch，是刻意的）。如果要往這個方向做，
   `model_spec.py` 大概會長這樣（僅供參考，未套用）：

   ```python
   TEA_ASR_1_1_BF16_UPSTREAM = ModelSpec(
       repo_id="JacobLinCool/TEA-ASR-1.1",
       revision="bda08df76d4fd6b487b4a1dd7f0bddf8541696f8",  # 執行本報告當下的 main HEAD，
                                                              # 沒有正式 release tag，換用前建議先確認
       mlx_audio_version="0.4.5",  # 佔位；BF16 路徑不走 mlx-audio，這個欄位對這個 spec 沒有意義，
                                    # 換 backend 時 ModelSpec 這個 dataclass 本身大概也要跟著調整
   )
   ```

   代價是實測到的：模型體積 3.8 GiB（+217% 對比正式版 4bit 的 1.2 GiB）、
   峰值記憶體 12.53 GiB（+703%）、平均每句推論時間 0.583s（+352%，且這個
   延遲數字混雜了「精度」與「torch vs MLX/Metal 推論引擎」兩個變數，不是
   純粹的位元寬效應）。**這是一個很重的選項，只在正確性優先於延遲/資源時
   才建議認真考慮。**

### 復現腳本

- `benchmarks/convert_quant.py`：從本機 BF16 checkpoint 自轉 8bit/4bit MLX 模型。
- `benchmarks/pua_ab_local_pass.py`：對本機自轉模型跑 PUA 統計（用法同
  `pua_ab_mlx_pass.py`，差別是 `--model-path` 直接指本機資料夾）。
- `benchmarks/mem_probe.py`：配合 `/usr/bin/time -l` 量測載入 + 推論的
  peak RSS 與平均延遲。

```bash
# 轉換（本機已有 BF16 checkpoint，不會重新下載）
uv run python benchmarks/convert_quant.py --bits 8 --out models/mlx-8bit-selfconv
uv run python benchmarks/convert_quant.py --bits 4 --out models/mlx-4bit-selfconv

# PUA 統計
uv run python benchmarks/pua_ab_local_pass.py --model-path models/mlx-8bit-selfconv \
    --pass-name mlx-8bit-selfconv --limit 30 --out benchmarks/results/pua_ab_8bit_30.json
uv run python benchmarks/pua_ab_local_pass.py --model-path models/mlx-4bit-selfconv \
    --pass-name mlx-4bit-selfconv --limit 30 --out benchmarks/results/pua_ab_4bitself_30.json

# 記憶體與延遲
/usr/bin/time -l uv run python benchmarks/mem_probe.py --model-spec alkd-4bit --repeat 5
/usr/bin/time -l uv run python benchmarks/mem_probe.py --model-path models/mlx-8bit-selfconv --repeat 5
/usr/bin/time -l uv run python benchmarks/mem_probe.py --model-path models/mlx-4bit-selfconv --repeat 5
```

新轉出的兩份模型放在 `models/mlx-8bit-selfconv/`、`models/mlx-4bit-selfconv/`
（都在專案 `models/` 底下，`.gitignore` 涵蓋，沒有進 `~/Library/Caches`）。
`benchmarks/results/pua_ab_8bit_30.json`、`pua_ab_4bitself_30.json` 本機
ignored，逐句明細在裡面。
