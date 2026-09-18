# 01｜產品規劃

> 決策稿 v0.1 · 2026-09-18 · 給產品擁有者與實作模型

## 一句話定位

把 Mac 變成個人的語音辨識引擎：開一次服務，多種工具都能送音訊、取得文字與時間區間，模型只載入一次。

設計優先順序為：**正確與不漏音 → 可理解的失敗 → 即時體驗 → 記憶體與安裝體積 → 額外功能。**「輕量」必須同時量測常駐 RAM、推論峰值、閒置耗電、依賴體積與使用步驟，不能用 Rust 執行檔大小代表整個系統。

## 範圍

### v0.1：可供 client 使用的辨識核心

1. Apple Silicon 原生執行；CLI 啟動、診斷、模型準備與狀態查詢。
2. 一次性 PCM HTTP 辨識，以及 WebSocket 按鍵式／連續式收音。
3. TEA-ASR 混合量化相容層、常駐單模型、明確 readiness。
4. 有上限的緩衝與排程、工作取消、推論逾時、worker 復原。
5. 語音活動偵測（VAD）切段、不可變 final、來源時間區間。
6. 最小 Python CLI 範例 client、協定與真模型測試。

### v0.1.1：邊說邊修訂（P2a）

把正在說的一段話顯示為可替換預覽，累積更多音訊後利用後文修訂，停頓／放開按鍵才定稿。優先使用同一ASR模型重辨識，不增加常駐LLM。規劃包含修訂事件、有限音訊窗口、負載降級與client安全更新；詳見 [07](07-contextual-streaming.md)。這是必要產品里程碑，效能須驗證；final-only仍保留。

### v0.2：長檔案與會議可用

檔案 jobs、SQLite checkpoint、重新啟動後續跑、SRT／VTT／JSON 匯出、durable meeting session、原始音訊保留政策、LaunchAgent 安裝。完成此階段才宣稱可長時間保存會議紀錄。

### v0.3：產品 client 與選配功能

Swift 選單列語音輸入 client、OBS 整合、會議錄音 client、字幕編修與翻譯 adapter。Mac先做浮動預覽修訂（P5a），再做InputMethodKit組字區內直接修訂（P5b）。精準對齊、diarization另有驗證關卡。

### 暫不納入

Intel Mac 推論、Windows server、多人伺服器叢集、帳號系統、雲端同步、模型訓練、任意插件執行、預設雲端上傳，以及第一版完整 InputMethodKit 輸入法。LAN 可於後續增加受保護入口；v0.1 只服務本機。

## 四種用途如何共用服務

| 用途 | Client 負責 | Server 負責 | 注意事項 |
|---|---|---|---|
| macOS 語音輸入 | 熱鍵、AVAudioEngine、預覽替換、final貼入；後續IME組字 | utterance／continuous辨識、partial修訂、final定稿 | P5a浮動預覽；P5b正式IME，均共用server |
| 影片字幕／翻譯 | 選檔、FFmpeg 抽音、時間軸、編修、翻譯設定 | batch 辨識、來源時間、SRT/VTT、可選翻譯 job | TEA-ASR 負責轉錄；翻譯由另一 provider 負責 |
| OBS 即時字幕 | 音訊 filter 擷取、非阻塞 ring buffer、畫面呈現 | continuous 辨識、分段事件、負載資訊 | 先用 bridge＋文字來源驗證，再做原生 plugin |
| 背景會議紀錄 | 麥克風／系統音訊、開始停止、錄音狀態、離線暫存 | durable session、逐段保存、續傳、匯出 | 持續錄音不等於聲紋分人；來源軌與說話者不可混稱 |

多個 client 連線不代表多路 GPU 同時推論。預設先驗證「一個連續 session，加一個偶發語音輸入」；未達效能關卡就限制連續 session 數量，不能無條件宣稱多路即時。

## 使用體驗

以下是將來應達成的命令介面，現在尚不可執行：

```text
tea-asr doctor
tea-asr model prepare
tea-asr serve
tea-asr status
tea-asr transcribe sample.wav
tea-asr service install       # v0.2，使用者選擇登入自啟
tea-asr service uninstall
```

第一次下載模型時顯示大小、進度、快取路徑；下載中斷可重試。缺模型時 `serve` 明確提示 prepare，不在背景無限下載。完成準備後，辨識在斷網環境仍應可用。

狀態只用少數明確文字：尚未準備、載入中、可辨識、忙碌、需要處理。出錯時提供原因與一個建議動作；不讓使用者判讀 Python traceback。

預設不保存語音輸入歷史。會議 client 開啟 durable session 時，清楚顯示保留位置與停止按鈕。轉錄文字不寫入一般 log。錄音權限由 client 請求，server 無需麥克風權限。

## Client 後續技術建議

- **Mac client：** Swift／SwiftUI、AVAudioEngine；系統音訊走 ScreenCaptureKit，依實際最低 OS 驗證授權與 API availability。輸入注入須測試輔助使用權限、剪貼簿還原、中文組字中狀態與安全輸入欄位。[Apple ScreenCaptureKit](https://developer.apple.com/documentation/ScreenCaptureKit)
- **影片工具：** 先 CLI 與 sidecar 字幕；不要第一版內建影片編輯器。FFmpeg 是選配工具，不是純 PCM 辨識服務的必要依賴。
- **OBS：** 官方 plugin 介面為 C/C++；用獨立網路執行緒把 PCM 送 server，禁止在 audio callback 等待網路／ASR。Browser Source 只能呈現，仍需要明確音訊擷取來源。[OBS plugin 文件](https://docs.obsproject.com/plugins)
- **會議：** 最初可合成 mono 送辨識，但保留來源資訊。若分兩軌，分別建立 session 與共同 recording ID；時鐘校準、回音與重複音訊消除屬 client 工作。

## 尚待使用者實際環境確認

Mac 晶片／RAM／OS、是否需要其他裝置經 LAN 接入、影片主要語言與翻譯目標、會議保存期限。這些不阻擋核心開發：先採 Apple Silicon、16GB 參考機、本機入口、中文含英文混用、無自動翻譯、durable 原始音訊 24 小時與逐字稿 7 天的可調預設。保存期限只在使用者啟用 durable 功能後生效。
