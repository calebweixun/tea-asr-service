# 04｜API 與事件契約

> 基線wire 1.0，P2a增加opt-in wire 1.1擴充；路徑仍使用 `/v1`。v0.1／v0.1.1／v0.2是產品里程碑，不是wire版本。
>
> **實作狀態（2026-09-19）：** v0.1的HTTP端點與WS utterance session已實作，機器可讀契約在 [docs/api/](api/)。
> continuous profile與Silero VAD已實作，`speech.started` 與 `boundary=silence/max_duration` 會實際送出。
> 尚未實作：durable session、`/v1/jobs`、resume；這些選項一律回 `unsupported_option` 或404，不會靜默降級。
> P2a的1.1擴充已實作但未通過驗收，預設關閉（`TEA_ASR_EXPERIMENTAL_REVISABLE_PREVIEW=1` 才啟用）。

## 共通規則

- 本機 URL：`http://127.0.0.1:8327`；WS 為 `ws://127.0.0.1:8327/v1/stream`。port 可由設定檔改，但預設刻意避開 8765 等 AI 工具常用 port。
- 除 `/healthz`、`/readyz` 外，需要 `Authorization: Bearer <token>`。WS upgrade 時驗證。
- IDs 為 server UUID 字串；client `request_id` 為1–64字元識別碼，同一 session 不得重用於不同操作。
- JSON 為 UTF-8；拒絕未知 client 欄位與不支援選項，不能 silently ignore。client 可忽略未知 server 欄位，但未知 event type 要記錄診斷。
- 所有 duration/timing 欄位單位明示；`start_sample`／`end_sample` 採16kHz來源時間軸、左閉右開。
- `audio_ms` 是送入辨識片段的樣本數／16；`queue_ms` 是等待 scheduler；`inference_ms` 是 worker 實際耗時。不得混用。
- 時間戳是 segment 範圍，不是逐字對齊。`raw_text` 保留原始辨識；v0.1 `text=raw_text`。
- 所有 error 有 `code`、可讀 `message`、`retryable`、`request_id`（若可取得）；內部 traceback 不回 client。

```json
{"error":{"code":"queue_full","message":"辨識佇列已滿，請稍後重試。","retryable":true,"request_id":"req-1"}}
```

## HTTP：v0.1

| Endpoint | 用途 | 結果 |
|---|---|---|
| `GET /healthz` | 主程序存活 | 200 `{"status":"ok"}`；不代表模型 ready |
| `GET /readyz` | 可接受辨識 | 200 ready，否則503；只回 state |
| `GET /v1/capabilities` | 協定／能力／容量 | 實際配置與已驗證能力 |
| `GET /v1/status` | 經授權診斷 | model state、queue、worker generation、版本；無音訊原文 |
| `POST /v1/transcriptions` | 短 PCM 辨識 | 200結果、202不使用；同步最多30秒音訊 |

`/readyz` 在模型 ready 且未 shutdown 時為200；queue 滿則 request 本身429，不把 queue 狀態誤報模型故障。`idle_unloaded` 回503，第一個辨識請求觸發 load 並回 `model_loading`。

`POST /v1/transcriptions` 使用 `Content-Type: application/octet-stream`，query 必填 `sample_rate=16000&channels=1&format=pcm_s16le`；`X-Request-ID` 建議提供。格式值不符422。streaming body 接收上限960,000 bytes，不能只信 Content-Length；空 body、奇數長度回422；超過回413。收到完整 body 前不排推論。v0.1 不接受 WAV、路徑、URL、base64 或 translation 參數。CLI 負責把 WAV 轉 raw PCM。

HTTP 無 idempotency 保證；斷線後可能已完成運算，client 重試會重算。server 偵測取消後移除尚未開始工作，已開始則丟棄結果。結果僅在回應內，不自動持久化。

```json
{
  "request_id":"req-1",
  "model_revision":"caee57a908b6d64be08a6462c7a21ececbd4d7cb",
  "text":"下午跟 client 開會。",
  "raw_text":"下午跟 client 開會。",
  "language":"Chinese",
  "segments":[{"segment_id":"seg-uuid","start_sample":0,"end_sample":64000,"text":"下午跟 client 開會。","timestamp_quality":"segment"}],
  "audio_ms":4000,
  "queue_ms":12,
  "inference_ms":480,
  "warnings":[]
}
```

上述數字只示範欄位，**不是效能實測**。純靜音成功回空 text、空 segments、`warnings:["no_speech"]`；模型失敗則5xx，不能混為一談。

Capabilities 最少有：

```json
{
  "protocol_version":"1.0",
  "audio":{"sample_rate":16000,"channels":1,"format":"pcm_s16le"},
  "profiles":["utterance","continuous"],
  "features":{"native_audio_streaming":false,"partial_transcripts":false,"word_timestamps":false,"translation":false,"diarization":false,"hotwords":false,"durable_sessions":false,"batch_jobs":false},
  "limits":{"max_frame_pcm_bytes":6400,"max_utterance_ms":30000,"max_continuous_sessions":1,"max_total_connections":5}
}
```

feature 只有完成該功能驗收才變 true；即使 mock mode 也不能假稱真模型。

## WebSocket：v0.1

一條連線＝一個 session，避免多路 PCM multiplexing。read loop、event writer、scheduler 獨立執行。server 建立 socket 後送 `hello`（協定版本、model_state），client 在5秒內送 `session.start`；server 只有在模型 ready、容量允許時回 `session.started`。未 started 禁止 binary。

```json
{"type":"session.start","request_id":"start-1","profile":"continuous","audio":{"sample_rate":16000,"channels":1,"format":"pcm_s16le"},"language":"Chinese","durable":false}
```

```json
{"type":"session.started","session_id":"session-uuid","profile":"continuous","next_seq":0,"next_sample":0,"send_until_sample":80000}
```

`send_until_sample` 是流控窗口上界；初始5秒。任何 frame 的 end_sample 不得超過此值。server 釋放緩衝後以 `flow.control` 提高窗口，永不回退。每次 grant 先保留 session/global buffer capacity，避免多連線同時超配。server 每250ms或窗口變動時可送 update；未取得新窗口時 client 必須暫存或停止送出。client 即時收音本身不能停頓來偽造連續錄音。

### Binary frame

每個 WS binary message：16-byte header＋PCM。header 前8 bytes 為 uint64 little-endian `seq`，後8 bytes 為 uint64 little-endian `start_sample`，PCM 長度1–6,400 bytes 且為偶數。

- 第一個 seq=0、start_sample=0；往後 seq 每次＋1，start_sample 必須等於前一個 end_sample。
- end_sample＝start_sample＋PCM bytes／2。
- 不含 RIFF/WAV header；每個 message 是一個 frame，不依 TCP 分包猜測邊界。
- seq 重複／跳號、sample 不連續、超 window、非 started 狀態送音訊皆 protocol error，不能默默補零或重複辨識。
- JSON 中 sample/seq 整數不得超過 `2^53-1`，方便 JavaScript client 正確表示。

continuous 靜音也照送，VAD 才能維持來源時間。若 client 擷取中斷，v0.1 結束當前 session 並建立新 session；不可把間斷後的聲音接在旧 sample clock，假裝沒有缺口。

### Client 控制事件

| type | 欄位／語意 |
|---|---|
| `session.start` | 上述欄位；同連線只能一次 |
| `audio.commit` | `request_id`、`through_seq`；utterance 將目前已收音訊提交一段，不關 session |
| `session.stop` | `request_id`、`through_seq`；最後 frame seq，尚無音訊可為null；flush VAD 殘留並等所有 final |
| `session.cancel` | `request_id`；放棄未完成 segment，不再發其 final |
| `ping` | `request_id`；回 pong |

commit/stop 的 `through_seq` 必須等於最後已收 seq；WS 有序保證此 barrier，不一致回 error。utterance commit 後下一段可繼續送，但總 sample clock 不重設。continuous 不接受 audio.commit。空 commit 回 `audio.committed`（segment_id=null、reason=no_audio）。重送相同 request_id＋相同內容時，在當前 session 只重送既有控制 ACK，不重複提交；ID對應內容不同回 conflict。控制去重表上限256項，淘汰最舊已完成项，client 不得依赖被淘汰項跨期去重。

### Server 事件與文字終局

所有 session.started 之後的 server events 含 `session_id`、單調递增 `event_id`，控制 reply 同時帶 request_id。event_id 含 flow/pong 等非文字事件；session.started 固定 event_id=0。

| type | 核心欄位 | 意義 |
|---|---|---|
| `audio.ack` | received_seq、received_sample、persisted_seq、persisted_sample | v0.1 persisted 欄位為null，ACK接收不等於可復原 |
| `audio.committed` | request_id、segment_id、reason | 手動片段進入排程；不等於完成 |
| `speech.started` | segment_id、start_sample | VAD確認開始；utterance可省略 |
| `segment.queued` | segment_id、segment_index、start_sample、end_sample、boundary | segment已封口；boundary為manual/silence/max_duration/stop |
| `transcript.final` | 下列示例 | 此 segment 的不可變終稿 |
| `segment.skipped` | segment_id、segment_index、reason | no_speech 或 empty；不生成虛構文字 |
| `segment.error` | segment_id、segment_index、code、retryable | 明確指出片段未產生結果 |
| `flow.control` | send_until_sample、reason | 更多窗口或目前持續暫停；窗口不回退 |
| `session.stopped` | request_id、last_seq、status、failed_segments | flush與所有片段終局後送出，status=completed或completed_with_errors |
| `session.cancelled` | request_id | 此事件後禁止任何新 final |
| `pong` | request_id | transport仍存活，不保證模型ready |
| `error` | code、message、retryable、request_id | session/協定級錯誤 |

```json
{
  "type":"transcript.final",
  "session_id":"session-uuid",
  "event_id":7,
  "segment_id":"segment-uuid",
  "segment_index":0,
  "revision":1,
  "start_sample":3200,
  "end_sample":51200,
  "timestamp_quality":"segment",
  "text":"這份 PR 已經 merge 了。",
  "raw_text":"這份 PR 已經 merge 了。",
  "audio_ms":3000,
  "queue_ms":10,
  "inference_ms":430,
  "warnings":[]
}
```

同 session 的 segment_index 按來源順序由0遞增；final/error/skipped 依此順序交付，一個 segment 恰有一個終局事件。客戶端以 `(session_id, segment_id)` 去重，只有final可正式注入文件。v0.1不送partial；P2a的partial只更新自有預覽或有所有權的組字區，依下列契約。final之後不再改字。

terminal event 送出後正常close1000。client unexpected disconnect：v0.1 丟棄未提交 buffer，取消未開始工作；正在推論可完成但結果丟棄。不得宣稱 resume。15秒 server WS ping，30秒無 pong 切斷；連續120秒沒收到音訊或控制訊息可 idle close。活躍靜音 frame 不算 idle。

### 慢 client 與故障

outgoing events 最多256項或1MiB，先到為準。flow/ACK可合併成最新值，final/error不可丟棄。超限持續5秒時：ephemeral 回 error（若可送）後close1013；durable v0.2 已保存事件供 replay。推論失敗回 segment.error，接收與控制仍可用；worker重啟時縮停輸入窗口直到可處理。

| 錯誤 | HTTP | WS處置 |
|---|---|---|
| unauthenticated／forbidden_origin | 401／403 | upgrade拒絕 |
| invalid_audio／unsupported_option | 422 | error；協定損壞close1008 |
| payload_too_large | 413 | close1009 |
| queue_full／session_limit | 429 | start拒絕或flow pause；超配close1013 |
| model_loading／model_unavailable | 503 | start拒絕；既有session送狀態相關error |
| inference_failed／inference_timeout | 500／504 | segment.error；必要時worker復原 |
| timeline_gap | 409 | 機器睡眠等原因使 sample clock 出現缺口；送 error 後close1012，client 須開新 session |
| storage_full（v0.2） | 507 | 停止 durable ACK、error、close1013 |

## P2a／v0.1.1：可修訂預覽擴充

此節是07所描述功能的wire契約。server完成驗收後hello/capabilities宣告 `protocol_version="1.1"`、`partial_transcripts=true`；仍接受省略新欄位的1.0 client，且不向這些client送新增事件。`native_audio_streaming`維持false。`context_biasing`另外宣告，預設false，不能由partial_transcripts推定支援。

session.start增加可選 `transcript_mode="final_only"|"revisable"`，省略等於final_only。明確要求revisable但server不支援時，回unsupported_option，不能靜默當成功；client可自行提示並重新以final_only建立session。

```json
{"type":"session.start","request_id":"start-1","profile":"continuous","audio":{"sample_rate":16000,"channels":1,"format":"pcm_s16le"},"language":"Chinese","durable":false,"transcript_mode":"revisable"}
```

session.started增加transcript_mode與 `preview_policy`：`min_audio_ms=800`、`min_interval_ms=800`、`max_preview_audio_ms=8000`，continuous另有 `endpoint_silence_ms=900`、`max_segment_ms=8000`；utterance `max_segment_ms=30000`、endpoint_silence_ms=null。limits反映實際調校後配置，不把示例數值硬稱保證。

preview_policy還含 `context_biasing=false`。若啟用獨立實驗，session.start允許 `context={"use_previous_finals":true,"hotwords":["TEA-ASR"]}`，兩欄必填、無其他欄位；僅在context_biasing=true時接受。省略context表示完全不使用文字提示。hotwords上限32詞、每詞32 code points；超限422等價error，內部prompt總token上限與凍結規則依07。`hotwords` feature只有真正驗證後才能true；context中帶非空hotwords而其feature=false時拒絕。

新增事件：

| type | 欄位 | 語意 |
|---|---|---|
| `transcript.partial` | session_id、event_id、segment_id、segment_index、revision、start_sample、end_sample、text、timestamp_quality | 替換該segment的完整暫定文字；text可為空字串以清除舊猜測 |
| `preview.status` | session_id、event_id、state、reason | state=active/paused；reason=normal/load/long_utterance/backend_error；只影響預覽，不表示停止收音 |

```json
{"type":"transcript.partial","session_id":"session-uuid","event_id":3,"segment_id":"segment-uuid","segment_index":0,"revision":1,"start_sample":0,"end_sample":25600,"text":"我們需要權限","timestamp_quality":"segment"}
```

同segment的後續partial例如revision=2、end_sample=51200、text="我們需要全線停駛"；client整段替換。final使用同segment_id/index，revision嚴格大於該段所有已發布partial，包含04既有final全部欄位。未曾partial的final仍revision=1。server不得用partial更新其他segment或已final範圍。

revisable模式在片段打開時就分配ID/index，因此partial可能早於segment.queued；queued只表示音訊已封口。即使最後no_speech，也必須對已分配ID發segment.skipped。client可從partial建立顯示row，不必先看到speech.started。

事件處理順序：先看segment是否已terminal，terminal後拒絕partial；否則只套用更高revision。final/skipped/error清除該segment暫定狀態。session.cancelled清除所有未final預覽；此後禁止partial及final。對照event_id可診斷缺號，但partial可合併，client不能要求每個revision都收到。

outgoing queue可把同segment尚未送出的partial合併成最新值；final送出前移除該段待送partial。final/error/skipped仍不可丟棄。scheduler淘汰preview與writer淘汰partial不代表丟掉已接收音訊。

P2a先驗收ephemeral；同時要求durable=true且尚未完成P4整合時回unsupported_option。P4完成後在capabilities另外宣告 `durable_revisable=true`；resume時client清除未final預覽，server不重播舊partial，依07保存revision high-water mark並從音訊重建。final保留原本的持久化、順序與去重保證。

HTTP一次性transcription不提供partial；既有client不選revisable，端點與效能行為保持基線。

## v0.2：長檔案 jobs

新增功能完成前 capabilities=false，路徑回404。不只先做一個回假202的入口。

| Endpoint | 契約 |
|---|---|
| `POST /v1/jobs?sample_rate=16000&channels=1&format=pcm_s16le` | raw PCM streamed upload；max 1GiB與4小時取先到，metadata經headers提供；完成落盤才202 |
| `GET /v1/jobs/{id}` | state、已處理sample/總sample、錯誤、warnings；不得用生成token數冒充時間進度 |
| `GET /v1/jobs/{id}/result?format=json\|srt\|vtt` | completed才200，尚未完成409；原始時間軸 |
| `POST /v1/jobs/{id}/cancel` | 冪等202，取消於片段邊界；已取消回相同狀態 |
| `DELETE /v1/jobs/{id}` | completed/failed/cancelled才可刪；active回409 |

job state：`uploading → queued → running ↔ paused_for_live → completed/failed/cancelled`。中斷upload刪除temp，未回202就不能聲稱提交成功。同時最多1 upload、10 queued jobs；磁碟配額見03。長片先抽音、降採樣串流上傳，server不接受任意檔案路徑或URL。

metadata headers：`X-Request-ID`、可選 `X-Content-SHA256`；server計算真hash校驗，錯誤422。可選 `Idempotency-Key` 保留24小時：同key＋同SHA256＋相同options回原job，內容不同409；使用key時必填hash，且首次server仍要驗證完整內容。上傳途中key為reserved，重試回409 upload_in_progress；中斷釋放reservation。

SRT/VTT由辨識segment生成粗字幕，一段一cue，不假裝精準按字切時。長句可供編修；若自動切字分cue，必須另標估算並 opt-in。JSON保留segment sample與timestamp_quality。翻譯功能後續另加，不接受 `translate=true` 就直接更改 ASR language。

## v0.2：durable meeting 與 resume

session.start 的 `durable=true` 開啟spool與事件交易。server回session ID＋只屬於此session的resume token（不得寫log）；後續 ACK 的 persisted_seq/sample 是已 fsync＋DB commit 的最後完整frame。

client 本地 spool 保留所有尚未 durable ACK 的 frames；收到 persisted watermark 才可刪除。每次 ACK 必須單調、不可超過received watermark。重連後首先送：

```json
{"type":"session.resume","request_id":"resume-1","session_id":"session-uuid","resume_token":"secret","last_event_id":17}
```

server驗證TTL與單一writer lease，踢除／拒絕競爭writer須明確回409等價錯誤；回 `session.resumed`、next_seq、next_sample、send_until_sample。client從server要求的seq重送；只允許重送已知同seq同hash的frame（忽略且ACK），同seq不同內容回conflict。重新連線不得沿用舊的flow window。

server重播 `last_event_id` 之後已持久化的文字／片段終局事件；flow/pong/received ACK不重播，event ID可有缺號。`last_event_id`超出server已知範圍回invalid_cursor；超出事件保留期回resume_expired。傳輸為at-least-once，client按event/segment ID去重，不承諾網路exactly-once。

若client暫存耗盡而漏錄，不能跳seq繼續送。先結束舊session，再以共同recording ID建立新session並記錄wall-clock gap；後續多軌recording manifest須保留各session offset與gap。v0.2交付時需定義此manifest schema並驗收，未完成前僅宣稱單session續傳。

## 契約測試要求

P1把本文件的v0.1欄位實作為Pydantic models，匯出OpenAPI、WS JSON Schema與可執行golden dialogues；P2a追加修訂擴充，P4再追加v0.2。驗證格式拒絕、sample clock、無聲片段、stop barrier、取消競態、慢client、超量、斷線、worker crash，以及partial替換／亂序／final不可回改。修改wire契約時同步版本與範例，不能只改server。
