# R2T2（Confucius4-R2T2）接入可行性評估

執行日期：2026-09-24。狀態：**停在關卡 1**。依專案既有 CER 規則（docs/05，繁簡不轉換），
R2T2 明顯劣於現有模型；**差距全部來自輸出混入簡體字**，把字形因素拿掉後兩者準確度相當。
關卡 2（串流速度）沒有執行，也沒有接入任何 production 程式碼。

## 結論

| 問題 | 答案 |
|---|---|
| 能用既有 `qwen3_asr` 路徑載入嗎？ | **能**。`TeaMlxBackend` 原樣 strict load 成功，不需要改 backend |
| 準確度不劣於現有模型嗎？ | **否**（CER 7.98% vs 4.92%，差 +3.06 個百分點，95% CI [+2.28, +3.87]） |
| 為什麼劣？ | 1317 句中有 210 句（15.9%）輸出含簡體字（例：并、没、险、流转）。兩邊都做 OpenCC `s2tw` 後，CER 4.43% vs 4.49%，差值 CI [−0.49, +0.37]，**無顯著差異** |
| 有什麼是 R2T2 比較好的？ | 私用區字元（PUA）leak **0%**，現有模型 72.7%（見 [PUA A/B 報告](pua-bf16-ab-report.md)） |
| 速度／記憶體？ | 離線 RTF p50 0.0344 vs 0.0323（慢約 6%）；MLX 峰值記憶體相同（1.484 GiB） |

## 設定

- 機器：MacBook Pro、Apple M4 Pro（10P+4E）、48 GB、macOS 27.0。兩個模型在同一台機器、
  同一個 `.venv`、依序執行（不併行）。
- Runtime：mlx 0.32.2、mlx-audio 0.4.5、mlx-lm 0.31.3、transformers 5.12.1（專案鎖定版本）。
- 現有模型：`Alkd/TEA-ASR-1.1-MLX-4bit` revision `caee57a908b6d64be08a6462c7a21ececbd4d7cb`（models.lock.json）。
- R2T2：`netease-youdao/Confucius4-R2T2` revision `185ce639118ad1362d049ca0d8ed04b6ec5cd6c9`（BF16 `model.safetensors` 4.08 GB），
  本機轉成 MLX 4bit。

### 量化：與現有模型同一個配方

`benchmarks/convert_quant.py --source r2t2 --recipe audio8` 重現 Alkd 那版的配方：文字解碼器（含 embedding）
4bit／group 64／affine，audio_tower 的 linear 層逐層覆寫為 8bit／group 64。核對結果：

- 輸出 `config.json` 的 147 個逐層覆寫與 Alkd 版**鍵集合完全相同**、值都是 `{"bits": 8, "group_size": 64}`。
- `model.safetensors` 大小 **1,309,677,806 bytes，與 Alkd 版逐 byte 同長**。

因此兩者的差異只來自權重本身，不來自量化設定。

### 語料

[adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw](https://huggingface.co/datasets/adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw)
revision `ff0e8047bdce71c881c6d26eec7b2bbab6381ac1`，`test/test-000000.tar` **全部 1317 句**
（平均 3.3 秒、1.1–8.5 秒、共 72 分鐘、參考文字 10,950 個中文字）。台灣華語朗讀、TRAIL-D 授權，
與 [P0 品質報告](p0-quality-report.md) 同一份（前 200 句的現有模型 CER 4.92%，與 P0 報告一致，證明量測可重現）。

**英文 WER 未量測**：這份語料的英文參考 token 數為 0，repo 內也沒有英文測試音訊；本輪不下載其他資料。

## 結果（1317 句）

| 指標 | TEA-ASR-1.1-MLX-4bit（現有） | R2T2 MLX-4bit（audio8） | R2T2＋固定 system prompt（實驗） |
|---|---:|---:|---:|
| CER（docs/05 規則，不轉繁簡） | **4.92%** | 7.98% | 5.46% |
| 　差值 95% CI（對現有，百分點） | — | [+2.28, +3.87] | [−0.02, +1.13] |
| CER（兩邊都先 `s2tw`） | 4.49% | 4.43% | 4.25% |
| 　差值 95% CI | — | [−0.49, +0.37] | [−0.63, +0.15] |
| 含簡體字的句數 | 5（註） | **210（15.9%）** | 111（8.4%） |
| raw MER（含標點） | 32.29% | 20.94% | 18.54% |
| 私用區字元出現率 | 72.74% | **0%** | 0% |
| RTF p50／p95／mean | 0.0323／0.0449／0.0332 | 0.0344／0.0483／0.0354 | 0.0367／0.0493／0.0371 |
| MLX active／peak 記憶體 | 1.242／1.484 GiB | 1.242／1.484 GiB | 1.240／1.484 GiB |
| 進程 peak RSS（`/usr/bin/time -l`） | 1.61 GB | 1.65 GB | 1.65 GB |

註：「含簡體字」＝假設文字經 `s2tw` 會改變。現有模型的 5 句多半是 `台→檯` 這類異體轉換，可視為這個指標的雜訊底線。

- CI 為逐句配對 bootstrap（5000 次、seed 0）。
- RTF 是單句離線辨識的牆鐘時間／音訊長度，含前處理；MLX 峰值在模型載入後重設，涵蓋全部推論。
- 「固定 system prompt」＝把 `請使用台灣繁體中文輸出。` 當 Qwen3-ASR 的 context 送進去。**只是探路**：生產路徑不送
  context，docs/04 也規定 `context_biasing`／熱詞未驗證前不得宣告。它把簡體句數砍半，但仍有 8.4% 的句子含簡體字，
  CER 仍略高於現有模型（CI 下緣貼著 0），所以不能據此判定通過。

簡體混入的樣子（語料原文 → R2T2 輸出）：

```text
並授權給搜尋引擎使用 → 并授權給搜尋引擎使用。
步伐踉蹌險些跌倒在地 → 步伐踉跄，险些跌倒在地。
都行，沒差         → 都行，没差。
```

同一句裡繁簡混用，而且沒有固定規律，所以不能靠 client 端顯示設定處理。

## 判定：關卡 1 不通過

派工的通過條件是「以既有路徑載入並產出合理文字，且準確度不劣於現有模型」。載入與文字品質都沒問題，
但在專案自己的 CER 規則下 R2T2 **顯著較差**（CI 整段大於 0）。這個規則刻意不做繁簡轉換，
理由是 docs/05 所說的「會把真正的字形差異藏起來」；對一個輸出繁體中文的產品，句子裡冒出簡體字就是錯字，
使用者看得到。所以依約停在這裡，沒有進關卡 2。

要讓 R2T2 過關，得先由使用者決定下面其中一條（都不在本輪權限內）：

1. **接受在 R2T2 的輸出後面加 `s2tw` 轉換**。量測顯示這樣準確度與現有模型相當，且 PUA 問題消失。
   代價：要新增 OpenCC 依賴（`pyproject.toml`，需先核准）；`s2tw` 是逐字轉換，偶有一對多字（例如 `台→檯`）；
   並且要修改 docs/05 的比較規則，或明訂「R2T2 路徑的 raw_text 保留模型原文、text 做轉換」這類契約
   （類似現有的 PUA 過濾：raw_text 保留、text 過濾）。
2. **接受固定 system prompt**。只有部分效果（8.4% 句子仍含簡體），單獨用不夠。

## 關卡 2 沒有執行；讀原始碼確認的事實

為了評估關卡 2 的成本，讀了官方 repo（commit `c4611929bc3592b38dab34e96a8c9940d6da3755`）的
`r2t2/r2t2_asr.py::streaming_transcribe` 與 `ws_server.py::asr_stream_api_v0`。以下是原始碼層級的事實，**不是量測**：

- **沒有 KV cache 跨 chunk 重用**：每個 chunk 把「從 segment 開頭到目前」的全部音訊重新送進 encoder 與 LLM prefill，
  prompt＝chat template＋`language X<asr_text>`＋上一輪文字去掉最後 `unfixed_token_num` 個 token。
  每步只生成很少的 token（`max_new_tokens` 以每 80 ms 一個 token 為預算，中文加倍，160 ms chunk 上限 4）。
  所以每 chunk 的成本會隨 segment 長度線性成長；本服務 continuous segment 上限 14 秒，這是關卡 2 要量的最壞情況。
- **append-only 是包裝層保證的，不是模型性質**：`ws_server.py` 只在 `len(fixed_text) > len(last_fixed)` 時，
  把多出來的尾巴接在已輸出文字後面；模型這一輪的前綴若與已輸出的不同，會被這層直接蓋掉、不回改。
- 官方 finish（片段結束時）只給很小的 token 預算；串流若落後，句尾可能被截斷。

MLX 版的串流迴圈 spike 已寫好但**沒有執行、沒有收進 repo**（依約不進關卡 2），沒有任何延遲數字。

## 授權

R2T2 權重採 NetEase Youdao Model Use License。轉成 MLX 4bit 屬授權 1.5(iii) 明列的衍生作品（quantizing），
轉換後的資料夾 `models/r2t2-mlx-4bit-audio8/` 內含官方 `MODEL_LICENSE` 原文副本（由 `convert_quant.py --license-file`
複製，未修改；sha256 `4d9321cdad58182faa878b015de7d60069881614ddd7571de70f751a9b8e3811`），滿足 3.4(b)
「每份使用中的衍生作品都保留授權副本」。本輪沒有散佈；若日後要散佈，還要附上 4.1(a) 規定的免責聲明。
權重與轉換結果都在 gitignored 的 `models/`，不進 Git。

接入前使用者要知道的其他條款（摘要，不是法律意見）：2.2 月活超過 1 億或年營收超過人民幣 10 億須另取授權；
3.4(c) 不得用 R2T2 或其衍生作品改進其他 AI 模型（R2T2 本身、其衍生作品與非商用模型除外）；
4.2 禁止用於醫療診斷、自動駕駛等高風險場景。這與現有模型的 MIT 授權不同。

## 重跑

```bash
# 1. 下載（需使用者同意；約 4.1 GB）
uv run python -c "from huggingface_hub import snapshot_download; \
  snapshot_download('netease-youdao/Confucius4-R2T2', \
  revision='185ce639118ad1362d049ca0d8ed04b6ec5cd6c9', cache_dir='models')"
# 2. 轉換（MODEL_LICENSE 取自官方 GitHub repo）
uv run python benchmarks/convert_quant.py --source r2t2 --recipe audio8 --bits 4 \
  --out models/r2t2-mlx-4bit-audio8 --license-file MODEL_LICENSE
# 3. 離線辨識（兩個模型、同一份語料）
uv run python benchmarks/quality_eval.py --limit 1317 \
  --model-path <Alkd snapshot> --model-label Alkd/TEA-ASR-1.1-MLX-4bit \
  --model-revision caee57a908b6d64be08a6462c7a21ececbd4d7cb \
  --out benchmarks/results/r2t2_gate1_tea_full.json
uv run python benchmarks/quality_eval.py --limit 1317 \
  --model-path models/r2t2-mlx-4bit-audio8 \
  --model-label netease-youdao/Confucius4-R2T2@mlx-4bit-audio8 \
  --model-revision 185ce639118ad1362d049ca0d8ed04b6ec5cd6c9+audio8 \
  --out benchmarks/results/r2t2_gate1_r2t2_full.json
#    實驗組再加：--system-prompt "請使用台灣繁體中文輸出。" --out .../r2t2_gate1_r2t2_ctx_full.json
# 4. 繁簡拆解與信賴區間（任何裝了 opencc-python-reimplemented 0.1.7 的 Python；不是專案依賴）
python3.11 benchmarks/r2t2_script_cer.py benchmarks/results/r2t2_gate1_tea_full.json \
  benchmarks/results/r2t2_gate1_r2t2_full.json benchmarks/results/r2t2_gate1_r2t2_ctx_full.json
```

逐句明細 JSON 在本機 ignored 的 `benchmarks/results/`。
