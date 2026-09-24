# T3PO（Confucius4-T3PO）同步翻譯接入評估

執行日期：2026-09-24。狀態：**兩關都過，已接入為 opt-in 翻譯 provider（預設關閉）**。
只宣告 **zh→en**；品質只有 20 句質性抽樣，**沒有 BLEU／COMET 等量化結論**（repo 內沒有平行語料）。

## 結論

| 問題 | 答案 |
|---|---|
| MLX 載得起來嗎？ | **能**。mlx-lm 原生 `qwen2`，轉 4bit 後 7.77 GiB，冷載入 6.4 秒（外接 SSD），熱載入約 1.2 秒 |
| 翻得出合理英文嗎？ | 20 句抽樣看起來大多合理（見下表，請用眼睛判斷），**這不是量化品質結論** |
| en→zh？ | 能跑，但 20 句回譯有 18 句含簡體字（OpenCC `s2tw` 會改變），用語也是大陸用語 → **不提供** |
| 記憶體？ | MLX 峰值 8.1–8.5 GiB、進程 RSS 8.1 GiB；ASR 峰值 1.47 GiB。48 GB 機器上兩者並存約 10 GiB |
| KV cache 真的能跨步驟重用嗎？ | **能，實證**：每步平均只 prefill 13 個 token（prompt 478），每次呼叫 0.26 s vs 從頭算 3.50 s（13.5 倍），194 步中 193 步輸出逐字相同 |
| 但官方的 history 滑動窗口會破壞重用 | 官方每提交一段就把窗口滑一對，history 開頭每步都變。實測重用率掉到 84%、呼叫 p95 2.7 s。改成「超過 30 對才一次砍到剩 10 對」後 p95 0.53 s |
| 跟得上即時嗎？（ASR 同時在跑） | **跟得上**。三檔位翻譯 worker 使用率 0.35–0.38（語速 2.7 字/秒），壓縮到 5.0 字/秒時 0.52；佇列最多積 2–5 個到達 |
| ASR 被拖慢多少？ | **final RTF 0.037 → 0.082（約 2.2 倍）**，final 延遲 p95 0.22 → 0.49 s。仍遠低於即時（RTF ≪ 1），但使用者會多等約 0.2–0.3 s |
| append-only？ | 成立。所有重播 0 次違反（已提交譯文永遠是下一版的前綴）；協定本身沒有撤回通道 |

## 設定

- 機器：MacBook Pro、Apple M4 Pro、48 GB、macOS 27.0。mlx 0.32.2、mlx-lm 0.31.3、mlx-audio 0.4.5、transformers 5.12.1
  （全部是 `uv.lock` 既有版本；mlx-lm 是 mlx-audio 的傳遞依賴，**沒有改 `pyproject.toml`**）。
- 模型：`netease-youdao/Confucius4-T3PO` revision `446e5dcca080740f2c2dc9d06a91ed66a9920410`（Apache-2.0，base Qwen2.5-14B），
  bf16 8 個 shard 共 29.5 GB，逐一核對 sha256 後轉換。
- 量化：`python -m mlx_lm convert -q --q-bits 4 --q-group-size 64 --q-mode affine`（全部線性層與 embedding 4bit、
  group 64、affine；mlx-lm 回報 4.501 bits/weight）。轉換 38 秒。輸出 8,142,716 KiB（7.77 GiB，2 個 shard），sha256 在 `models.lock.json` 的 `translation.local_build`。
- 位置：模型只放外接 SSD `/Volumes/DigiFusion/tea-asr-models/t3po-mlx-4bit`。bf16 確認 4bit 可用（production worker 實際翻譯成功）後已移到該磁碟的垃圾桶。
- 協定：照官方 repo `netease-youdao/Confucius4-T3PO` commit `4827f02b7b344d9ab5af95ad484fd64b6682cb85` 的
  `inference/prompts.py`（長版任務提示，不是模型卡的短版）、`translation.py`、`latency.py` 移植，沒有引入 vLLM／PyTorch／CUDA。
  三個檔位＝對兩個停止 token（151643、151645）加 logit bias `-tau*scale`（tau：low 0.9375、native 0、high −0.39），
  非 native 再加 vLLM 語意的 repetition penalty 1.05；force＝第一個 token 禁止停止 token（等於 vLLM `min_tokens=1`）。greedy。
- ASR：`Alkd/TEA-ASR-1.1-MLX-4bit` revision `caee57a908b6d64be08a6462c7a21ececbd4d7cb`（生產模型，走 `TeaMlxBackend`）。
- 語料：Taiwan-Tongues zh-TW test shard（`adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw` revision `ff0e8047…`，與 R2T2 報告同一份）。
  前 200 句先用生產 ASR 跑一次（CER 4.92%，與 P0／R2T2 報告一致），拿**真實辨識文字**（PUA 已過濾）與每句音訊長度當翻譯的輸入節奏。

## 關卡 1：載入、吞吐、記憶體、質性抽樣

| 指標 | 值 |
|---|---:|
| 轉換後大小 | 7.77 GiB |
| 載入時間（冷，外接 SSD） | 6.38 s |
| 載入時間（熱，page cache） | 1.1–1.3 s |
| prefill（冷 cache、668 token prompt） | 227 tokens/s |
| decode（單獨跑） | 25.4 tokens/s |
| decode（串流重播中，~700 token context） | 21–22 tokens/s |
| MLX active（載入後）／peak | 7.74 ／ 8.30 GiB |
| 進程 max RSS | 8.13 GiB |

### 平行語料

repo 內**沒有**任何中英平行語料（Taiwan-Tongues 只有中文轉錄）。要算 BLEU／COMET 需要使用者同意下載，例如：

- **FLORES+ devtest**（`openlanguagedata/flores_plus`，Hugging Face，gated、CC BY-SA 4.0）：`cmn_Hant`（繁中）↔ `eng_Latn` 各 1012 句，文字檔約數百 KB。這是最小、最直接的一份。
- 分數工具：`sacrebleu`（BLEU／chrF，純 Python，小）；COMET 要 `Unbabel/wmt22-comet-da`（約 2.3 GB）且依賴 PyTorch（CPU 即可），只能放在獨立環境，不進專案依賴。
- 若要評「同步」延遲指標（AL／LAAL），還需要有語音時間軸的平行語料（例如 BSTC，需申請），這輪不建議。

### 20 句質性抽樣（zh→en）

取前 200 句的第 0、10、…、190 句（語料的**人工轉錄**文字，不是 ASR 輸出）。每句一個全新 session，串流欄是照協定每次送 2 個字、句尾 flush（native）；
整句欄是一次 force 翻譯。**請用眼睛判斷，這不是品質評分。**

| # | 原文 | 串流 zh→en | 整句 zh→en |
|---:|---|---|---|
| 1 | 遲遲未定的原因 | The reason for the delay | The reason for the delay |
| 2 | 極度瞧不起他 | I really look down on him | I really look down on him |
| 3 | 有朝一日 | One day | One day |
| 4 | 都行，沒差 | It's fine, no difference. | It's fine, no difference. |
| 5 | 用來蒐集意見 | to collect opinions | to collect opinions |
| 6 | 三零七是台北公車之王 | 307 is the king of Taipei buses | 307 is the king of Taipei buses |
| 7 | 我能反殺 | I can kill back | I can kill back |
| 8 | 有很多人突然不確定時間 | Many people suddenly aren't sure about the time | Many people suddenly aren't sure about the time |
| 9 | 轉帳再提領出來 | Transfer the money and then withdraw it. | Transfer the money and then withdraw it. |
| 10 | 只要稍微注意新聞報導 | As long as you pay a little attention to the news reports | As long as you pay a little attention to the news reports |
| 11 | 下雨地上有水 | It's raining and the ground is wet. | It's raining and the ground is wet. |
| 12 | 我問過律師跟法律人 | I've asked lawyers and legal professionals | I've asked lawyers and legal professionals |
| 13 | 有時候也是一種方式 | Sometimes waiting can also be a way | Sometimes it's also a way |
| 14 | 至契約期限屆滿 | Upon the expiration of the contract term | Upon the expiration of the contract term |
| 15 | 提供舞台給研發團隊展現成果 | Provide a platform for the R&D team to showcase their achievements | Provide a platform for the R&D team to showcase their achievements |
| 16 | 讓社區民眾住的安心 | to ensure that community residents live with peace of mind | to ensure that community residents live with peace of mind |
| 17 | 愚民就是被收割 | The ignorant are being harvested | The ignorant are being harvested |
| 18 | 然後利用深度學習演算法預測 | and then use deep learning algorithms to predict | Then, using deep learning algorithms to predict |
| 19 | 看著外婆慈祥的笑容 | Looking at my grandmother's kind smile | Looking at my grandmother's kind smile |
| 20 | 來採集臺灣的少數語言 | to collect Taiwan's minority languages | to collect Taiwan's minority languages |

觀察（不是結論）：第 13 句串流版多出原文沒有的 "waiting"——它在只看到「有時」時就先提交了一段，後面為了接得通順補了字。
這正是 append-only 同步翻譯的代價：早提交的錯不能撤回。第 7、17 句是字面直譯。第 13、18 句以外，串流與整句譯文逐字相同（第 18 句只差句首與分詞形式）。

**en→zh 回譯**（把上面英文譯文當原文，每次 2 個詞）：20 句中 18 句經 OpenCC `s2tw` 會改變（例：`延迟的原因`、`307路是台北公交车的王者`），
也就是輸出簡體與大陸用語。所以 capabilities **只宣告 zh2en**。

**通過判定**：能載入、能產出看起來合理的英文、峰值 8.3 GiB＋ASR 1.5 GiB ≈ 10 GiB，48 GB 機器有大量餘裕 → **關卡 1 通過**。

## 關卡 2：串流迴圈、KV 重用、即時性、GPU 爭用

### 輸入節奏

前 100 句（345 秒音訊、924 字，平均 2.7 字/秒；朗讀語料、句與句無上下文）首尾相接成一條時間軸。兩種節奏：

- **increments**：每句 ASR 文字切成約每 0.5 秒一塊，在該句音訊時間內均勻到達，句尾 flush（force）。
  模擬官方設計的「串流 ASR 增量＋句尾 reset」；上游延遲當 0（樂觀）。
- **finals**：整句在「句尾＋實測 ASR 推論時間」一次到達並 force。**這才是本服務能給的上游**：只有 `transcript.final`
  不可變，partial 會被修訂，餵給 append-only 翻譯會產生撤不回的錯譯。

翻譯端是單一 worker：閒下來時把已到達的增量合併成一次呼叫（佇列不會無限堆），句尾直接併入 buffer 後 force，不先多做一次 probe。
量測欄位：決策延遲＝到第一個 token（READ/WRITE 已決定）的時間；呼叫延遲＝整個呼叫；句譯延遲＝句尾（音訊結束）到該句全部譯文提交。

### KV cache 重用（實證）

`benchmarks/t3po_kv_check.py`：前 40 句、每 2 字一步、句尾 flush，不合併；同一串輸入跑三遍（native）。

| 設定 | 呼叫數 | 平均 prompt | 平均 prefill | 重用率 | 呼叫 mean／p95 | 總時間 |
|---|---:|---:|---:|---:|---:|---:|
| 重用（窗口 30、超過砍到 10） | 194 | 478 | 12.9 | 97.3% | 0.258／0.561 s | 50 s |
| 每步從頭 prefill | 194 | 478 | 478 | 0% | 3.495／4.981 s | 678 s |
| 重用＋官方逐對滑動窗口 | 194 | 542 | 33.8 | 93.8% | 0.420／1.897 s | 81 s |

- 正確性：重用與從頭算 194 步中 **193 步的 READ/WRITE 與譯文逐字相同**；唯一差異在第 79 步（`father` vs `dad`），
  是不同 prefill 形狀下的浮點差異讓 greedy 選了另一個近義詞，不是 cache 錯位（之後又收斂回同樣的譯文）。
- 模型卡「interleaved history 可重用 KV cache」**在前綴層級成立**：WAIT 時只有 `<CURRENT_INPUT>` 尾巴變，TRANS 時 history 只在尾端 append。
  但官方 `history_window=30` 的逐對滑動會讓窗口滿了之後每次提交都改到 history 開頭，重用率下降、p95 變成 3.4 倍。
  接入版改成超過 30 對才一次砍到剩 10 對（`src/tea_asr/translation/simt.py`），這是**偏離官方參數**的地方，品質影響未評估。

### 單獨跑（虛擬時鐘，服務時間是真實量到的）

| 檔位 | 節奏 | 語速 | 呼叫（WAIT/TRANS） | 決策 mean／p95 (s) | 呼叫 mean／p95 (s) | 句譯延遲 mean／p95 (s) | 到達 字/s | 消化 字/忙碌秒 | 使用率 | 重用率 | 違反 append-only |
|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|
| native | increments | 1× | 693 (586/107) | 0.160／0.251 | 0.209／0.533 | 0.46／0.67 | 2.67 | 6.37 | 0.42 | 0.980 | 0 |
| low | increments | 1× | 697 (474/223) | 0.157／0.254 | 0.211／0.458 | 0.36／0.58 | 2.67 | 6.28 | 0.43 | 0.970 | 0 |
| high | increments | 1× | 696 (587/109) | 0.163／0.264 | 0.212／0.524 | 0.47／0.69 | 2.67 | 6.25 | 0.43 | 0.980 | 0 |
| native | finals | 1× | 100 (0/100) | 0.464／1.496 | 0.808／1.822 | 0.92／1.91 | 2.67 | 11.44 | 0.23 | 0.922 | 0 |
| low | finals | 1× | 100 (0/100) | 0.447／1.324 | 0.788／1.730 | 0.90／1.82 | 2.67 | 11.72 | 0.23 | 0.922 | 0 |
| high | finals | 1× | 100 (0/100) | 0.448／1.259 | 0.789／1.653 | 0.90／1.83 | 2.67 | 11.71 | 0.23 | 0.922 | 0 |
| native | increments | 1.8× | 418 (311/107) | 0.198／0.376 | 0.278／0.620 | 0.51／0.71 | 4.82 | 7.96 | 0.61 | 0.973 | 0 |
| native（官方逐對滑動窗口） | increments | 1× | 415 (310/105) | 0.529／2.621 | 0.609／2.697 | 1.07／3.24 | 2.67 | 3.66 | 0.73 | 0.838 | 0 |

- 峰值 MLX 記憶體每一組都在 8.13–8.48 GiB。
- `low` 檔明顯比較早提交（TRANS 223 次 vs 107），句譯延遲低 0.1 s；`high` 與 `native` 幾乎一樣。
- **finals 節奏下三檔幾乎沒差**：每次都是 force，檔位的 bias 只影響句中何時停，對「整句一次翻」沒什麼作用。
  這表示接入後（只餵 final）官方的 READ/WRITE 延遲優勢基本用不到；要用到得先有不可變的串流 ASR 增量。

### GPU 爭用：ASR 與翻譯同時跑（牆鐘）

兩個獨立程序共用同一顆 GPU，同一時刻起跑、跑同一條 345 秒時間軸：
ASR 程序（`benchmarks/t3po_asr_load.py`）用生產 `TeaMlxBackend` 即時重播**音訊**，句尾做 final，句中每 0.8 秒做一次 preview
（模仿 continuous＋revisable session；落後時只跑最新 preview）；翻譯程序（`benchmarks/t3po_replay.py --clock wall`）重播 ASR 文字。

**ASR 端**（100 個 final、約 380 個 preview）：

| 條件 | final RTF mean／p95 | final 延遲 mean／p95／max (s) | preview RTF mean／p95 |
|---|---:|---:|---:|
| ASR 單獨 | 0.037／0.051 | 0.135／0.217／0.253 | 0.063／0.105 |
| ＋翻譯 native increments | **0.082／0.115** | **0.297／0.489**／0.575 | 0.074／0.193 |
| ＋翻譯 low increments | 0.082／0.116 | 0.296／0.457／0.685 | 0.073／0.151 |
| ＋翻譯 high increments | 0.082／0.113 | 0.298／0.502／0.645 | 0.073／0.253 |
| ＋翻譯 native finals（接入後的實際路徑） | **0.045／0.096** | **0.168／0.454**／0.944 | 0.066／0.147 |
| ＋翻譯 native increments 1.8× 語速 | 0.058／0.107 | 0.225／0.497／0.730 | 0.095／0.212 |

**翻譯端**：

| 條件 | 呼叫數 | 決策 mean／p95 (s) | 呼叫 mean／p95／max (s) | 句譯延遲 mean／p95／max (s) | 使用率 | 最大積壓 | decode tok/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| native increments 單獨（牆鐘） | 692 | 0.120／0.178 | 0.169／0.489／3.36 | 0.45／0.64／2.57 | 0.34 | 8 | 21.5 |
| native increments ＋ASR | 699 | 0.117／0.164 | 0.170／0.533／1.35 | 0.49／0.71／1.17 | 0.35 | 2 | 19.2 |
| low increments ＋ASR | 701 | 0.127／0.192 | 0.188／0.469／1.23 | 0.38／0.64／0.81 | 0.38 | 2 | 16.8 |
| high increments ＋ASR | 700 | 0.117／0.162 | 0.171／0.537／1.36 | 0.49／0.74／1.34 | 0.35 | 2 | 19.1 |
| native finals ＋ASR | 100 | 0.311／0.889 | 0.640／1.301／1.60 | 0.75／1.46／1.79 | 0.19 | 1 | 21.9 |
| native increments 1.8×（5.0 字/s）＋ASR | 730 | 0.149／0.293 | 0.236／0.566／1.96 | 0.49／0.73／2.27 | 0.52 | 5 | 20.0 |

解讀：

- **爭用是真的，主要傷在 ASR**：翻譯在跑時 ASR final RTF 變成約 2.2 倍（0.037→0.082），final 延遲 p95 多 0.27 s。
  原因是 14B 模型每次 decode 都要讀 7.7 GiB 權重，把記憶體頻寬吃掉。翻譯端自己只慢一點（decode 21.5→19.2 tok/s，句譯延遲 p95 +0.07 s）。
- 接入後的實際路徑（finals）翻譯呼叫少很多，ASR final RTF 只從 0.037 變 0.045，但 p95 0.096、max 延遲 0.94 s：
  翻譯剛好在跑一次較長的 force 時，ASR final 要等 GPU。
- **即時性**：所有並行組合翻譯使用率 ≤0.52、積壓 ≤5 個到達、句譯延遲 p95 ≤0.74 s（finals 1.46 s），沒有累積落後。
- **通過判定**：ASR 仍遠快於即時，final 延遲 p95 ≤0.50 s（P2 一小時 soak 的端到端 p95 是 0.57 s）；翻譯跟得上 2.7 與 5.0 字/秒 → **關卡 2 通過**。
  但「多等 0.2–0.3 s 會不會影響聽寫手感」是體感問題，**CLI 無法驗證，需要使用者實機確認**；翻譯預設關閉正是因為這個代價。

### append-only

- 每次 TRANS 後檢查「目前串起來的譯文」是否以上一版為前綴：全部 20 組重播 **0 次違反**。
- 結構上：協定只有 WAIT／TRANS 兩種回應，沒有撤回或修改已提交段的通道；接入版的 `translation.segment` 也只有新增、沒有修訂事件。
- 代價就是上面第 13 句那種「早提交的錯撤不回」。

## 接入後的形狀（摘要，契約見 [docs/04](../04-api.md) 「翻譯（opt-in）」）

- 獨立 worker 子程序 `python -m tea_asr.translation.worker`，自己的模型、佇列（每 session 16 段）、逾時（20 s，超過殺掉重啟）、記憶體上限（預設 12 GiB）。
- 預設關閉；`translation_enabled=true`＋`translation_model_path` 才啟用。模型路徑不存在（SSD 拔掉）時服務照常啟動、ASR 照常運作，
  provider 狀態 `failed` 並寫明原因，要求翻譯的 `session.start` 被拒（`translation_unavailable`，close 4503）。
- 只翻譯 `transcript.final`，譯文走新的 `translation.segment`／`translation.error` 事件，`source_segment_ids` 對回 final；既有事件一個欄位都沒改。
- 真模型煙霧測試：`TEA_ASR_TRANSLATION_MODEL_PATH=... uv run pytest -m hardware tests/hardware/test_real_translation.py`（通過；第二句重用 222/259 個 prompt token，峰值 8.13 GiB）。

## 限制與未驗證

- 品質：沒有平行語料，**沒有量化品質**。20 句抽樣是朗讀短句，不代表會議、口語、中英混用。
- 語料是互不相關的短句接成時間軸，history 對翻譯沒有真實的上下文價值；真實對話的 prompt 會比較長（仍受 30 對窗口限制）。
- increments 節奏假設上游 0 延遲、字元在音訊時間內均勻出現；本服務目前沒有這種上游。
- 「超過 30 對砍到 10 對」偏離官方逐對滑動，對品質的影響未評估。
- 4bit 量化可能改變官方以 bf16 校準的 tau 門檻；三檔位的相對行為有出現（low 提交較早），但絕對校準未驗證。
- 只測了單一語者、單一機器、一個翻譯 session；flat-out（ASR 與翻譯都滿載）情境沒測。

## 授權

T3PO 權重為 Apache-2.0。轉換成 4bit 屬衍生作品；本輪只在本機使用、沒有散佈，若要散佈須附 LICENSE 並註明修改。
提示字串移植自官方 repo（Apache-2.0），來源 commit 寫在 `src/tea_asr/translation/simt.py` 開頭。

## 重跑

```bash
M=/Volumes/DigiFusion/tea-asr-models
# 0. 下載（需使用者同意；bf16 29.5 GB）與轉換
HF_HOME=$M/hf-cache hf download netease-youdao/Confucius4-T3PO \
  --revision 446e5dcca080740f2c2dc9d06a91ed66a9920410 --local-dir $M/t3po-bf16
uv run python -m mlx_lm convert --hf-path $M/t3po-bf16 --mlx-path $M/t3po-mlx-4bit \
  -q --q-bits 4 --q-group-size 64 --q-mode affine
# 1. ASR 真實輸出（生產模型，前 200 句）
TEA_ASR_MODELS_DIR=<ASR models dir> uv run python benchmarks/quality_eval.py --limit 200 \
  --out benchmarks/results/t3po_asr_hyp_200.json
H=benchmarks/results/t3po_asr_hyp_200.json
# 2. 關卡 1
uv run python benchmarks/t3po_gate1.py --model $M/t3po-mlx-4bit --hyp $H --out benchmarks/results/t3po_gate1.json
# 3. 關卡 2：KV 重用、單獨重播（mode ∈ low/native/high，cadence ∈ increments/finals）
uv run python benchmarks/t3po_kv_check.py --model $M/t3po-mlx-4bit --hyp $H --limit 40 --out benchmarks/results/t3po_kv_check.json
uv run python benchmarks/t3po_replay.py --model $M/t3po-mlx-4bit --hyp $H --limit 100 --history-keep 10 \
  --mode native --cadence increments --out benchmarks/results/t3po_g2_native_inc_keep10.json
#    官方逐對滑動窗口：去掉 --history-keep；語速壓縮：--speed 1.8
# 4. 關卡 2：GPU 爭用（兩個程序同一時刻起跑）
S=$(python3 -c 'import time; print(time.time()+50)')
TEA_ASR_MODELS_DIR=<ASR models dir> uv run python benchmarks/t3po_asr_load.py --limit 100 --start-at $S \
  --out benchmarks/results/t3po_g2c_asr_with_native_inc.json &
uv run python benchmarks/t3po_replay.py --model $M/t3po-mlx-4bit --hyp $H --limit 100 --history-keep 10 \
  --clock wall --start-at $S --mode native --cadence increments \
  --out benchmarks/results/t3po_g2c_tr_native_inc_with_asr.json
wait
#    ASR 基準：只跑 t3po_asr_load.py；1.8× 組：翻譯端 --limit 180 --speed 1.8（時間軸同長）
# 5. en→zh 字形檢查（任何裝了 opencc-python-reimplemented 的 Python；不是專案依賴）
```

逐句明細 JSON 在本機 ignored 的 `benchmarks/results/`（`t3po_*`）。
