# 09｜測試指南

給要實際用用看的人。每一節都可以獨立執行，不必照順序。

## 一行確認全部狀態

```bash
uv run tea-asr doctor
```

它會回報環境、模型與 VAD 資產、執行中的服務狀態，並在模型 ready 時實際送一秒音訊
走完整條推論路徑。`service.reachable` 為 false 就先啟動服務：

```bash
uv run tea-asr serve
```

服務監聽 `127.0.0.1:8327`。要讓它登入時自動啟動：`uv run tea-asr service install`。

## 一、Mac client：語音輸入

```bash
open "clients/macos/build/TEA ASR.app"
```

選單列會出現一個麥克風圖示。選「開始聽寫」（或按 ⌥⌘D）。

- 第一次會要求**麥克風權限**。
- 要讓文字自動貼進前景 app，還需要**輔助使用權限**：選單列 →「設定…」→「開啟輔助使用設定」。
  沒給也能用，定稿會放進剪貼簿，自己按 ⌘V。

講一句、停一下，server 會自己斷句並把定稿貼出來。**不用按任何鍵標記句子結束。**

要看的重點：
- 停頓後多久出字（實測 p95 約 0.5 秒）。
- 第一個字有沒有被吃掉——這是校準過的重點，如果又出現請告訴我。
- 中英混用（「這個 PR 已經 merge 了」）有沒有保留英文。

## 二、Mac client：會議記錄

選單列 →「開始會議記錄」。會開一個視窗即時累積逐字稿：

- 灰色那行是**暫定文字**，會隨後文改寫。
- 黑色的是**定稿**，之後不會再變。
- 每段定稿都會自動寫入 `~/Library/Application Support/TEA ASR/meetings/`，
  所以當掉或忘記存檔也不會整場消失。
- 「存成 Markdown…」可以另存到你要的位置。

要看的重點：長時間（20 分鐘以上）會不會變慢、記憶體會不會一直長、有沒有漏段。

## 三、不用 GUI 的驗證

不需要任何權限，直接把一段錄音走完整個 client 路徑：

```bash
clients/macos/.build/release/TeaASRClient --selftest /path/to/16k-mono.wav
```

加 `--preview` 會顯示邊說邊修訂的過程。

## 四、錄自己的樣本給後續調整用

```bash
uv run python examples/mic_stream.py --device 2 --save-wav ~/tea-asr-takes/take2.wav
```

存下來的是**實際送給 server 的音訊**，所以同一段可以反覆換參數重跑，不必重講：

```bash
uv run python benchmarks/replay_segmenter.py ~/tea-asr-takes/take2.wav --transcribe
uv run python benchmarks/replay_segmenter.py ~/tea-asr-takes/take2.wav --transcribe --end-silence-ms 700
```

斷句太碎就把 `--end-silence-ms` 調大，太黏就調小。

## 五、量測

| 想知道什麼 | 指令 |
|---|---|
| 對固定語料的品質（CER／MER） | `uv run python benchmarks/quality_eval.py --limit 200` |
| 串流預覽的延遲與修訂行為 | `uv run python benchmarks/preview_eval.py --wav <take>.wav` |
| 預覽會不會擋住正式辨識 | `uv run python benchmarks/mixed_load.py --wav <take>.wav` |
| 長時間穩定性 | `uv run python benchmarks/soak_continuous.py --wav <take>.wav --minutes 60` |

## 六、會遇到的已知狀況

- **辨識結果夾帶看不見的私用區字元。** 約七成的句子會有，服務會在 `warnings` 標示
  `private_use_characters` 但不會靜默刪掉。成因與現況見 [P0 品質報告](benchmarks/p0-quality-report.md)。
- **久沒用之後第一次啟動會等幾秒**：閒置 15 分鐘後模型會卸載釋放記憶體，
  client 會顯示「模型載入中…」並自動等待。不想卸載就在 `config.toml` 設 `keep_warm = true`。
- **機器睡眠醒來後進行中的 session 會被中止**，client 需要重新開始。
  v0.1 沒有 resume，把睡眠前後的音訊接在同一個時間軸上會是假的。
- **一直講不停會在 12 秒附近被切段**，切點會挑最近的安靜處。
- **同音詞與人名仍會認錯**（「姿勢」→「知識」、「林佳蓉」→「林嘉蓉」），這是模型層的限制。

## 七、模型不見了怎麼辦

`doctor` 回報 `model_prepared: false` 但服務還跑得起來，通常表示資產被刪了
（曾發生過：磁碟剩不到 6 GB 時 macOS 清掉快取）。服務要到下一個辨識請求才會失敗。

```bash
uv run tea-asr model-prepare
```

資產現在放標準的 Hugging Face cache，不再放在系統會回收的 `~/Library/Caches`。

## 八、回報問題時附上什麼

```bash
uv run tea-asr doctor > doctor.json
tail -200 ~/Library/Logs/TEA\ ASR/service.log > service-log.txt
```

如果是辨識品質問題，加上用 `--save-wav` 錄下的那段音訊與它的 `.events.jsonl`，
這樣同一個情境可以被重現與反覆測試。
