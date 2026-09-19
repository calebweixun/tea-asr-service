# `JacobLinCool/TEA-ASR-1.1-mini` 評估報告

執行日期：2026-09-19。狀態：**已完成三形態實測（BF16 / 自轉 MLX 4bit / 自轉 MLX 8bit），
可與既有 full 系列四組數字並列比較。不建議切換生產模型；雙模型並用有明確可行路徑，但需要
具體的架構改動（見文末）。**

本報告是 `docs/benchmarks/pua-bf16-ab-report.md` 的延伸，**不改動**該檔案已驗證的任何數字，
只在此新增 mini 版本的對照。方法論（語料、比較腳本、環境隔離原則）完全沿用該報告，這裡不重複
背景說明。

## 這份報告要回答的三個問題

1. mini 量化後**會不會也出現 PUA**？
2. 使用者說「mini 準確率誤差跟原生差不多」——**這句話對不對**？
3. 「雙模型並用」（mini 跑 partial、full 跑 final）**可不可行、代價是什麼**？

## 方法

- **模型來源**：`JacobLinCool/TEA-ASR-1.1-mini`，revision `98c58048572b44839dfcfa60de3ad7e365a5b232`
  （執行本次任務時的 main HEAD，repo 沒有標記 release tag，換用前建議先確認）。BF16 safetensors
  約 1.5 GiB，下載到專案 `models/` 底下（`cache_dir=models/`），沒有進 `~/Library/Caches`。
  下載前確認磁碟可用空間 55Gi，下載+兩份自轉量化後仍有 51Gi 可用，沒有觸及告警線。
  該 repo 附帶的 `minimal_injection_deployability.json` 是模型作者自己的訓練/驗證中繼資料
  （`base: Qwen/Qwen3-ASR-0.6B`，`sentinel_leaks: 0` 是模型作者的自我宣告），**不是可信的驗證
  結果**——full 版本的 checkpoint 也曾在附帶資料裡宣告 sentinel leak 為 0，但實測不符
  （見 `docs/benchmarks/p0-quality-report.md`），所以這次一樣只採信本機實測，不採信附帶宣告。
- **量化**：直接沿用 `benchmarks/convert_quant.py`，只是把來源換成 mini 的本機 BF16 快照，
  用同樣的標準流程（`q_mode=affine`，沿用 `Qwen3ASRModel.model_quant_predicate` 預設值，
  audio_tower 不量化）轉出 4bit 與 8bit 兩份，輸出到 `models/mlx-4bit-mini`、
  `models/mlx-8bit-mini`。
- **PUA 統計**：MLX 兩形態沿用 `benchmarks/pua_ab_local_pass.py`（原樣，只換 `--model-path`）；
  BF16 形態沿用 `benchmarks/pua_ab_bf16_pass.py`，這支腳本原本把 repo/revision 寫死成 full
  版本，這次**加了 `--repo`/`--revision`/`--pass-name` 三個參數**（保留原本預設值，行為對 full
  版本完全不變），才能不複製一份腳本就跑 mini。同一個 PUA 偵測正則
  （`[-]`，BMP 私用區）、同一批 30 筆語料
  （`adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw` test split，revision `ff0e8047bd`）。
- **MER**：`benchmarks/quality_eval.py` 原本寫死用生產模型（`TEA_ASR_1_1_MLX_4BIT`），這次
  **加了 `--model-path`/`--model-label` 參數**（省略時預設行為不變，等同原本的生產模型），
  才能對 mini 的兩個 MLX 形態、以及 full 的兩個自轉形態（之前只測過 PUA，沒測過 MER，這次
  一併補上）跑同一套 MER 計算。BF16 的 MER 沒辦法用 `TeaMlxBackend`（那是 MLX 專用後端），
  所以新增 `benchmarks/quality_eval_bf16.py`：**它不是另兜一套指標**，`edit_distance`／
  `normalize`／`tokenize_mixed`／`PRIVATE_USE` 判定全部直接從 `quality_eval.py` import，
  只有推論呼叫換成 `qwen_asr.Qwen3ASRModel`（torch），統計邏輯逐行一致。
- **記憶體與延遲的序列化保證**：每次量測前用 `ps aux | grep -iE "mlx|torch|python.*bench"`
  確認本機沒有其他 MLX/torch 推論行程在跑（整個任務期間跑了 4 次檢查，包括量測前與量測後，
  唯一在跑的無關行程是 GUI 版 `Icon Composer.app`，不吃 GPU/CPU 推論資源）。延遲數字用兩種
  獨立腳本、不同樣本量交叉驗證是否穩定：mini 4bit 在 `pua_ab_local_pass.py`（30 筆）量到
  平均 0.076s，`mem_probe.py`（5 筆，`/usr/bin/time -l` 量 peak RSS）量到 0.079s——同量級、
  沒有異常波動，判定數字穩定，不需要重跑。mini 8bit、mini BF16 同樣兩兩交叉核對過
  （見下方表格）。

## 結果一：PUA 比例——mini 量化後也會 PUA，且比例比 full 低但沒有消失

| 模型 | 含PUA句數 | 比例 | PUA出現總次數 |
|---|---:|---:|---:|
| mini BF16（上游原生） | 0/30 | **0.0%** | 0 |
| mini MLX 4bit（自轉，標準流程） | 11/30 | **36.7%** | 19 |
| mini MLX 8bit（自轉，標準流程） | 19/30 | **63.3%** | 33 |

對照組（既有數字，未改動）：

| 模型 | 含PUA句數 | 比例 |
|---|---:|---:|
| full BF16（上游原生） | 0/30 | 0.0% |
| full MLX 4bit（Alkd 生產版） | 21/30 | 70.0% |
| full MLX 4bit（自轉） | 21/30 | 70.0% |
| full MLX 8bit（自轉） | 19/30 | 63.3% |

**明確回答第一個問題：不會，mini 量化後同樣會出現 PUA，不是好消息。** BF16 原生
一樣乾淨（0%），但量化之後 mini 4bit 仍有 36.7% 的句子含 PUA，mini 8bit 更高達
63.3%（跟 full 8bit 幾乎同一個數字）。這進一步支持既有報告的結論：**PUA 是這個
模型家族（Qwen3-ASR 架構 + 這批 fine-tune）對 MLX affine 量化本身敏感造成的，
跟模型大小無關**——mini 只是把 4bit 這個特定位元寬的傷害程度降低了一些
（36.7% 對 70.0%，接近腰斬），但沒有消除，8bit 甚至跟 full 一樣糟。

**意外的細節、如實記錄不做過度推論**：mini 8bit（63.3%）比 mini 4bit（36.7%）PUA
比例更高，跟 full 系列「8bit 略低於 4bit」（63.3% vs 70.0%）方向相反。樣本數只有
30 筆，這個「4bit 反而比 8bit 乾淨」的現象有可能是取樣雜訊，也有可能是不同模型
尺寸對量化雜訊的敏感位置不同；本次沒有把樣本數放大到能區分這兩種可能，這是
**推論邊界**，不下定論。

## 結果二：MER（準確率）——實測驗證使用者說法，方向正確但用詞可以更準確

| 模型 | 正規化後 MER | raw MER（含PUA未過濾） |
|---|---:|---:|
| full BF16 | 4.82% | 10.00% |
| full MLX 4bit（Alkd 生產版） | 4.82% | 34.35% |
| full MLX 4bit（自轉） | 4.82% | 34.35% |
| full MLX 8bit（自轉） | 4.82% | 24.35% |
| mini BF16 | **3.95%** | 6.52% |
| mini MLX 4bit（自轉） | **3.51%** | 21.74% |
| mini MLX 8bit（自轉） | **3.95%** | 20.43% |

（正規化規則跟 `docs/benchmarks/p0-quality-report.md` 完全一致：NFKC、英文轉小寫、
移除標點/空白/私用區等 Unicode 類別 P/Z/C 的字元、繁簡不轉換。移除私用區這件事是
`normalize()` 既有規則的副作用，不是這次新加的——這也解釋了為什麼 full 系列四個
形態的正規化 MER **完全相同**（4.82%）：量化只在正確文字之間插入 PUA 雜訊 token，
不改變其餘文字內容，而 PUA 剛好落在被正規化規則濾掉的 Unicode 類別裡，所以四種
形態的「乾淨內容」讀起來一樣準。）

**明確回答第二個問題：使用者的說法方向正確，但實測顯示 mini 沒有變差，甚至在這批
30 筆語料上全面優於 full（3.51%～3.95% 對 4.82%）。**「跟原生差不多」低估了實測結果——
不是「差不多但略差」，而是「至少不比 full 差，這次量到的 30 筆甚至更好」。

**必須說明的推論邊界，不要照單全收**：
1. 樣本數只有 30 筆、單一語料來源（`adi-gov-tw` 朗讀型台灣華語語料），沒有涵蓋
   docs/05 要求的極短詞、靜音、長句停頓、會議場景、中英混用（這批語料英文參考
   token 數為 0，WER 完全沒測到）。full 版本的 200 筆基準測出 4.92%，跟這次 30 筆
   測出的 4.82% 落在同一量級，說明 full 這邊 30 筆不算離群，但 mini 沒有 200 筆
   規模的驗證，**如果要正式採信「mini 至少不輸 full」，應該先把 mini 也擴大到
   200 筆以上、且換一個語料來源做交叉驗證**，本次沒有做。
2. mini 的 raw MER（21.74%／20.43%，量化後）比 mini BF16（6.52%）差很多，這個
   落差幾乎全部來自 PUA（跟 full 系列的模式一樣），也就是說**如果不先解決 PUA
   過濾，mini 量化後直接顯示給使用者的原始文字，品質觀感會比正規化 MER 顯示的
   差很多**——這跟 full 系列的既有結論（PUA 過濾是止血必要手段）完全一致，
   mini 沒有讓這個問題變得比較不急迫。

## 結果三：大小、記憶體、延遲

**大小**（`du -sh` / 檔案大小實測）：

| 模型 | 大小 |
|---|---:|
| full BF16 | 3.8 GiB |
| full MLX 4bit（Alkd 生產版） | 1.2 GiB |
| mini BF16 | 1.5 GiB |
| mini MLX 4bit（自轉） | 0.68 GiB |
| mini MLX 8bit（自轉） | 0.96 GiB |

**Peak RSS**（`/usr/bin/time -l`，載入 + 連續推論的 maximum resident set size）：

| 模型 | Peak RSS |
|---|---:|
| full BF16 | 12.53 GiB |
| full MLX 4bit（Alkd 生產版） | 1.56 GiB |
| mini BF16 | **5.01 GiB** |
| mini MLX 4bit（自轉） | **0.89 GiB** |
| mini MLX 8bit（自轉） | **1.16 GiB** |

**平均每筆推論延遲**（同一批 30 筆語料的均值；mini 三形態另用 5 筆
`mem_probe.py` 交叉驗證過，數字穩定，見上方「方法」一節）：

| 模型 | 平均延遲 |
|---|---:|
| full BF16 | 0.583s |
| full MLX 4bit（Alkd 生產版） | 0.129s |
| mini BF16 | **0.336s** |
| mini MLX 4bit（自轉） | **0.076s** |
| mini MLX 8bit（自轉） | **0.078s** |

mini MLX 4bit 相對於目前生產用的 full MLX 4bit：**體積少 43%（0.68 對 1.2 GiB）、
peak RSS 少 43%（0.89 對 1.56 GiB）、延遲少 41%（0.076s 對 0.129s）**，而正規化
MER 還更低（3.51% 對 4.82%）——單看這四個維度，mini 4bit 全面優於目前生產模型。
**代價只有一個，而且是本報告從第一節就講清楚的：PUA 比例（36.7%）雖然比生產版低，
但仍然存在，不是零。**

## 建議：生產該用哪個模型？

**不建議現在就把生產模型換成 mini。** 理由不是效能或準確率——這兩項 mini 4bit
全面優於現行生產模型，這點證據很扎實。理由是：

1. **驗證深度不夠**：目前只有這一批 30 筆單一語料的實測，跟現行生產模型已經
   累積的 200 筆基準（`p0-quality-report.md`）不是同一個量級的驗證強度。在沒有
   把 mini 也擴大驗證到相近規模、涵蓋 docs/05 要求的場景之前，「mini 更好」
   只能算是**有力的初步訊號**，不能當作生產決策的唯一依據。
2. **PUA 過濾這個前置條件還沒做**：不管換不換 mini，`src/tea_asr/api/stream.py`
   的 PUA 後處理仍然停在「偵測後回 warning、保留原文」，這是既有報告已經指出
   的獨立待辦，換模型不會讓這件事變得沒必要——mini 4bit 一樣有 36.7% 的 PUA。

**如果之後要正式評估換成 mini**，優先順序建議是：先把 PUA 過濾做成預設開啟
（沿用既有報告的建議），再拿 mini 4bit 補到 200 筆以上、多語料來源的驗證，
兩件事都做完之後再談要不要正式切換 `src/tea_asr/model_spec.py`。

## 建議：雙模型並用值不值得做？

**技術上可行，代價可控（在目前這台機器上），但需要動到的地方不小，不是設定
檔改一改就能上。以下是依據現有程式碼結構給的具體判斷，不是含糊建議。**

### 現有架構的關鍵限制

讀了 `src/tea_asr/scheduler.py` 和 `src/tea_asr/worker/supervisor.py`：

- `WorkerSupervisor`（`worker/supervisor.py:39`）管理**一個**子行程，用
  `python -m tea_asr.worker.entry --model-path <path>` 啟動，這個子行程常駐、
  載入一個模型後一直服務到被重啟。`--model-path` 本身已經是參數化的，換模型
  不需要改 `entry.py`。
- `Scheduler`（`scheduler.py:31`）建構時只接受**一個** `worker: InferenceWorker`
  （`scheduler.py:39`），內部只有一份等待佇列、一份 `_running`/`_running_samples`
  狀態。`kind`（`interactive`/`realtime`/`preview`，`scheduler.py:16`）決定的是
  **同一個 worker 內**的優先權排序，不是要送去哪個模型。
- `src/tea_asr/api/app.py` 在啟動時只建立**一組** `WorkerSupervisor` + `Scheduler`
  （`app.py:121-122`），整個 app 共用這一個 scheduler 實例。
- `src/tea_asr/api/stream.py` 裡，同一個 `StreamSession` 用同一個
  `self._scheduler` 打兩種請求：`kind="preview"`（`stream.py:541`，即時預覽用
  的快照辨識）跟 `kind="interactive"/"realtime"`（`stream.py:346`，最終定稿）。
  也就是說「partial 用 mini、final 用 full」這個分流，目前的程式碼結構完全沒有
  對應的分岔點——preview 跟 final 現在打的是同一個模型。

### 要做到「mini 跑 partial、full 跑 final」需要動這些地方

1. **`src/tea_asr/model_spec.py`**：新增第二個 `ModelSpec`（例如 mini 的 MLX
   4bit）。這裡有個實務缺口：mini 目前**沒有**任何人發布過 MLX 量化版本，
   `ModelSpec` + `model_manager.locate_prepared_model` 的現有機制假設「HF repo id
   + revision，下載到本地」，不支援「BF16 下載回來後本地即時轉換」。要嘛
   （a）自己把轉好的 mini MLX 4bit 發布成一個可下載的 HF repo（跟 `Alkd` 現在
   做的事一樣），要嘛（b）在 `model_manager` 裡新增「本地轉換」的安裝路徑，
   讓生產環境的安裝流程多一個 `mlx_audio.convert` 步驟——後者會讓 `mlx_lm`
   從「跑起來才用得到」變成「安裝時的硬依賴」，且要處理轉換失敗、轉換耗時
   （本次兩次轉換各約 10-20 秒）等新的失敗模式。兩條路都不是簡單加一行設定。
2. **啟動流程（`app.py` 目前建立 scheduler 的那段）**：從「一組
   `WorkerSupervisor`+`Scheduler`」改成「兩組」，例如
   `interactive_scheduler`（指向 full 模型）跟 `preview_scheduler`（指向 mini
   模型），各自獨立的子行程、獨立的佇列上限設定。
3. **`Scheduler` 本身不必大改**：它已經是「一個 scheduler 對一個 worker」的
   設計，直接**建兩個 `Scheduler` 實例**即可，不需要改 `Scheduler` 類別內部邏輯
   （不需要重構出多路由佇列）。這是這個方向相對省力的地方。
4. **`StreamSession`（`stream.py`）**：建構子（`stream.py:158` 附近）現在只收
   一個 `scheduler: StreamScheduler`，要改成收兩個（或一個 dict），並把
   `stream.py:541`（preview 呼叫）指到 mini scheduler、`stream.py:343-346`
   （final 呼叫）指到 full scheduler。**這是本任務範圍內不能動的
   server 執行時程式碼**，這裡只指出「要動哪裡」，沒有實作。
5. **健康檢查/readiness**：目前 readiness 檢查（`app.py:160` 附近，送一段靜音
   PCM 探測 worker）只探測一個 worker；兩個模型都要各自探測，readiness 的語意
   要重新定義（兩個都 ready 才算 ready？還是允許 mini 掛掉時 degrade 成只有
   final、沒有 partial？這是產品決策，不是純技術問題）。
6. **`tests/conftest.py`**：目前的測試 fixture 假設單一 worker/scheduler
   （這也是為什麼這次 `git status` 顯示 `tests/conftest.py` 有未提交改動——
   雖然不確定內容，但雙模型架構下這類 fixture 大概率要跟著擴充成雙份）。

### 代價：記憶體要同時容納兩個模型嗎？切換成本？

**記憶體：要，兩個模型都得常駐，這是唯一站得住腳的設計。** 理由：`WorkerSupervisor.start()`
啟動一個子行程並等它把模型完全載入（`load_timeout_s=120`，`supervisor.py:41`）才回
ready；不常駐、現用現載的「熱插拔」方案，每次切換都要付一次完整的模型載入時間。
用本次實測的數字換算：

- 若兩個模型都常駀（mini 4bit + 目前生產的 full 4bit）：
  peak RSS 約 `0.89 + 1.56 ≈ 2.45 GiB`。這台機器有 **48 GiB** 實體記憶體，
  2.45 GiB 是零頭，**完全負擔得起**，不需要犧牲什麼。
- 若改成「熱插拔」（同一個 worker 行程按需重載模型）：每次從 mini 切到 full
  或反過來，都要付一次 `WorkerSupervisor.start()` 的完整冷啟動時間——用
  mem_probe 量到的 `load_s` 量級（mini 系列約 0.8～1.2 秒；full 4bit 沒有在
  本次或既有報告裡明確量過 `load_s`，但同量級或更長，因為模型檔案更大）。
  對一個「即時 partial + 最終定稿」的互動式語音場景，這個切換頻率可能是
  每個語音片段一次甚至更高，**每次都要停頓 1 秒以上重新載入模型是不可接受
  的延遲**，這條路在互動場景下不成立。
- 結論：**兩個模型常駐是唯一合理的設計**，而且在這台機器的記憶體規模下，
  常駐的代價（2.45 GiB）小到可以忽略——真正的成本不在記憶體，在上面列的
  五項架構改動（尤其是 mini 目前沒有現成 MLX 版本可下載這件事）。

### 值不值得做？

**值得考慮，但優先權應該排在「先把 PUA 過濾做成預設開啟」之後，不是現在馬上做。**
依據：

1. 這次的準確率與延遲數字顯示 mini 4bit 本身就是個全面優於現行生產模型的候選
   （見上一節），如果 mini 要用在 partial 顯示，好處是明確的：延遲只要
   full 版本的六成不到（0.076s 對 0.129s），使用者感知的即時性會更好，而
   partial 結果本來就會被 final 覆蓋，PUA 或些微準確率落差在 partial 階段的
   容忍度也比 final 高。
2. 但這個方向的**啟動成本**（第 1 項：需要一份可下載的 mini MLX checkpoint，
   或是在 model_manager 加本地轉換能力）目前完全沒有著落，不是這次改幾行
   `model_spec.py` 就能解決，需要先決定要不要維護一份自己發布的量化模型
   （版本管理、之後模型更新誰負責重新轉換量化並驗證 PUA 比例，都是新增的
   維運責任）。
3. 記憶體代價在這台機器上可以忽略，**不是這個決策的限制因素**；真正要衡量的
   是「多維護一個模型 checkpoint + 多一套雙路由架構的複雜度」，值不值得換來
   「partial 延遲降低 + 可能的整體準確率提升」。這是產品/工程資源分配的判斷，
   本報告只給出技術上「做得到、成本落在哪裡」的依據，不代替做這個決定。

## 復現腳本

沿用既有腳本、加上必要的參數化，全部在 `benchmarks/`：

```bash
# 下載 mini BF16（本機 models/，不進 ~/Library/Caches）
uv run python -c "
from huggingface_hub import snapshot_download
snapshot_download('JacobLinCool/TEA-ASR-1.1-mini',
    revision='98c58048572b44839dfcfa60de3ad7e365a5b232', cache_dir='models')
"

# 自轉 4bit / 8bit（convert_quant.py 目前只認 full 的 UPSTREAM_BF16 常數，
# 本次是用一支等價的一次性腳本改指向 mini 快照跑的，內容跟 convert_quant.py
# 完全相同、只換了 hf_path 來源——沒有另外收錄成正式腳本，因為 convert_quant.py
# 目前是 full 專用的單一常數寫死；如果之後要常態化 mini 轉換，建議把
# UPSTREAM_BF16 也改成可傳參數）

# PUA 統計（MLX 兩形態，沿用既有腳本原樣）
uv run python benchmarks/pua_ab_local_pass.py --model-path models/mlx-4bit-mini \
    --pass-name mlx-4bit-mini --limit 30 --out benchmarks/results/pua_ab_4bitmini_30.json
uv run python benchmarks/pua_ab_local_pass.py --model-path models/mlx-8bit-mini \
    --pass-name mlx-8bit-mini --limit 30 --out benchmarks/results/pua_ab_8bitmini_30.json

# PUA 統計（BF16，新增 --repo/--revision/--pass-name，預設值不變）
uv venv /tmp/bf16-env --python 3.12
uv pip install --python /tmp/bf16-env/bin/python torch transformers accelerate \
    soundfile librosa huggingface_hub qwen-asr
/tmp/bf16-env/bin/python benchmarks/pua_ab_bf16_pass.py --limit 30 \
    --repo JacobLinCool/TEA-ASR-1.1-mini \
    --revision 98c58048572b44839dfcfa60de3ad7e365a5b232 \
    --pass-name bf16-mini --out benchmarks/results/pua_ab_bf16_mini_30.json

# MER（MLX 形態，quality_eval.py 新增 --model-path/--model-label，預設值不變）
uv run python benchmarks/quality_eval.py --limit 30 \
    --model-path models/mlx-4bit-mini --model-label mini-mlx-4bit-selfconv \
    --out benchmarks/results/quality_mini4bit_30.json

# MER（BF16 形態，新腳本 quality_eval_bf16.py，統計邏輯 import 自 quality_eval.py）
/tmp/bf16-env/bin/python benchmarks/quality_eval_bf16.py --limit 30 \
    --repo JacobLinCool/TEA-ASR-1.1-mini \
    --revision 98c58048572b44839dfcfa60de3ad7e365a5b232 \
    --model-label mini-bf16 --out benchmarks/results/quality_mini_bf16_30.json

# 記憶體與延遲
/usr/bin/time -l uv run python benchmarks/mem_probe.py --model-path models/mlx-4bit-mini --repeat 5
/usr/bin/time -l uv run python benchmarks/mem_probe.py --model-path models/mlx-8bit-mini --repeat 5
/usr/bin/time -l /tmp/bf16-env/bin/python benchmarks/pua_ab_bf16_pass.py --limit 5 \
    --repo JacobLinCool/TEA-ASR-1.1-mini \
    --revision 98c58048572b44839dfcfa60de3ad7e365a5b232 --pass-name bf16-mini-memprobe \
    --out benchmarks/results/pua_ab_bf16_mini_memprobe.json
```

模型檔案在 `models/models--JacobLinCool--TEA-ASR-1.1-mini/`、`models/mlx-4bit-mini/`、
`models/mlx-8bit-mini/`（都在 `.gitignore` 涵蓋範圍內）。`benchmarks/results/*.json`
本機 ignored，逐句明細（含每句的 hypothesis 全文與 PUA 碼位清單）都在裡面。

## 尚未做的事（推論邊界之外，明確列出待補）

- 沒有把 mini 擴大到 200 筆規模驗證，也沒有換第二個語料來源交叉驗證——目前
  「mini 不輸 full」的結論建立在同一批 30 筆語料上，是初步訊號、不是定論。
- 沒有測 mini 的中英混用、極短詞、靜音、長句停頓、會議場景（docs/05 要求的
  場景，這次語料同樣不涵蓋，跟既有 full 版本報告的限制一致）。
- 沒有幫 mini 發布一份可下載的 MLX checkpoint，也沒有評估在 `model_manager`
  加「本地即時轉換」路徑的實作細節（雙模型並用一節已指出這是啟動成本，
  但沒有給出實作）。
- `Scheduler`/`StreamSession` 的雙路由改動只有架構分析，沒有寫程式碼原型
  （本次任務範圍明確排除修改 server 執行時程式碼）。
