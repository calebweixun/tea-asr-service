# P0 本機驗證報告

執行日期：2026-09-18。狀態：**效能可行；品質驗收尚未通過。**

## 環境與版本

- Apple M4 Pro、48 GiB RAM、arm64 macOS 26.6.2
- uv 管理的 CPython 3.12.11
- MLX 0.32.2、mlx-audio 0.4.5、transformers 5.12.1
- `Alkd/TEA-ASR-1.1-MLX-4bit` revision `caee57a908b6d64be08a6462c7a21ececbd4d7cb`
- Python環境約427 MiB；模型cache約1.2 GiB；依賴樹沒有PyTorch

模型檔案已逐一計算SHA-256並寫入根目錄 `models.lock.json`。模型能以本機snapshot、混合量化predicate修正及 `strict=True` 載入；關網路後的local-files-only路徑已由service整合測試使用。

## 真模型結果

測試音訊由macOS `say -v Meijia` 合成，再以FFmpeg轉成16 kHz mono PCM；內容為「這份 PR 已經 merge 了，我們下午跟 client 開會。」。合成音訊只驗證管線與效能，不能代表真實口語品質。

| 指標 | 本機結果 |
|---|---:|
| 音訊長度 | 4.249秒 |
| worker嚴格載入 | 0.82–1.03秒 |
| 冷CLI端到端 | 5.36秒 |
| 冷CLI模型推論 | 1.89秒 |
| 暖worker完整句推論 | 0.17–0.22秒 |
| 暖worker完整句RTF | 約0.04–0.05 |
| 累積預覽6次總推論 | 0.96秒 |
| 累積預覽單次RTF | 約0.04–0.20 |
| 最大RSS（獨立CLI量測） | 約1.46 GiB |

累積預覽的中途結果曾把 `client` 判為「客戶溝通」，收到後文後改為 `client 開會`。這證明整段替換修訂機制能利用後文，仍需真實語料量化「錯改對」與「對改錯」。

## 阻礙

辨識結果穩定夾帶Unicode私用區字元，例如 U+E013、U+E305、U+E095：

```text
這份 pr 已經<U+E013> merge 了，我們<U+E305>下午跟 client 開會<U+E095>。
```

checkpoint隨附資料宣告sentinel leak為0，但這次mlx-audio 0.4.5＋量化模型的合成音訊實測出現leak。服務目前保留原文並回 `private_use_characters` warning；沒有靜默刪除，因為尚未證明這些字元只是可安全移除的sentinel。

已用transformers 5.5.0與5.12.1 A/B；兩者輸出逐字相同，因此排除這段版本範圍內的tokenizer套件漂移。專案鎖回本次完整驗證使用的5.12.1。

P0品質驗收需補真實台灣華語、短詞、靜音與中英混合corpus。若真實語音也重現，應比較原始BF16或其他MLX量化，並向模型轉換作者確認；未解決前不能宣稱正式品質已通過。

## 已驗證的垂直切片

真模型已透過受監督子程序與binary IPC啟動；HTTP health、授權status與PCM transcription均實際通過。一次請求的service結果為暖推論218 ms，並正確回傳私用區warning。測試原始JSON在本機ignored檔 `benchmarks/results/p0.json`，可用 `benchmarks/p0_revisable.py`重跑。
