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

## 六、真實錄音字幕 soak

`benchmarks/soak_real_audio.py` 會把本機錄音切成 16 kHz mono PCM16、以 1x 送到測試 server、
重播事件 trace 到 OBS 外掛字幕 state machine，並輸出 metrics report 與本機逐字稿 review。
原始音訊、WAV、trace、server log sidecar 與 review 都放在 `.soak/`；請把 `/.soak/`
加到這個 worktree 的 `.git/info/exclude`，不要放進 `.gitignore`。Review 含私人逐字稿，不可提交；
metrics report 不含逐字稿。

準備 30:00–59:09 的預設區間與三個說話者 sections：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py extract
```

`extract` 預設讀取本機指定錄音。可用 `--input` 指定其他檔案、`--start`／`--end` 覆寫時間，
也可用重複的 `--section NAME,START,END` 覆寫 sections（時間接受秒數、`MM:SS` 或 `HH:MM:SS`；
最後一段的 END 可寫 `end`）。兩分鐘 fake-backend smoke cut：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py extract --start 00:30:00 --end 00:32:00 \
  --out .soak/audio/fake-2m.wav --manifest .soak/audio/fake-2m.sections.json
```

Fake backend 使用 OBS 外掛 e2e harness 的 `tests/e2e/fake_asr_server.py`，並把 `--service-dir`
指向目前 worktree，因此載入這個 worktree 的 server code。它不載入模型：

```bash
mkdir -p .soak/fake/state .soak/traces
/Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  /Users/c2leb/Codes/obs-plugins/tea-live-subtitle/tests/e2e/fake_asr_server.py \
  --service-dir "$PWD" --port 8422 \
  --state-dir "$PWD/.soak/fake/state" \
  --request-log "$PWD/.soak/fake/requests.jsonl" --revisable --emulate-segmentation
```

另一個終端機以 fake server 建立的 token 做 1x capture：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py capture --wav .soak/audio/fake-2m.wav \
  --url ws://127.0.0.1:8422/v1/stream \
  --token-file .soak/fake/state/token --end-silence 300 \
  --manifest .soak/audio/fake-2m.sections.json \
  --server-log .soak/fake/state/logs/service.log \
  --out .soak/traces/fake-2m-300.jsonl
```

Fake server 結束後，重播、分析並產生報告：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py replay .soak/traces/fake-2m-300.jsonl
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py analyze .soak/traces/fake-2m-300.jsonl
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py report .soak/traces/fake-2m-300.jsonl
```

`analyze` 會同時計算 trace/server 指標與 replay hard thresholds；hard failure 時回傳非零。
`report` 寫 metrics-only Markdown 到 `.soak/reports/`，另寫 `.soak/review-*.md`，按錄音分鐘列出
final 與字幕畫面在 segment close 時的內容。

Replay 的每條重複字幕行會對照該 segment 的 server partial/final 分類：`plugin-origin` 表示重複單位在 server 文字裡找不到 fuzzy 的重複；`model-origin` 表示 server partial 已包含重複；`speech-origin` 表示 server final 至少包含兩份 fuzzy 相符的單位。比對會先移除標點與空白、統一大小寫及全形字元，再允許每 4 個字元至多 1 個編輯差異。只有 `plugin-origin` 計入 hard threshold；`model-origin` 和 `speech-origin` 會列在報告的 soft 分類中。無法對應 trace final 的 replay 行會保守計入 plugin hard threshold。

已有 `caption-replay` binary 時可透過 `--replay-binary` 重用，避免建置 plugin：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py analyze .soak/traces/fake-2m-300.jsonl \
  --replay-binary .soak/build/caption-replay
```

真實模型必須使用獨立 HOME、主 checkout 已準備好的 `models/`，並設 `HF_HUB_OFFLINE=1`：

```bash
SOAK_HOME="$PWD/.soak/real-home"
mkdir -p "$SOAK_HOME" .soak/traces
HOME="$SOAK_HOME" TEA_ASR_MODELS_DIR=/Users/c2leb/Codes/tea-asr-service/models \
  HF_HUB_OFFLINE=1 PYTHONPATH="$PWD/src" \
  /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  -c 'from tea_asr.cli import main; raise SystemExit(main())' serve --port 8421
```

服務建立的 token 與 log 分別位於 `$SOAK_HOME/Library/Application Support/TEA ASR/token` 和
`$SOAK_HOME/Library/Logs/TEA ASR/service.log`。服務持續執行時，另一個終端機先後執行兩個 29 分鐘
capture；每次完成後跑同一組 `replay`、`analyze`、`report`：

```bash
SOAK_HOME="$PWD/.soak/real-home"
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py capture --wav .soak/audio/church-30m-59m.wav \
  --url ws://127.0.0.1:8421/v1/stream \
  --token-file "$SOAK_HOME/Library/Application Support/TEA ASR/token" \
  --server-log "$SOAK_HOME/Library/Logs/TEA ASR/service.log" \
  --manifest .soak/audio/church-30m-59m.sections.json --end-silence 300 \
  --out .soak/traces/church-300.jsonl

PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/soak_real_audio.py capture --wav .soak/audio/church-30m-59m.wav \
  --url ws://127.0.0.1:8421/v1/stream \
  --token-file "$SOAK_HOME/Library/Application Support/TEA ASR/token" \
  --server-log "$SOAK_HOME/Library/Logs/TEA ASR/service.log" \
  --manifest .soak/audio/church-30m-59m.sections.json --end-silence 600 \
  --out .soak/traces/church-600.jsonl
```

Capture 結束後，對兩個 run 產生各自的 metrics 與 review：

```bash
for run in church-300 church-600; do
  trace=".soak/traces/${run}.jsonl"
  PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
    benchmarks/soak_real_audio.py replay "$trace"
  PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
    benchmarks/soak_real_audio.py analyze "$trace"
  PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
    benchmarks/soak_real_audio.py report "$trace"
done
```

完成後在 server terminal 按 Ctrl-C。對所有已保存 trace 重做 plugin replay 與 hard-threshold 檢查：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/replay_regression.py
```

可用 `TEA_SOAK_TRACES=/path/to/traces` 或 `--trace-dir` 指定 trace 目錄。Replay 會從
`/Users/c2leb/Codes/obs-plugins/tea-live-subtitle/tests/replay/` 編譯到 `.soak/build/`；不需要 OBS。
硬門檻為 0 invalid_ipc、layout moves、mid-speech fade-outs、duplication lines，以及沒有超過 10 秒
未出文字的 speech stretch。stable gap p95 > 3 秒與 final latency p95 > 1.5 秒只列 soft failure。

2026-09-28 驗證狀態：extract 與 synthetic trace 的 replay/analyze/report 已在此環境跑通；fake server
嘗試 bind `127.0.0.1:8422` 回 `Operation not permitted`，因此此 sandbox 無法驗證 live capture。真模型也
沒有在此環境啟動；請在可 loopback bind 的終端機照上方命令執行，不要改用 8327。

## 七、會遇到的已知狀況

- **辨識結果原本會夾帶看不見的私用區字元，現在預設過濾掉。** 根因是
  `Alkd/TEA-ASR-1.1-MLX-4bit` 的 4bit 量化（上游 BF16 是 0%、換 8bit 也只降到
  63.3%，都不是可行的解），詳見 [PUA vs BF16 A/B 報告](benchmarks/pua-bf16-ab-report.md)。
  服務預設把 `text`（partial 與 final 都會過濾）裡的 PUA 字元移除，`raw_text`
  仍保留原始辨識供除錯；`warnings` 照樣標示 `private_use_characters`。可用
  `filter_pua = false`（設定檔）或 `TEA_ASR_FILTER_PUA=0`（環境變數）關閉這個
  過濾，恢復未過濾行為。這是繞過上游量化缺陷的暫時措施，不是永久方案；上游若
  提供乾淨的量化模型就可以移除，見 `tea_asr/api/stream.py` 的
  `filter_private_use_characters`。
- **久沒用之後第一次啟動會等幾秒**：閒置 15 分鐘後模型會卸載釋放記憶體，
  client 會顯示「模型載入中…」並自動等待。不想卸載就在 `config.toml` 設 `keep_warm = true`。
- **機器睡眠醒來後進行中的 session 會被中止**，client 需要重新開始。
  v0.1 沒有 resume，把睡眠前後的音訊接在同一個時間軸上會是假的。
- **一直講不停會在 12 秒附近被切段**，切點會挑最近的安靜處。
- **同音詞與人名仍會認錯**（「姿勢」→「知識」、「林佳蓉」→「林嘉蓉」），這是模型層的限制。

## 八、模型不見了怎麼辦

`doctor` 回報 `model_prepared: false` 但服務還跑得起來，通常表示資產被刪了
（曾發生過：磁碟剩不到 6 GB 時 macOS 清掉快取）。服務要到下一個辨識請求才會失敗。

```bash
uv run tea-asr model-prepare
```

資產現在放標準的 Hugging Face cache，不再放在系統會回收的 `~/Library/Caches`。

## 九、回報問題時附上什麼

```bash
uv run tea-asr doctor > doctor.json
tail -200 ~/Library/Logs/TEA\ ASR/service.log > service-log.txt
```

如果是辨識品質問題，加上用 `--save-wav` 錄下的那段音訊與它的 `.events.jsonl`，
這樣同一個情境可以被重現與反覆測試。

即時字幕整段沒有字時，先看那段時間的 `stream.heartbeat` 與 `stream.audio_*`／`stream.vad_no_speech`
警告，對照 [04「怎麼診斷『沒有字幕』」](04-api.md#怎麼診斷沒有字幕) 的表。要重現同一段聲音，
暫時用 `TEA_ASR_DEBUG_CAPTURE_AUDIO=1` 啟動服務（會把串流進來的聲音存到
`~/Library/Logs/TEA ASR/captures/`，查完請關掉並刪除）。
