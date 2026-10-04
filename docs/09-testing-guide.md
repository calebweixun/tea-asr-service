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

## 聆聽校稿與 CER 比較

從 soak 音訊與 trace 建立本機校稿頁；沒有下載或連線需求：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/gold_kit.py build
```

用 Finder 開啟 `.soak/gold/<set>/index.html`，播放每段並直接修正草稿。頁面會在瀏覽器
localStorage 自動保存；完成後按「下載答案 JSON」，把檔案放進 `.soak/gold/answers/`。
每個答案保留 ASR 草稿、校正 reference、音樂干擾與聽不清楚標記。

用校正答案比較 trace 或 `{ "item-id": "辨識文字" }` 格式的 JSON hypothesis：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/cer_eval.py .soak/gold/answers/speakers-answers.json \
  .soak/traces/system-a.jsonl .soak/gold/answers/system-b.json
```

報告包含逐段、每個 set 與整體 CER、替換／刪除／插入數、音樂干擾分組，以及兩個以上
system 的逐段 paired bootstrap 95% 差異區間。`聽不清楚` 預設排除；只有 venv 已裝
OpenCC 時才附上簡體轉繁體版本。加 `--fold-pronouns` 會同時回報原分數與折疊
祢→你、祂／它→他的分數；`per_set_concatenated` 會依答案順序連接同一 set 的所有項目，
再計算 CER。

## 離線 Gold 辨識比較

`gold_offline_eval.py` 以服務後端解碼校正答案對應的 WAV 區間，並另存每項解碼時間。
使用本機預備好的模型執行全部 4-bit、8-bit、span、字典與 prompt 變體：

```bash
/Users/c2leb/Codes/tea-asr-service/.soak/gold/run-offline-eval.sh
```

單獨測試 span 與兩秒前後文，並比較原分數、折疊分數及相對於第一個 hypothesis 的
paired bootstrap 95% 區間：

```bash
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/gold_offline_eval.py \
  --answers /Users/c2leb/Codes/tea-asr-service/.soak/gold/answers/speakers-answers.json \
  --wav /Users/c2leb/Codes/tea-asr-service/.soak/audio/church-30m-59m.wav \
  --model-path /Users/c2leb/Codes/tea-asr-service/models/models--Alkd--TEA-ASR-1.1-MLX-4bit/snapshots/caee57a908b6d64be08a6462c7a21ececbd4d7cb \
  --mode span --pad-s 2 \
  --output /Users/c2leb/Codes/tea-asr-service/.soak/gold/eval/span.json
PYTHONPATH="$PWD/src" /Users/c2leb/Codes/tea-asr-service/.venv/bin/python \
  benchmarks/cer_eval.py \
  /Users/c2leb/Codes/tea-asr-service/.soak/gold/answers/speakers-answers.json \
  /Users/c2leb/Codes/tea-asr-service/.soak/gold/eval/01-4bit-segment.json \
  /Users/c2leb/Codes/tea-asr-service/.soak/gold/eval/span.json --fold-pronouns
```

## 整場主日 SRT 評估（church-eval，2026-10）

用使用者人工訂正過的整場 SRT 當答案，量三場主日（各約 2 小時）的離線 CER、串流字幕行為與歌唱偵測。
結果與決策見 [church-eval 報告](benchmarks/church-eval-2026-10.md)。音訊、SRT、逐字稿、trace 全部留在
git-excluded 的 `.soak/church-eval/`（衍生檔在 `work/`），報告只含數字。下面的路徑以
`S=/Users/c2leb/Codes/tea-asr-service`、`W=$S/.soak/church-eval/work`、`PY=$S/.venv/bin/python` 表示，
指令都在 worktree 根目錄用 `export PYTHONPATH=$PWD/src:$PWD HF_HUB_OFFLINE=1` 執行。

```bash
# 1. SRT → 答案 kit（合併相鄰 cue、上限約 15 s、間隔 ≥0.6 s 就切；≥20 s 無字幕的間隔寫進
#    uncaptioned sidecar，不當作語音答案），並轉成 16 kHz mono WAV
for d in 20260704 20260822 20260912; do
  $PY benchmarks/srt_gold.py --srt $S/.soak/church-eval/$d.srt --set-name $d \
    --audio $S/.soak/church-eval/$d.m4a --wav-out $W/$d.wav \
    --answers-out $W/answers/$d-raw.json --uncaptioned-out $W/answers/$d-uncaptioned.json
  $PY benchmarks/srt_gold.py --srt $S/.soak/church-eval/$d.srt --set-name $d \
    --audio $S/.soak/church-eval/$d.m4a --pad-s 0.25 \
    --answers-out $W/answers/$d-pad25.json --uncaptioned-out /dev/null
done
# 對齊檢查：把每個 item 的區間平移 δ 再解碼，CER 最低的 δ 就是 SRT 的固定偏移
$PY benchmarks/church_eval_align.py --answers $W/answers/20260704-raw.json --wav $W/20260704.wav \
  --model-path <4bit snapshot> --output $W/align/20260704.json --count 30 --deltas -1 -0.5 -0.25 0 0.25 0.5 1

# 2. 離線矩陣：標籤 m{4|8}-{base|c<L>[g<G>]}[-repl][-prompt]，可續跑
cp ~/Library/Application\ Support/TEA\ ASR/dictionaries/church.toml $W/church.toml   # 只讀複本
$PY benchmarks/church_eval_matrix.py m4-base m4-c2 m4-c3 m4-c4 m8-base m8-c3 m4-c3-repl \
  m4-c3-repl-prompt m8-c3-repl m8-c3-repl-prompt --dictionary $W/church.toml
$PY benchmarks/church_eval_report.py m4-base m4-c2 m4-c3 m4-c4 m8-base m8-c3 m4-c3-repl \
  m4-c3-repl-prompt m8-c3-repl m8-c3-repl-prompt --dictionary $W/church.toml --repl-base m4-base \
  --out $W/report-main.json            # 每場與合併 CER、cluster bootstrap 95% CI、paired delta

# 3. 字典：用未替換的假設稿挖候選，交叉驗證（兩場挖、第三場測，輪流）與最終合併
$PY benchmarks/church_eval_dict.py cv --base-label m4-c3 --current $W/church.toml
$PY benchmarks/church_eval_dict.py final --base-label m4-c3 --current $W/church.toml --out-dir $S/.soak/church-eval/recommended

# 4. 串流：切 20 分鐘視窗，對 8431 埠的隔離 server 以 1x 串流，再用 SRT 與 OBS replay 評分
$PY benchmarks/church_eval_stream.py windows
#    server：HOME=$W/server-home（含 config.toml 與 dictionaries/church.toml）、
#    TEA_ASR_MODELS_DIR=$S/models、HF_HUB_OFFLINE=1，serve --port 8431；不要碰 8327
$PY benchmarks/church_eval_stream.py capture --token-file <token> --server-log <service.log> \
  --end-silence 600 870 --context-profile church
$PY benchmarks/church_eval_stream.py score --end-silence 600 870 --out $W/stream-metrics.json

# 5. 歌唱偵測：整場 YAMNet + Silero VAD，以字幕當語音真值；--judge-model-path 把每個誤判片段
#    解碼回來，量「被藏掉的是真字幕還是幻聽」；--sweep 掃遲滯／門檻
TEA_ASR_MODELS_DIR=$S/models $PY benchmarks/church_eval_singing.py --end-silence-ms 600 870 \
  --judge-model-path <4bit snapshot> --srt-root $S/.soak/church-eval --out $W/singing/metrics.json
TEA_ASR_MODELS_DIR=$S/models $PY benchmarks/church_eval_singing.py --end-silence-ms 870 --sweep --out $W/singing/sweep.json
```

`soak_real_audio.py` 預設把衍生檔限制在 `<checkout>/.soak/`；worktree 用 `TEA_SOAK_ROOT=$W`
改指向共用的 git-excluded 目錄（`church_eval_stream.py` 已自動設定）。`capture_event_trace.py` 與
`soak_real_audio.py capture` 的 `--context-profile` 等同 OBS 外掛的 `hints_profile`（要 server 開
`context_hints_enabled`，否則 replacement 不會生效）。串流 benchmark 的 server 要設 `keep_warm = true`，
否則視窗之間模型被卸載，下一個 session 會收到 `model_loading`。

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

## 十、從訂正稿挖 replacement 候選

用答案 kit 與一個或多個 id→文字假設稿產生安全候選和人工 review。預設略過標記為 unclear
的項目；style conventions 會另外計數，不會變成 replacement。

```bash
uv run python benchmarks/dict_mine.py \
  --answers .soak/gold/answers/speakers-answers.json \
  --hypothesis .soak/gold/eval/01-4bit-segment.json \
  --candidates-toml .soak/dict-mine/candidates.toml \
  --review-md .soak/dict-mine/candidates-review.md \
  --merge-with docs/examples/dictionaries/church.example.toml
```

Review 會列出 contexts、precision proxy、harm、style convention 次數、number formatting (style) pairs，以及和既有字典的重複或衝突。數字格式候選只供人工參考，不會進入候選 TOML。
可用 `--hotwords-file` 放寬網域詞的 count/support 門檻，或用
`--stoplist-file` 擴充常見詞排除表。輸出含逐字稿片段，請留在 git-excluded 的
`.soak/`；加入字典前先人工檢查。

## 同音候選 + 模型重評分（離線實驗，2026-10）

想法：像輸入法選字。對可疑片段用拼音找出聽起來像字典詞（`to` 值、hotwords、帶稱謂的名字）的候選，
再讓 ASR 模型自己對**同一段音訊**做 teacher-forced 對數似然來挑。純離線 benchmark
（`benchmarks/phonetic_rescore.py`），live server 不 import 它。結果見
[報告](benchmarks/phonetic-rescore-2026-10.md)。需要 `pypinyin==0.55.0`（只裝在 venv，**不在** pyproject／uv.lock；
測試用 `pytest.importorskip`，CI 沒裝時會跳過）。私有資料與快取都在 git-excluded 的 `.soak/`。
路徑沿用上一節的 `S`、`W`、`PY`；`LIVE=~/Library/Application\ Support/TEA\ ASR/dictionaries/church.toml`（唯讀）。

```bash
export PYTHONPATH=$PWD/src:$PWD HF_HUB_OFFLINE=1
M8=$S/models/mlx-8bit-selfconv; mkdir -p $W/rescore
# 1. 候選 + 模型打分（每場一次；交叉驗證用「另兩場挖出來的」fold 字典與名字，避免洩漏）
for d in 20260704 20260822 20260912; do
  others=$(for o in 20260704 20260822 20260912; do [ $o != $d ] && echo $W/answers/$o-pad25.json; done)
  $PY benchmarks/phonetic_rescore.py score --answers $W/answers/$d-pad25.json --wav $W/$d.wav \
    --hypotheses $W/eval/m8-c3/$d.json --dictionary $W/dict-cv/$d/fold-safe.toml \
    --hotwords-from "$LIVE" --name-references $others --model-path $M8 --out $W/rescore/$d.jsonl
done
#    手動訂正集（用 live 字典）
$PY benchmarks/phonetic_rescore.py score --answers $S/.soak/gold/answers/speakers-answers.json \
  --wav $S/.soak/audio/church-30m-59m.wav --hypotheses $S/.soak/gold/eval/03-8bit-segment.json \
  --dictionary "$LIVE" --name-references $W/answers/2026*-pad25.json --model-path $M8 --out $W/rescore/gold.jsonl
# 2. 交叉驗證調參（兩場調、第三場測）＋ norm／margin／oracle 表
$PY benchmarks/phonetic_rescore.py tune --work $W --gold-answers $S/.soak/gold/answers/speakers-answers.json \
  --results $W/rescore/results.json
# 3. 端到端重跑（每個 fold 用自己的參數）並量每個 final 的額外時間
for d in 20260704 20260822 20260912; do
  $PY benchmarks/phonetic_rescore.py time --answers $W/answers/$d-pad25.json --wav $W/$d.wav \
    --cache $W/rescore/$d.jsonl --tuned $W/rescore/results.json --fold $d --model-path $M8 --out $W/rescore/$d.e2e.jsonl
done
$PY benchmarks/phonetic_rescore.py time --answers $S/.soak/gold/answers/speakers-answers.json \
  --wav $S/.soak/audio/church-30m-59m.wav --cache $W/rescore/gold.jsonl --tuned $W/rescore/results.json \
  --model-path $M8 --out $W/rescore/gold.e2e.jsonl
# 4. 彙整（只有數字）
$PY benchmarks/phonetic_rescore.py report --work $W --gold-answers $S/.soak/gold/answers/speakers-answers.json \
  --results $W/rescore/results.json --markdown docs/benchmarks/phonetic-rescore-2026-10.md
$PY -m pytest tests/unit/test_phonetic_rescore.py -q   # 沒有 pypinyin 時只跑決策規則／指標測試
```

限制：teacher-forced 打分只做在 item 音訊（不含 carry 的 3 s）；模型原始輸出含私用區字元（PUA），
過濾後的文字對模型來說略偏離分佈，但原稿與候選同樣被過濾，所以比較仍然公平。
