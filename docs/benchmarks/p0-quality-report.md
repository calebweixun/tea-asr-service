# P0 品質量測報告

執行日期：2026-09-19。狀態：**辨識品質達標；私用區字元leak確認在真實語音重現，仍是封鎖問題。**

## 語料

[adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw](https://huggingface.co/datasets/adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw)
的 test split，revision `ff0e8047bdce71c881c6d26eec7b2bbab6381ac1`，Common Voice zh-TW 衍生，
授權 TRAIL-D（明文允許自由使用、再散布與修改，衍生需附授權與出處）。

本次取前200筆，音訊只留在 Hugging Face cache，不進Git。重跑：

```bash
uv run python benchmarks/quality_eval.py --limit 200
```

這是台灣華語朗讀語料，**不涵蓋** docs/05 要求的極短詞、靜音、長句停頓與會議場景；
中英混用樣本在這200筆裡為0（`english_reference_tokens=0`），所以**WER與MER的混用部分尚未量測**。

## 本機結果（M4 Pro、`Alkd/TEA-ASR-1.1-MLX-4bit` revision `caee57a9`）

| 指標 | 結果 |
|---|---:|
| 正規化後 CER | **4.92%** |
| 正規化後 MER | 4.92% |
| raw MER（原樣比對） | 33.37% |
| raw MER（僅移除私用區字元） | 17.14% |
| 英文 WER | 無樣本，未量測 |
| RTF p50 / p95 | 0.033 / 0.045 |

正規化規則：NFKC、英文轉小寫、移除標點與空白類字元；**繁簡不轉換**，避免把真正的字形差異藏起來。
規則與逐筆明細由 `benchmarks/quality_eval.py` 產生，原始JSON在本機ignored檔 `benchmarks/results/`。

raw 與正規化的落差幾乎全部來自兩件事：模型會自己補句號等標點（語料原文沒有），以及下列私用區字元。

## 私用區字元：真實語音也重現

| 觀察 | 數值 |
|---|---:|
| 受影響樣本比例 | **73.5%**（147/200） |
| 私用區字元總出現次數 | 266 |
| 不重複codepoint數 | **210** |

**210個不重複codepoint對266次出現**，代表這不是固定的sentinel集合。範例：

```text
REF: 整理裡面的聲音與字幕
HYP: 整理裡面<U+E371>的聲音與<U+E3C7>字幕。

REF: 爭取必要的資源
HYP: 爭取必要的資源<U+E020>。
```

字元是**插入**在正確文字之間，不是取代正確文字：移除後周圍內容仍與參考一致，MER從33.4%降到17.1%，
其餘落差才是標點。

追查結果：tokenizer的vocab裡**沒有**任何entry含私用區字元；這些字元是模型實際生成的
byte-level token序列（例如 U+E371 對應 ids `[170, 235, 109]`，即UTF-8 `EE 8D B1`）。
因此**不是decode端的bug，是模型輸出本身**，改tokenizer或decode流程不會解決。

checkpoint隨附資料宣告sentinel leak為0，與本次實測不符。

## 尚未做的比較

未與上游 `JacobLinCool/TEA-ASR-1.1`（BF16）在同一語料比較，因此**無法判定leak是MLX 4bit量化造成、
還是上游模型本身就有**。這個比較需要安裝PyTorch，與本專案「不引進Torch」的依賴決策衝突，
應在獨立環境執行。這是解除P0品質封鎖前必須補的證據。

## 結論與後續

1. 內容正確性達到可用水準（CER 4.92%），效能也達標（RTF p50 0.033）。
2. 私用區字元是目前唯一的品質封鎖點，影響近四分之三的句子。
3. 服務維持保留原文並回 `private_use_characters` warning；**在釐清成因前不靜默移除**。
   若要提供移除選項，應寫進 `text` 並讓 `raw_text` 保留原文，而不是直接改寫辨識結果。
4. 下一步證據順序：上游BF16同語料A/B → 若上游乾淨則向轉換作者回報 → 若上游也有則向模型作者回報。
5. 仍缺的語料類型：極短詞、靜音／噪音、長句停頓、中英混用、會議與影片長檔。
