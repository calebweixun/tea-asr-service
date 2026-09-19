# 併發 continuous session 上限量測報告

執行日期：2026-09-19。狀態：**已量測，據此設定 enforcement 上限。**

## 背景

`CapabilityLimits.max_continuous_sessions` 從一開始就只是一個寫在 wire schema
裡的數字（`1`），`/v1/stream` 從未真正拒絕過超過這個數字的連線——`app.py` 的
`/v1/stream` 端點只有 `activity.sessions += 1`，之後沒有任何拒絕邏輯。這份報告
補上那個從未做過的量測，回答一個具體問題：**在這台機器、這個服務架構下，同時開
幾個 continuous session 還算安全？**

`src/tea_asr/worker/supervisor.py` 只起一個 MLX 子行程、用一把 `asyncio.Lock`
序列化所有推論；`src/tea_asr/scheduler.py` 在其前面做有界的 admission，並依
`interactive > realtime > preview`（docs/03）排優先權。這代表「並行上限」量的
不是「模型能不能同時吃兩份工作」（不能，永遠序列化），而是：N 個 continuous
session 同時把音訊灌進這唯一一個 worker，還能不能維持：

1. 每個 session 的 backlog（已送出音訊 − 已定稿音訊）不持續成長；
2. 端到端延遲（說完到 `transcript.final` 抵達）維持在可用範圍；
3. 高優先權的 HTTP `interactive` 請求不被 `realtime` 流量餓死；
4. RSS 不隨 N 不受控成長；
5. 辨識結果不因為併發而改變（同一份音訊、同一個 worker，序列化推論理論上
   每一段的結果都應該與單獨執行時相同，只是排隊時間不同）。

## 方法

新增 `benchmarks/concurrency_capacity.py`：

```bash
uv run tea-asr serve   # 已在跑的正式服務，本機常駐、單一 worker
uv run python benchmarks/concurrency_capacity.py \
    --wav benchmarks/generated/taiwan.wav --max-n 4 --seconds 90
```

對 N = 1, 2, 3, 4，依序（不是同時）做一輪 90 秒的試驗：

- 開 N 個 `continuous` / `final_only` WebSocket session，各自用自己的 wall
  clock 把同一段 4.25 秒真人口語錄音（`benchmarks/generated/taiwan.wav`）
  循環、以真實時間（real-time pacing）送出——這確保 realtime 優先權的負載是
  「一直有人在說話」的最壞情況，而不是灌一次就結束。
- 同時每 4 秒打一次 `/v1/transcriptions`（HTTP、`interactive` 優先權），
  模擬使用者在用其中一個 continuous session 聽寫的同時，臨時叫用一次性
  utterance 辨識——這是 docs/03 優先權設計本來就要保證「不互相餓死」的情境。
- 每 10 秒記錄一次每個 session 的 backlog（`已送樣本 − 已定稿樣本`，換算秒數）。
- 記錄「說完到定稿」延遲（用 `soak_continuous.py` 同一套定義：session 起點
  wall clock + `end_sample`／16000 推算說話結束時刻，與 final 抵達時刻相減）。
- 記錄每輪結束時的 RSS（`ps` 依 command 分組加總，`worker`／`service` 各一組）。
- 兩輪之間睡 5 秒，讓 worker 回到穩定狀態，避免 N=k 的殘留 backlog 污染 N=k+1。

量測前用 `/v1/status` 確認機器沒有其他重度 GPU/CPU 工作在跑（`idle_unloaded`、
`active_sessions:0`），並先送一次 HTTP 請求把模型從 `idle_unloaded` 喚醒到
`ready`，避免第一輪把模型載入時間算進延遲。

## 環境

Apple M4 Pro、14 核心、48 GiB 統一記憶體，macOS 26 (26A428)，單一
`tea-asr serve` 常駐行程，模型 `Alkd/TEA-ASR-1.1-MLX-4bit`
（`caee57a9…`）。量測期間機器閒置（`top` 顯示 CPU 81% idle），沒有其他
agent/工作在跑。

## 結果

原始資料：`benchmarks/results/concurrency-capacity.json`。

| N | finals/session | 說完到定稿 p50 | p95 | max | backlog 趨勢 | HTTP interactive | worker RSS | service RSS |
|--:|:--:|--:|--:|--:|---|---|--:|--:|
| 1 | 8 | 0.418 s | 1.252 s | 1.252 s | 收斂（−6.28 s） | 21/21 皆 200，p95 0.207 s | 1,508 MB | 96 MB |
| 2 | 8, 8 | 0.734 s | 1.666 s | 1.666 s | 收斂（−6.28 s） | 21/21 皆 200，p95 0.209 s | 1,507 MB | 100 MB |
| 3 | 8, 8, 8 | 0.855 s | 1.695 s | 2.131 s | 收斂（−6.28 s） | 21/21 皆 200，p95 0.409 s | 1,516 MB | 102 MB |
| 4 | 8, 8, 8, 8 | 1.086 s | 2.081 s | 2.661 s | 收斂（−6.28 s） | 21/21 皆 200，p95 0.382 s | 1,516 MB | 105 MB |

「backlog 趨勢」是每個 session 的 backlog 序列中，最後一筆減第一筆的最大值
（跨 session 取最差）；四輪全部是負值，代表 90 秒內 backlog 是在收斂而不是
成長——沒有任何一輪出現「送得比轉得快」的情況。

**沒有任何一輪出現錯誤**（`session_limit`、`queue_full`、`segment.error`、
連線中斷都沒有）。

**每個 session 的 final 段數在所有 N 下都是 8 段，逐段內容一致**（同一份
4.25 秒音訊在 90 秒內循環約 21 次，VAD 依相同的靜音/語音邊界切出相同段數）；
這是預期中的結果——序列化推論下，每一段送進 worker 的 PCM 完全相同，辨識
結果不會因為排在誰後面而改變，只有排隊時間會變。**沒有觀察到並行導致的辨識
品質劣化。**

**HTTP interactive 請求在 N=4 時仍然 100% 成功、p95 仍在 0.4 秒內**——這是
`interactive > realtime` 優先權設計確實生效的直接證據：就算 4 個 continuous
session 同時灌音訊，一次性的 utterance 請求依然幾乎不用排隊。

**worker RSS 幾乎不隨 N 變動**（1,508→1,516 MB，+0.5%）：單一 MLX 子行程處理
所有 session 的音訊，記憶體開銷主要是模型權重本身，不是每個 session 的狀態。
**service RSS 隨 N 小幅成長**（96→105 MB，每多一個 session 約 +3 MB），量級
上完全不構成問題。

**延遲隨 N 大致線性成長**：p50 每多一個 session 增加約 0.2–0.3 秒，p95 在
N=4 時到 2.08 秒、單一 outlier 到 2.66 秒。這是序列化推論的直接後果——N 個
session 都在說話時，每個 segment 平均要等其他 N−1 個處理完才輪到自己。

## 沒有涵蓋的（推論，不是量測）

- **只測到 N=4**，因為這是單一使用者的本機桌面服務（`docs/03`：「每台機器
  只跑一份服務與一份模型」），測更高的 N 對這個產品形態的代表性遞減，且會
  排擠這台開發機當時其他工作。N=4 之內沒有找到任何劣化跡象，**不代表 N=5
  就會壞**，只是還沒量。
- **每輪只跑 90 秒**，不是 `p2-soak-report.md` 那種一小時。backlog 在 90 秒
  內收斂是強訊號，但不能排除更長時間、更多樣的語速/靜音分布下是否仍然收斂；
  這份報告只承諾「90 秒、這份素材、這個負載模式下沒有成長趨勢」。
  一小時多 session soak 是後續可以再補的量測，不影響本次 enforcement 的
  上限選擇（見下）。
- **只用同一份 4.25 秒錄音**，語者、語速、口音單一。
- **revisable/preview 沒有和多 session 一起測**——這裡的負載都是
  `transcript_mode=final_only`；preview 疊加多 session 是另一組尚未量測的
  組合。
- **N 個 session 的到達時間是同時的**（全部一起連線、一起開始說話），不是
  逐一到達再疊加；錯開到達的情境沒有測。

## 建議的上限與依據

**`ServiceConfig.max_continuous_sessions` 預設設為 2**（可透過
`config.toml` 的 `[service] max_continuous_sessions` 調整），`/v1/stream`
對第 3 個以上的 `continuous` `session.start` 回 `concurrent_session_limit`
並拒絕。

- **正確性上限（量測到的）是 4**：N=1..4 全部零錯誤、backlog 收斂、辨識結果
  一致、interactive 沒被餓死。如果之後有真實需求，把 `max_continuous_sessions`
  調到 3 或 4 有這份量測撐腰，不是憑空放寬。
- **預設值 2 是產品面的選擇，不是「3、4 不安全」**：這是單一使用者的本機
  桌面服務，典型情境最多是「主視窗聽寫 + 一個 OBS 字幕來源」同時開兩個
  continuous session；量測顯示 N=2 時 p95 延遲 1.67 秒，仍在
  `docs/05`／`p2-soak-report.md` 沿用的「p95 ≤ 2 秒」可用基準之內，N=4 的
  2.08 秒已經摸到那條線。把預設值訂在量測安全上限（4）而不是留延遲餘裕，
  對這個產品沒有實際好處，卻讓單一失控的 client（例如忘了關閉舊連線）更容易
  把延遲推到使用者能感覺到的程度。
- 這個值現在是**可調的**（`config.toml`），不再是寫死、也不再是文件推導；
  之前唯一的「1」既沒有量測依據，也從未被強制執行。

## 對 client 的影響（例如 OBS 外掛）

超過上限的 `session.start` 現在會收到：

```json
{"type":"error","code":"concurrent_session_limit","message":"併發 continuous session 已達上限（2），請稍後再試或等其他 session 結束。","retryable":true}
```

接著連線以 **WS close code 4029** 關閉——這是特意選的、不與現有
`queue_full`／`session_limit`／`slow_client` 共用的 1013 混在一起的新代碼
（4000–4999 是 RFC 6455 §7.4.2 保留給應用私有用途的區段）。`retryable:true`
的理由：這和 `queue_full` 同一類——是暫時性的容量背壓，另一個 session 結束
就會空出名額，client 應該退避後重試，而不是把它當成永久性設定錯誤。

`code=concurrent_session_limit` 與既有 `code=session_limit`（單一 session
內部待轉錄佇列滿了，`api/stream.py` 的 `MAX_PENDING_SEGMENTS=16`）語意完全
不同，刻意沒有共用同一個字串，避免 client 端把兩種情況誤判成同一種恢復策略。
