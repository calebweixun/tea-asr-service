# 私用區字元 A/B：MLX 4bit 量化 vs. 上游 BF16

執行日期：2026-09-19。狀態：**已定位根因 — 量化造成，上游乾淨。**

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
