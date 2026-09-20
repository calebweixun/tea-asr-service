> **狀態（2026-09-20）**：本檔由規劃 agent 產出，未經實作驗證。
> 使用者已裁示：**GGUF 不做**，模型範圍收斂為在 `JacobLinCool/TEA-ASR-1.1-mini`
> 的原版與 MLX 版之間切換，走既有 `tea-asr model-prepare`，不引入第二套 runtime。
> 因此 **W4、W5 作廢，D1／D2／D3 作廢**。D4 已選定「選項 B：app 自洽的安全層」
> （完整 token lifecycle + rate limit + Host/Origin 驗證 + 連線數上限，含 TLS）。
> D5、D6 照建議採用 A。W1 與 W9 曾派工但因 agent 越界寫入被終止，尚未開始。

# tea-asr Mac client 四項功能實作方案

調查日期：2026-09-20

調查範圍：/Users/c2leb/Codes/tea-asr-service 的目前工作樹、HEAD、指定文件、codebase-memory graph、相關 Python/Swift 原始碼與 git history。

本文件是規劃，不是實作；調查期間沒有修改 repo 檔案，也沒有 commit 或 git 寫入。行號以本次調查時的工作樹為準；其他 agent 正在修改 clients/macos，後續派工時須重新讀取檔案與重跑 baseline。

## 調查基線

- 工作樹在調查開始前已經有其他 agent 的未提交變更，包含：
  - clients/macos/Sources/TeaASRClient/AudioCapture.swift
  - clients/macos/Sources/TeaASRClient/AudioDeviceSelection.swift
  - clients/macos/Tests/TeaASRClientTests/AudioDeviceSelectionTests.swift
  - 未追蹤的 .swift-exec-wrapper、AudioLevelBarView.swift、AudioLevelMonitor.swift、AudioLevelMonitorTests.swift
- 目前架構的真實基線是：Python 3.12 + uv、FastAPI/Uvicorn 單 worker、MLX worker、固定單一 ASR 模型；Mac app 透過獨立的 tea-asr serve 行程連線，不是把 server 嵌在 Swift app 內。依據 docs/03-architecture.md:9-11,51-75、clients/macos/Sources/TeaASRClient/ServiceControl.swift:3-7,66-106。
- 目前沒有重新跑測試或 build：這次是唯讀規劃，且 clients/macos 有並行修改。交付實作前，每個工作項都必須按 AGENTS.md:60-64 重跑相應層的 baseline；docs/10-session-handoff.md:15 的 143 個 Python 測試與 docs/06-handoff.md:130 的 65 個 Mac 測試是交接紀錄，不視為這個髒工作樹的現況數字。

## 1. 可行性結論

### 需求 3：GGUF / Hugging Face 模型選擇

**結論：現在可以做「顯示目前固定 MLX 模型」與「使用者明確觸發 tea-asr model-prepare」；真正的 Hugging Face 模型選擇、下載、切換需要先補 server 端能力；client 直接下載 GGUF 或自帶 GGUF runtime 被既有約束擋住，需要決策。**

目前 server runtime 是 mlx-audio==0.4.5，實際 backend 是 TeaMlxBackend，透過 mlx_audio.stt.utils.load_model 載入模型，見 pyproject.toml:12-21、src/tea_asr/backend.py:46-103。目前依賴沒有 GGUF/GGML runtime；docs/03-architecture.md:30 反而把「Rust＋GGUF C++ 引擎」列為「不在本期」。

「前面測試的 mini (gguf)」在本 repo 查不到 GGUF 證據。能找到的是 JacobLinCool/TEA-ASR-1.1-mini 的 BF16、self-converted MLX 4-bit、self-converted MLX 8-bit benchmark，見 docs/benchmarks/mini-model-report.md:1-5,17-43；報告明確說尚未切 production，見 docs/benchmarks/mini-model-report.md:157-172。git history 能找到 mini 的 benchmark commit 7974923，但沒有找到 GGUF 檔名、GGUF repo/revision 或 GGUF runtime。唯一的 GGUF 文字是架構文件把它列為本期之外的候選方向。

models.lock.json:1-30 目前是固定 ASR/VAD 的靜態 reproducibility manifest，含 ASR repo、revision、mlx_audio 版本、檔名與 hash；現有 ASR prepare code 沒有泛用地讀這個 lock，見 src/tea_asr/model_manager.py:10-29。tea-asr model-prepare 沒有 model 參數，見 src/tea_asr/cli.py:33-39,256-264；它準備目前固定的 MLX 模型與 VAD，不提供 catalog、切換、job id 或進度 API。

P5a 的原文只把限制直接施加在 GUI：**「模型與 runtime 準備由 server 明確擁有；Mac GUI 僅可呼叫、引導或診斷 tea-asr model-prepare，不得靜默下載模型或自行建立 ASR runtime。」**（docs/06-handoff.md:64,126-127）。所以「由 server 實作、由 GUI 明確呼叫」的第二 runtime 並不是被這一句對 client 的文字直接永久禁止；但目前的共同架構約束仍是「每機一個服務instance、一個MLX worker、一份模型」與「保留單模型」（docs/06-handoff.md:153-160,178-181）。因此 GGUF 不能被當成 UI 小功能直接塞入：若要納入，必須先由 server 擁有 runtime、決定單模型替換或雙模型調度、補相容性/品質驗證，並正式解除或修訂單模型邊界。

### 需求 5：本機地址 + 開放外部連入

**結論：顯示本機地址、port、health/ready 與「目前僅本機」可以現在做；「開放外部連入」目前需要先補 TLS、完整授權策略、rate limit、連線上限 enforcement 與明確的 fail-closed 設定，不能交付一個只改 bind 的 checkbox。**

設定預設在 src/tea_asr/config.py:59-97：host=127.0.0.1、port=8327；設定檔讀取在 src/tea_asr/config.py:105-126。CLI 另外接受 serve --host、serve --port，見 src/tea_asr/cli.py:33-61,219-251，最後以 uvicorn.run(..., host=host, port=port, workers=1) 啟動。因此目前是「預設 loopback，但手動 CLI/config 可選別的 bind」，不是 server 已經完成安全 LAN mode。

有用的本機資訊應分開呈現：HTTP base URL http://127.0.0.1:8327、WS URL ws://127.0.0.1:8327/v1/stream、實際設定的 host/port、healthz/readyz 結果、最後探測時間與延遲；若之後列出實際 LAN IP，要明確標示「這是介面地址，不代表目前在監聽」。Mac 現在已會探測 /healthz、/readyz、/v1/status、/v1/capabilities，見 ServerStatus.swift:186-287；每 5 秒刷新，見 AppController.swift:109-114,334-342。

現有 bearer token 的 entropy 不弱：src/tea_asr/config.py:40-56 以 32 bytes random token 建立 0600 檔案；protected endpoint 是精確比對 Authorization: Bearer <token>，見 src/tea_asr/api/app.py:224-226。但它仍是沒有 TLS、沒有 expiration/rotation/revocation/scope、沒有 IP 或 request rate limit 的單一長期 bearer；/healthz、/readyz 不需 token，見 src/tea_asr/api/app.py:243-253。WS 有 bearer 與有限 Origin 檢查，見 src/tea_asr/api/stream.py:965-982。因此 token 強度不能替代 LAN 的傳輸保密與 abuse control。

### 需求 6：在 app 呈現 API/WS 連線狀態

**結論：現有 API/WS 的基本健康狀態可以現在呈現；endpoint URL、HTTP/WS 狀態、模型/queue/session 基本欄位可直接重用現有 snapshot；若要呈現「最近請求、最近 WS session、錯誤歷史、延遲」，需要先補 server 的 bounded diagnostics/status contract 與 Mac 的 typed telemetry。**

目前對外的實際 endpoint 與文件對照如下：

| 介面 | 實際程式 | 文件 | Mac 現況 |
|---|---|---|---|
| GET /healthz | src/tea_asr/api/app.py:243-245，不驗證，回 status=ok | docs/04-api.md:32-35 | ServiceProbe 已使用 |
| GET /readyz | app.py:247-253，不驗證，未 ready 時 503 | docs/04-api.md:36-40 | 已使用 |
| GET /v1/capabilities | app.py:255-268，需 bearer | docs/04-api.md:41,65-80 | 已 decode |
| GET /v1/status | app.py:270-287，需 bearer | docs/04-api.md:41 及 OpenAPI | 已 decode |
| POST /v1/transcriptions | app.py:318 起，需 bearer | docs/04-api.md:40-41 | Swift 主要走 WS；CLI probe 會用 HTTP |
| WS /v1/stream | app.py:289-317 + stream.py:965-982，需 bearer | docs/04-api.md:12-15,82-159 | ASRClient.swift:31-434 已使用 |
| /v1/jobs | 目前實作沒有 route | docs/04-api.md:218-230 說是 v0.2 未實作，session handoff 也記為 404 | 未使用 |

目前 /v1/status 已有 model state/revision、worker generation/load、last error、active sessions、queue，wire schema 見 src/tea_asr/wire.py:90-126；但沒有 server version、bind/port、uptime、每 endpoint 狀態、request/session history、request latency 或最近錯誤集合。ServiceProbe 也沒有記錄各 request 的 latency/timestamp，見 ServerStatus.swift:290-339。若 UI 只顯示目前計數，它可以先交付；若宣稱「server dashboard」，就應由 server 提供 bounded、去敏感資料的 diagnostics。

還有兩個需要在 W1 修正或明確記錄的落差：

- docs/03-architecture.md:132 描述會驗證 Host；目前實際程式只看到 WS Origin allowlist，沒有 HTTP Host middleware。
- wire.py:99 對外宣告 max_total_connections=4，但目前 app.py:289-317 的 WS lifecycle 只有遞增/遞減 activity session，未見總連線上限 enforcement；只有 continuous session admission 有明確限制。不能把 capability 裡的數字當成已驗證的保護。

### 需求 7：總覽改成 server dashboard

**結論：可以現在規劃並分兩段交付；第一段用既有 typed snapshot 把總覽變成資訊密度較高的原生狀態頁，第二段等 server diagnostics contract 完成後補 request/session/錯誤資料。這不需要違反現有視覺原則。**

目前 buildOverviewSection 已有「執行狀態」的服務、模型、Session、輸入、最近文字與三個動作按鈕，見 clients/macos/Sources/TeaASRClient/MainWindowController.swift:544-588。真正缺少的是 server health/ready、HTTP/WS URL、模型 revision/runtime、worker generation/load、queue 使用量與上限、capabilities/protocol、最近 server error、最後探測時間與延遲，以及 client WS session 的最近事件。

建議用目前已有的 grouped rows、plain status header 與固定 label，不引入彩色卡片、大量 icon、web-style tiles。clients/macos/Sources/TeaASRClient/MainWindowController.swift:1333-1388 已明確移除 tinted hero card；clients/macos/Sources/TeaASRClient/MainWindowController.swift:1441-1512 的 stableLabel 與 fixed label column 就是避免 5 秒刷新造成 layout jump 的機制。高頻欄位固定一行或兩行；可變長錯誤才使用可伸展 row。這是「資訊密度 dashboard」，不是「視覺風格 dashboard」。

## 2. 約束衝突

共 **4 條直接衝突或必須先取得決策的邊界**。最嚴重的是 C1/C2：需求 3 若被解讀為「Swift 直接從 Hugging Face 抓 GGUF 並自行載入」，會同時破壞 server ownership、單一 runtime 與可重現性。

### C1：GUI 直接下載模型或建立 GGUF runtime，違反 P5a

- 原文：docs/06-handoff.md:64,126-127：**「模型與 runtime 準備由 server 明確擁有；Mac GUI 僅可呼叫、引導或診斷 tea-asr model-prepare，不得靜默下載模型或自行建立 ASR runtime。」**
- 不能繞過的部分：Swift 不得有 Hugging Face downloader、GGUF loader、自己的 model cache、自己的 worker 或背景下載。
- 可繞過的方式：GUI 只送出明確使用者動作；短期直接呼叫精確的 tea-asr model-prepare；長期由 server 提供 allowlisted catalog/prepare/status/activate API，GUI 只呼叫 API 並顯示進度。

### C2：server 第二 runtime / 雙模型，與目前單 worker、單模型架構衝突

- 原文：docs/06-handoff.md:153-154,178-181：**「每機一個服務instance、一個MLX worker、一份模型」**、**「保留單模型」**；docs/03-architecture.md:30 又把 Rust＋GGUF C++ engine 列為「不在本期」。
- 判斷：P5a 的「不得自行建立另一套 ASR runtime」直接限制的是 GUI；它沒有把 server 未來永遠禁止第二 backend 寫死。但在現有交接狀態下，第二 runtime 仍然不能當作無害增量，因為會改變 worker/scheduler/lifecycle/模型選擇的架構假設。
- 可繞過的方式：先做 server-owned feasibility gate 與 user decision；若批准，必須更新 architecture/hand-off，定義單模型切換或雙模型調度、記憶體上限、readiness、session drain、capabilities 與品質驗證。沒有這些之前，GGUF 只能顯示「未支援」，不得假裝可選。

### C3：LAN checkbox 只改 bind，違反 LAN 進入條件

- 原文：docs/06-handoff.md:147：**「LAN | 明確需求、TLS／授權／rate limit；不得只改bind至0.0.0.0。」**
- 不能繞過的部分：不能把「開放外部連入」實作成 host=0.0.0.0 的布林值，也不能只因 bearer token 存在就宣稱安全。
- 可繞過的方式：先交付 loopback 地址與 disabled explanatory UI；等 TLS、授權/rotation、rate limiting、Host/Origin、連線上限 enforcement 和測試全部 PASS，再讓 server 以 explicit security-ready 設定進入 LAN mode；任何條件不滿足就 fail closed。

### C4：模型選擇不能靜默替換，也不能宣稱未驗證能力

- 原文：docs/06-handoff.md:158,181：**「模型不等於translator、aligner或diarizer；capabilities只宣告實際驗證能力。」**、**「不得靜默替換模型、fallback mock、宣稱未測的串流／翻譯／時間戳能力。」**
- 風險：任意 Hugging Face repo、任意 GGUF、未驗證 quantization 都可能沒有相同 tokenizer/feature extractor/quality；UI 不能只因模型出現在搜尋結果就把它標成可用。
- 可繞過的方式：只允許 server lockfile 的 model id/revision/hash/runtime；active model 切換必須是明確動作；prepare 失敗要顯示 typed error；每個模型的 capabilities 只能來自實際 contract/測試，不能從模型名稱推測。

## 3. 分階段實作方案

以下 10 個工作項都可獨立派工與驗收。每一項的「涉及檔案」是實作者第一輪必讀/可能修改的範圍，不代表可以忽略 AGENTS.md 或既有測試。

### W1 — 凍結目前 API/runtime 真實契約，先修正文件落差

**目標**

把目前已存在的 endpoint、auth、status 欄位、限制數字與「未實作」功能寫成單一真實契約，讓後續 Mac UI 不會針對不存在的 /v1/jobs 或假設已 enforce 的連線上限開發。這一項不新增 LAN 或模型能力。

**涉及檔案**

- src/tea_asr/api/app.py:224-317：endpoint、auth、health/ready、status、WS lifecycle。
- src/tea_asr/wire.py:90-126：Capabilities、StatusResponse、queue/limit schema。
- src/tea_asr/api/stream.py:965-982：WS auth/Origin。
- docs/04-api.md:3-15,32-80,218-230、docs/03-architecture.md:132-134、docs/api/openapi.json:398-580。
- 現有 HTTP/schema contract tests；先用 rg --files tests 找出對應檔案，不要新造第二套測試位置。

**可機械核銷的驗收條件**

- endpoint inventory 只列出實際存在的 /healthz、/readyz、/v1/capabilities、/v1/status、/v1/transcriptions、/v1/stream；/v1/jobs 明確標 404/未實作。
- contract test 明確驗證：health/ready 不需 bearer；capabilities/status/transcriptions/WS 缺 bearer 會被拒絕；WS 有效 bearer 可建立。
- 文件與 wire schema 都把 max_total_connections 寫成目前真實值 4，且不把它描述成已 enforcement，除非同一項實作了 enforcement。
- docs/03-architecture.md 不再把尚未存在的 HTTP Host 驗證寫成已完成，或補上對應測試/實作。
- uv run pytest tests/unit tests/integration -q 綠；未改行為時測試數不得低於 baseline。

**預估難度**：S。  
**相依關係**：無；建議第一個做。

### W2 — 本機地址與唯讀連線摘要

**目標**

現在就交付安全的需求 5 子集：顯示實際設定的 loopback endpoint、HTTP/WS URL、health/ready 狀態、最後探測時間與可連線性；明確告知外部連入尚未開放。不要新增 bind checkbox，不要改 server bind。

**涉及檔案**

- src/tea_asr/config.py:59-151、src/tea_asr/cli.py:30-61,219-251：確認 default/config/CLI 的 host-port 語意。
- clients/macos/Sources/TeaASRClient/Settings.swift:27-37,137-150：host/port/stream URL。
- clients/macos/Sources/TeaASRClient/ServerStatus.swift:115-126,186-355：ServiceSnapshot/probe。
- clients/macos/Sources/TeaASRClient/AppController.swift:109-114,334-342：5 秒 refresh。
- clients/macos/Sources/TeaASRClient/MainWindowController.swift:754-872,953-988,1608-1621：設定、diagnostics、overview summary。
- 相關 Swift model/probe/UI tests；不要覆蓋其他 agent 正在改的 audio 檔案。

**可機械核銷的驗收條件**

- 預設畫面可產生且顯示 http://127.0.0.1:8327 與 ws://127.0.0.1:8327/v1/stream；自訂 host/port 顯示自訂值。
- 至少分開顯示 healthz、readyz、/v1/status、/v1/capabilities 的成功/失敗；失敗只顯示 typed error，不顯示 response body/token。
- overview 或 diagnostics 有固定文案說明「目前僅本機監聽／外部連入尚未開放」；沒有產生 0.0.0.0 bind、沒有呼叫 serve --host、沒有寫 service config。
- Swift unit test 覆蓋 default endpoint、invalid port、health success + ready 503、401/timeout；cd clients/macos && swift build && swift test 綠。

**預估難度**：M。  
**相依關係**：W1；可與 W3 平行，但建議先完成 W1 契約。

### W3 — 明確觸發既有 model-prepare 的 client flow

**目標**

在 server catalog/API 尚未完成前，先讓 Mac 使用者能從 app 明確觸發既有 CLI，顯示「準備中／完成／失敗／退出碼」，且 app 啟動、probe、切換頁面都不會自動下載。這是需求 3 可立即交付的最小垂直切片，不是假裝已支援任意模型。

**涉及檔案**

- src/tea_asr/cli.py:33-39,256-264：命令目前無參數且同時處理 ASR/VAD。
- clients/macos/Sources/TeaASRClient/ServiceControl.swift:3-7,66-106：獨立 service process 與 executable resolution；必要時抽出可測試的 process runner。
- clients/macos/Sources/TeaASRClient/AppController.swift：action wiring、主執行緒/背景 process lifecycle。
- clients/macos/Sources/TeaASRClient/MainWindowController.swift:544-588,590-627,754-872：明確 action、進度與錯誤展示。
- client tests；必要時新增 dependency-injected fake process runner，不要測試真的下載。

**可機械核銷的驗收條件**

- 只有使用者按下明確的「準備模型」 action 才會 spawn process；app launch、5 秒 refresh、status probe、切換 overview 都不會 spawn。
- process arguments 精確為 tea-asr model-prepare，不接受 Swift 傳入任意 repo URL/path；不含 Hugging Face SDK、GGUF parser、第二 worker。
- UI 會處理 running、success、non-zero exit、找不到 executable、取消/中斷；process 不阻塞 main thread。
- 測試用 fake runner 驗證 invocation count=1/0 的情境；現有 service start/stop 行為不被改變。
- cd clients/macos && swift build && swift test 綠；文件明確寫成固定模型 prepare，不稱為 Hugging Face 任意下載。

**預估難度**：M。  
**相依關係**：W2；依賴現有 CLI，不等待 GGUF 決策。

### W4 — server-owned allowlist、模型 catalog 與 prepare job API

**目標**

若產品決策是要做「模型選擇頁」，先把選擇的所有權放在 server：只有 lockfile/allowlist 內、固定 revision/hash/runtime 的模型能被列出與準備。GUI 只呼叫 API；GET 不觸發下載；任何 prepare 都是明確使用者動作。建議 API contract（目前不存在，不能在 client 先假設）：

- GET /v1/models：列出 model_id、display name、repo/revision、runtime、prepared、active、已驗證 capabilities；需 bearer。
- POST /v1/model-preparations：body 只接受 allowlisted model_id；回 202 + preparation id；需 bearer。
- GET /v1/model-preparations/{id}：回 queued/running/ready/failed、phase、safe error、started/finished time；不得回 token/path。
- POST /v1/models/active：只接受已 prepared model；明確 action，若需要 drain/restart 要回 409/typed state，不得靜默替換。

若不想新增一套 preparation API，也可以把 W3 的 CLI 作為第一版 UX；但「列 catalog、進度、切換」不能靠讀 client 本機資料猜測，仍需 server contract。

**涉及檔案**

- models.lock.json:1-30、src/tea_asr/model_spec.py:8-49：由單一固定 spec 擴成嚴格 allowlist；保留 revision/hash/runtime。
- src/tea_asr/model_manager.py:10-29：prepare/locate 的 server-owned lifecycle、hash/lock 驗證與 bounded progress state。
- src/tea_asr/cli.py:33-39,219-264：CLI 與 API 共用 manager；保留舊的無參數 command 相容性或明確 migration。
- src/tea_asr/api/app.py:255-317、src/tea_asr/wire.py:90-126：新 route/request/response schema、auth、active model policy。
- src/tea_asr/worker/supervisor.py:39-85、scheduler/stream lifecycle：active model 切換前後 worker/readiness/session policy。
- tests/unit/、tests/integration/ 中 model/cache/API/contract tests；docs/03-architecture.md、docs/04-api.md、docs/06-handoff.md、docs/10-session-handoff.md、root README。

**可機械核銷的驗收條件**

- unknown model id、未 pin revision、hash mismatch、錯 runtime 都在 server/CLI 被拒絕；不能把任意 Hugging Face URL 當 model id。
- GET /v1/models 是 read-only；單純 probe/GET 不會建立 download task；沒有準備好的 model 不會被 serve 靜默下載或 fallback。
- prepare job 有可測試的狀態轉移與 bounded progress/error；server restart 或 worker failure 不會偽造 ready。
- active switch 只有明確 API action；正在使用中的 session 有明確 drain/reject policy；沒有 silent model replacement。
- 每個 model 的 capabilities 由 allowlist/測試資料提供，沒有因名稱自動宣稱 translation/timestamps/diarization。
- Python unit/integration tests、ruff、API schema export 全綠；docs 04 不把 /v1/jobs 的未實作狀態與新 preparation API 混為一談。

**預估難度**：L。  
**相依關係**：W1；需要先回答 D1、D2、D3。W5 的 GGUF 結論會決定 allowlist 是否只含 MLX。

### W5 — GGUF / mini server-side 可行性 gate（只做決策證據，不先做 UI）

**目標**

針對需求中「mini (gguf)」的真實來源做一次可重現的 server-side feasibility gate。因為 repo 只找到 mini 的 MLX/BF16 benchmark，實作者不能自行猜 repo、revision 或 runtime。若使用者提供確切 GGUF URL/revision，才進行 load/transcribe/品質/資源驗證；沒有輸入就交付「目前不支援」的明確結果。

**涉及檔案**

- pyproject.toml:12-21：目前依賴與可能引入的第二 runtime。
- src/tea_asr/backend.py:46-103、src/tea_asr/model_spec.py:8-49、src/tea_asr/model_manager.py:10-29：MLX-specific boundary。
- src/tea_asr/worker/supervisor.py:39-140、scheduler/stream session：單 worker/queue/lifecycle 影響。
- benchmarks/quality_eval.py:164-190、benchmarks/quality_eval_bf16.py:1-17、docs/benchmarks/mini-model-report.md:118-225。
- 若實驗成功才可修改 models.lock.json/docs/03-architecture.md/docs/06-handoff.md；不要先在 Swift 加 runtime。

**可機械核銷的驗收條件**

- 報告列出確切 model repo、revision、檔案格式、runtime/library、license 與是否可由 server 固定準備；缺資料則標 unverified，不補猜測。
- 在目標 Apple Silicon 上有最小 load + transcribe test；記錄 sample size、WER/PUA、RSS、latency、model load time，不能只引用 mini 的 n=30 benchmark。
- 驗證單 worker、bounded queue、continuous/utterance、worker restart、readiness 與 session drain；確認第二 runtime 是否會違反現有 memory/lifecycle 約束。
- 結果只能是：A. 維持 MLX-only/不支援 GGUF；或 B. server-owned 第二 runtime，並附 architecture ADR、更新約束、依賴、測試與 user approval。禁止 C. client-side GGUF runtime。
- 若未取得 D1 決策或真實 GGUF artifact，這項工作必須停在報告，不可合併「看起來能選」的 UI。

**預估難度**：XL。  
**相依關係**：D1、D2；可與 W2/W3 平行，但 W4 的最終 model catalog 必須等待此 gate。

### W6 — server bounded diagnostics/status contract

**目標**

補足需求 6/7 的 server 視角，但只暴露 dashboard 真正需要的 metadata。建議保留目前 /v1/status 的穩定 snapshot，另加需 bearer 的 GET /v1/diagnostics；若團隊偏好單一 endpoint，也可把欄位併入 status，但必須固定 schema。建議內容：

- service：protocol version、server version/build、bind host/port/transport、uptime、started_at。
- model/worker：model id/revision/runtime、state、generation、load_ms、safe error code。
- capacity：active WS session、configured/enforced limits、queue waiting tasks/samples/max。
- recent request/session ring：有限筆數、時間、endpoint/category、duration、profile、status/error code、opaque session/request id；不含 PCM、transcript、Authorization、filesystem path、完整 exception/body。
- recent errors：bounded、去敏感、可由 client 顯示；明確 retention 與 reset/restart 行為。

不需要 server endpoint 才能列出靜態 URL：client 可以依 host/port 組合 /healthz、/readyz、/v1/status、/v1/capabilities、/v1/transcriptions、/v1/stream。server API 的價值是提供「這些 endpoint 最近是否被使用／失敗」的 metadata。

**涉及檔案**

- src/tea_asr/wire.py:90-126：typed response schema。
- src/tea_asr/api/app.py:224-317：auth、status、HTTP/WS instrumentation。
- src/tea_asr/api/stream.py:965-982 及 session/activity lifecycle：建立/關閉/錯誤/延遲記錄。
- src/tea_asr/worker/supervisor.py:39-140：safe worker state/error mapping。
- src/tea_asr/config.py:59-151：retention/ring size 設定若需要。
- tests/unit/、tests/integration/、docs/04-api.md、OpenAPI、docs/06-handoff.md。

**可機械核銷的驗收條件**

- 未帶 bearer 不能讀 diagnostics；帶有效 bearer 只得到 schema 宣告的 metadata。
- ring buffer 有明確上限並在超限時丟棄最舊資料；測試能證明不會無界增長。
- API/WS success、4xx、5xx、timeout、disconnect 至少各有一條測試；session counter 在正常與異常 close 後都回到正確值。
- JSON/logs/UI diagnostics 中搜尋不到 bearer、PCM、transcript、完整 token、filesystem path；error 以 code/message allowlist 或 redaction 呈現。
- max_total_connections 要麼真正 enforce 並測試，要麼從 capability 改成未承諾；不能繼續宣告未實作的保護。
- Python tests + ruff + OpenAPI export 綠，且 docs 明確記錄新的 endpoint/retention/privacy。

**預估難度**：L。  
**相依關係**：W1；也是 W7/W8 的 server data prerequisite，和 W9 的 connection-limit gate 有重疊但不能省略。

### W7 — Mac typed API/WS telemetry

**目標**

把現有的健康 probe 擴成可供 UI 使用的 typed connection snapshot：每個 HTTP endpoint 的 URL、狀態、latency、last checked；WS 的 disconnected/connecting/listening/closed、close/error、session id/last event；加上 W6 的 server active session/queue/recent metadata。不要把 raw response body 或 token 帶進 UI。

**涉及檔案**

- clients/macos/Sources/TeaASRClient/ServerStatus.swift:8-173,186-355：新增 typed endpoint check/diagnostics model，保留現有 decode error redaction。
- clients/macos/Sources/TeaASRClient/AppController.swift:100-115,334-342：5 秒 polling 與 state update。
- clients/macos/Sources/TeaASRClient/ASRClient.swift:31-434、Protocol.swift:1-31：保存 session-started 的 opaque session id、last event/close/error、連線時間；不可保存音訊。
- clients/macos/Sources/TeaASRClient/AppState.swift：snapshot/last error 的資料流。
- clients/macos/Sources/TeaASRClient/MainWindowController.swift:953-988,1608-1645 及 typed model tests。

**可機械核銷的驗收條件**

- mock response 能 decode W6 schema；每個 endpoint check 都有 success/status/error/checkedAt/latency，且 timeout/401/503 不會互相覆蓋成一個模糊的「失敗」。
- ASRClient 收到 session.started 後保存 opaque session id，收到 close/error 後保存狀態；測試可驗證 state sequence。
- 5 秒 refresh 只更新 existing view/state，不重建 section root；probe failure 不會清掉上一個可用 snapshot 以致 UI 閃爍。
- Swift tests 覆蓋 valid/invalid JSON、missing token、401、ready 503、WS close/error、diagnostics redaction；swift build && swift test 綠。

**預估難度**：M。  
**相依關係**：W2、W6；可先用 fixture 開發，等 server endpoint 完成再 integration test。

### W8 — 原生 macOS dashboard 總覽

**目標**

把需求 7 實作成資訊密度較高、但仍像 Apple 原生 app 的 overview。保留現有三個動作按鈕與最近文字，把 server health、model、capacity、API/WS connection 變成可快速掃讀的 rows；詳細 request history/錯誤放 Diagnostics，不把全部 telemetry 堆在首頁。

**建議 overview 欄位**

- 服務：可用/未就緒/無法連線、host:port、HTTP base URL、WS URL、healthz/readyz、最後探測時間/延遲。
- 模型：display id、revision 短 hash、runtime（MLX；未支援 GGUF 不顯示成可用）、state、worker generation/load、idle 秒數。
- capacity：active session/上限、queue waiting tasks/samples/max；若上限尚未 enforcement，顯示「未啟用」而不是數字冒充保護。
- protocol/capabilities：版本、profile、已驗證 feature；把 false/未支援明確顯示，不推測翻譯/時間戳/diarization。
- 本機 client：輸入裝置、錄音/session 狀態、最近文字、最近 server/client error、三個現有 action buttons。

**涉及檔案**

- clients/macos/Sources/TeaASRClient/MainWindowController.swift:91-114,544-588,1333-1512,1608-1645：section、rows、status header、stableLabel/layout。
- clients/macos/Sources/TeaASRClient/AppState.swift、ServerStatus.swift、ASRClient.swift：資料來源。
- clients/macos/Tests/TeaASRClientTests/MainWindowSectionUpdateTests.swift:4-13,56-83 及新增 overview update tests。
- clients/macos/README.md:100-105：使用者可見功能說明。

**可機械核銷的驗收條件**

- overview update 前後 root section/view identity 不變；測試用多次 update 驗證。
- 所有高頻欄位使用固定 maxLines/固定 label row；model/session/queue/connection 數字更新不會改變 row 高度。可變長錯誤才允許 grow。
- 變更中沒有新增彩色卡片、彩色 status tiles、大量裝飾 icon、web-style grid；語意色只用於既有 error/warning 規則。
- tests 能核銷上述每一欄有資料來源；cd clients/macos && swift build && swift test 綠。
- 實機目視仍列為 user acceptance：視窗寬度、字體、refresh 時是否跳動、sidebar/overlay/focus 不能由 CLI agent 假裝已驗收。

**預估難度**：M。  
**相依關係**：W2、W7；W6 完成後才能呈現完整 server dashboard。必須避開其他 agent 的未提交 audio UI 變更。

### W9 — LAN mode security gate；完成前 UI 只能 disabled

**目標**

只有在安全條件全部滿足後才新增「開放外部連入」設定。預設永遠 loopback；不能以 checkbox 直接把 host 改成 0.0.0.0。需先選定 TLS 是 server 內建或明確支援的 reverse proxy，並讓 server 能判斷 security-ready，否則拒絕 non-loopback bind。

**必補能力**

- TLS：憑證配置、私鑰權限、啟動失敗與 renewal/替換策略；WS 必須走 wss。
- 授權：保留強 bearer 只是起點；定義 rotation/revocation/expiration、scope 或至少單一 server token 的 provisioning，且 protected HTTP/WS 覆蓋範圍有測試。
- rate limit：至少 handshake、HTTP transcription、prepare/status abuse path；回應 code/WS close code 與 reset window 固定。
- network validation：HTTP Host、WS Origin、IPv4/IPv6、可接受 interface；不得 wildcard allowlist。
- capacity enforcement：max_total_connections 真正 enforce；bounded queue/backpressure；不把未實作限制宣告為 capability。
- logging/privacy：不記 token、PCM、transcript、path；安全事件有 bounded audit metadata。
- UX：只有 server 回報 all gates PASS 才 enable；未完成時顯示缺哪些條件與如何回到 loopback。

**涉及檔案**

- src/tea_asr/config.py:59-151：validated LAN security config/default。
- src/tea_asr/cli.py:219-251：拒絕不安全 non-loopback 啟動或要求明確 security profile。
- src/tea_asr/api/app.py:224-317、src/tea_asr/api/stream.py:965-982、src/tea_asr/errors.py:7-50：auth/TLS/Host/Origin/rate-limit/error contract。
- clients/macos/Sources/TeaASRClient/Settings.swift:27-37,754-872、MainWindowController.swift：disabled/guarded UI，不能先改 bind。
- integration/security tests、docs/03-architecture.md、docs/04-api.md、docs/06-handoff.md、docs/10-session-handoff.md。

**可機械核銷的驗收條件**

- 預設啟動仍是 127.0.0.1:8327；缺任何 TLS/auth/rate-limit/validation gate 時，non-loopback bind 以明確錯誤退出，不會啟動半安全模式。
- TLS client test 能完成 HTTPS/WSS handshake；明文 remote HTTP/WS 被拒絕或只允許 loopback。
- 缺 token、錯 token、過期/撤銷 token、錯 Host/Origin、超出 rate limit、超過總連線數，各有固定可測試 response/close code。
- client 的 checkbox 在 server capability 未宣告 security-ready 時不可勾選；沒有任何 code path 只把 host 改成 0.0.0.0。
- Python/Swift tests、ruff、docs/OpenAPI 綠；需要真機網路與憑證的部分另列手動驗收。

**預估難度**：XL。  
**相依關係**：D4、W1、W6；W2 可先交付 loopback 顯示，但 W9 未完成前不可交付外部監聽。

### W10 — 每一階段的交接、文件與驗收封口

**目標**

確保「能做什麼」與程式一致，避免只改 UI 而 handoff 仍宣稱未完成，或只改 server 而 Mac 顯示錯誤。每個 W1-W9 完成後都要更新相應文件；W10 是 release gate，不是把文件延後到最後才補。

**涉及檔案**

- docs/06-handoff.md：P5a、延後功能進入條件、模型/LAN/API 完成狀態。
- docs/10-session-handoff.md：目前元件真實狀態、待驗收清單、測試 baseline。
- docs/03-architecture.md、docs/04-api.md、docs/api/openapi.json、root README.md、clients/macos/README.md。
- 受影響的 unit/integration/Swift tests 與 benchmark report。

**可機械核銷的驗收條件**

- uv run pytest tests/unit tests/integration -q、uv run ruff check .；若改 Mac，cd clients/macos && swift build && swift test 全綠。
- API endpoint、OpenAPI、README、handoff 的 model/host/auth/capabilities/未實作功能與程式一致；沒有未標示的 TODO/hack。
- git diff --stat 只包含該工作項允許的檔案；保留其他 agent 的既有修改，不做 reset/checkout。
- 驗收報告逐條列 PASS/FAIL、指令與 檔案:行號；實機 GUI、TCC、全域快捷鍵、真實模型品質、TLS/外網等不能在 CLI 證明的項目明確標「需要使用者用眼睛確認」。

**預估難度**：S。  
**相依關係**：各工作項的收尾步驟；不能用來掩蓋前一項未通過的測試。

### 建議執行順序

1. W1：先凍結 API/runtime 真實契約與文件落差。
2. W2：交付 loopback 地址、health/ready 與「外部尚未開放」說明。
3. W3：交付固定模型的明確 model-prepare action。
4. W6：補 server bounded diagnostics/status；可與 W4/W5 在不同檔案範圍平行，但都要先過 D1/D2 的模型決策。
5. W7：Mac typed API/WS telemetry。
6. W8：用 W2/W6/W7 的資料重做 native dashboard。
7. W4：若 D1/D2 決定要有模型選擇，實作 server-owned allowlist/catalog/prepare/switch contract。
8. W5：在納入任何 GGUF 前完成實證 gate；若不批准，W4 只支援已驗證 MLX。
9. W9：只有安全 gate 全部具備後才做 LAN mode；此順序不可提前。
10. W10：每項交付都同步文件與逐條驗收；不應等到所有功能做完才補。

## 4. 需要使用者決策的問題

### D1：GGUF 要不要進本期？

- A（建議）：本期 MLX-only；只做目前固定模型的顯示/prepare，GGUF 留在 W5 feasibility gate。
- B：批准 server-owned 第二 runtime；接受增加 dependency、記憶體、worker/scheduler/lifecycle 與測試成本，並正式修訂單模型約束。
- C（不建議且違反 P5a）：Swift client 自己下載/載入 GGUF。

### D2：Hugging Face 模型來源要多開放？

- A（建議）：只允許 repo 維護者審核的 allowlist + immutable revision/hash。
- B：允許使用者在 app 搜尋任意 Hugging Face repo；這需要額外 compatibility/security/license/quality sandbox，不應直接採用。
- C：允許使用者指定本機 model path；仍由 server 驗證/準備，且要定義 portability、權限與刪除 UX。

### D3：模型 activate 的行為？

- A（建議）：prepare 與 activate 分開；activate 時 drain 現有 session，必要時重啟唯一 worker，未完成前維持舊模型。
- B：同時 resident 兩模型、依 profile 路由；需要解除「一個 MLX worker、一份模型」並做記憶體/公平性驗證。
- C：prepare 後下次 server restart 才生效；實作簡單，但 UI 必須明確顯示「已準備、尚未 active」。

### D4：LAN 安全部署模式選哪一種？

- A（建議的近期決策）：維持 localhost-only，先交付地址/status 與 disabled 說明。
- B：server 內建 TLS + token lifecycle + rate limit + Host/Origin + connection limit；開發量最大但 app 可自洽。
- C：app/server 只提供明確 reverse-proxy integration，TLS 由使用者的 proxy 管理；需定義 proxy trust/header、部署文件與 UI 探測。

### D5：dashboard 的歷史資料保留多久？

- A（建議）：只保記憶體內 bounded recent metadata；不存音訊/文字，server restart 清空。
- B：持久化 request/session history；需要 retention、刪除、export、權限與隱私說明。
- C：只顯示目前 counters，不做 recent history；最安全但無法完整滿足「最近請求／WS session」。

### D6：模型準備進度的第一版 UX？

- A（建議）：先由 app 明確呼叫現有 CLI，顯示 process state/exit code；不提供任意 HF 搜尋。
- B：先做 server-owned preparation API/job；UX 完整，但要先完成 W4 的 manager/job contract。
- C：A+B；CLI 作為 recovery path、API 作為正常 UX，成本較高但最完整。

## 5. 未證實清單

- **「mini (gguf)」的真實 artifact 未證實**：repo/docs/benchmarks/git history 只找到 JacobLinCool/TEA-ASR-1.1-mini 的 BF16/自轉 MLX 4-bit/8-bit資料；沒有 GGUF repo、revision、檔名或使用者所指的前次測試 URL。需要使用者提供確切來源才能驗證。
- **目前正在運行的 server 狀態未證實**：已由 source 確認預設 127.0.0.1:8327，但這次沒有把 live process 當成事實來源，也沒有確認實機當下的 LAN interface/IP。
- **GGUF runtime 未選定、未安裝、未在 Apple Silicon 驗證**：不能預估第二 runtime 的實際 RSS、latency、Metal 相容性或品質。
- **TLS deployment 取捨未決**：server 內建 TLS 與 reverse proxy 的憑證、rotation、trust boundary、使用者操作流程尚未指定。
- **bearer token lifecycle 未定義**：目前已確認是 32 random bytes、0600 file、靜態 exact-match bearer；rotation、revocation、expiration、scope、多人/多 client provisioning 尚無產品決策。
- **HTTP Host 驗證是文件與實作落差**：architecture 文件寫有 Host verification，但目前查到的 code 明確實作的是 WS Origin allowlist；是否為遺漏、設計變更或文件過期，需在 W1 決定。
- **max_total_connections 的產品語意未定**：wire schema 宣告 4，但現有程式未見總連線 enforcement；需決定補 enforcement 或撤回該 capability。
- **CLI help 的現場執行未完成**：uv run tea-asr --help / model-prepare --help 在本次環境因 uv cache /Users/c2leb/.cache/uv/sdists-v9/.git permission error 退出；CLI options 已由 src/tea_asr/cli.py:33-61,256-264 source 核對，但仍需在正常開發環境實跑。
- **Mac 實機視覺與系統整合未證實**：排版是否跳動、overlay/focus、全域快捷鍵、TCC 權限、外部網路、真實模型延遲/辨識品質都不能由這份 CLI 唯讀調查標成 PASS，須依 AGENTS.md:81-88 列為 user acceptance。
