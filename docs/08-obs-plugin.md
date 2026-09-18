# 08｜OBS 直播即時字幕外掛：交接規格

> 本文件是交接規格，**此 repo 不含任何 OBS 程式碼**。依 [06](06-handoff.md) 的決定，正式 C++ 外掛另開 repo，
> 避免 server release 綁住多平台原生編譯。此 repo 只保留協定與 reference client。
>
> 本機事實查核日期：2026-09-19，OBS Studio 32.2.1 (macOS, Apple Silicon)。下列 API 與 plugin id 皆由安裝版本實際確認。

## 目標

在 OBS 內做到：直播時把本機 TEA ASR 的辨識結果即時疊在畫面上；使用者能從 OBS 選單開設定視窗，
調整字體、大小、顏色、描邊、陰影、位置、行數與捲動行為。行為要像原生 OBS 來源，不是外掛硬塞的浮動視窗。

## 為什麼不是 browser source

browser source ＋ 本機網頁是最快的做法，但它把 CEF 一整個 render process 綁進字幕路徑，
延遲與 CPU 都不必要，且字體設定會變成網頁 CSS 而不是 OBS 原生屬性面板。
使用者要的是「真正的專業外掛」，所以走 libobs source + Qt 設定視窗。

## 元件

```
obs-tea-asr-captions/            # 另一個 repo，從 obsproject/obs-plugintemplate 開始
  src/
    plugin-main.cpp              # obs_module_load：註冊 source、Tools 選單
    captions-source.cpp/.hpp     # obs_source_info：屬性、render、字幕狀態機
    audio-tap.cpp/.hpp           # 從指定音訊源取樣、resample、進 ring buffer
    asr-client.cpp/.hpp          # WebSocket client：協定狀態機、重連
    settings-dialog.cpp/.hpp     # QDialog：server URL、token、連線狀態、診斷
    ring-buffer.hpp              # 單生產者單消費者，音訊 callback 不配置記憶體
  data/locale/en-US.ini, zh-TW.ini
  buildspec.json, CMakeLists.txt, CMakePresets.json
```

### 1. 音訊來源

用 `obs_source_add_audio_capture_callback()` 掛在使用者選的音訊源（麥克風或桌面音訊），
不要自己開 CoreAudio，否則裝置選擇、靜音與增益會和 OBS 的混音器不一致。

```c
obs_source_add_audio_capture_callback(source, on_audio, self);
// void on_audio(void *param, obs_source_t *src, const struct audio_data *data, bool muted)
```

**這個 callback 在音訊執行緒上，絕對不能阻塞。** 它只做一件事：把 planar float 資料寫進固定大小的
ring buffer。resample、編碼、WebSocket 送出全部在自己的 worker thread 做。
Ring buffer 滿了就丟最舊的並累計 drop 計數，在設定視窗顯示——不要靜默丟音訊，也不要在音訊執行緒上等鎖。

重採樣用 libobs 自己的 `audio_resampler_create()`，目標 `AUDIO_FORMAT_16BIT`、`SPEAKERS_MONO`、16,000 Hz。
`muted` 為 true 時仍要送靜音樣本：server 的 VAD 靠連續的來源時間軸判斷句尾，缺口會讓 sample clock 對不上。

### 2. WebSocket client

**OBS 32.2.1 bundle 的 Qt 只有 QtCore/QtGui/QtNetwork/QtSvg/QtWidgets/QtXml，沒有 QtWebSockets。**
（已確認：`/Applications/OBS.app/Contents/Frameworks/`）所以不能直接 `#include <QWebSocket>`。三個選項：

| 做法 | 評估 |
|---|---|
| 靜態連結輕量 C++ WS library（ixwebsocket 等） | **建議**。只連本機、不需 TLS，體積小，簽章單純 |
| 用 QTcpSocket 自己實作 RFC 6455 | 可行且無新依賴，但 framing／ping／close 要自己測對 |
| 隨外掛 bundle QtWebSockets.framework | 要處理 rpath 與公證，最重 |

協定完全依 [04](04-api.md)：`ws://127.0.0.1:8327/v1/stream`、`Authorization: Bearer <token>`、
`profile="continuous"`、16-byte binary header（uint64 LE `seq` ＋ uint64 LE `start_sample`）、
單一 frame PCM 上限 6,400 bytes。**務必遵守 `send_until_sample` 流控窗口**，超出會被 server 視為
protocol error 並關閉連線。

token 讀 `~/Library/Application Support/TEA ASR/token`；設定視窗要能覆寫路徑，但不要把 token 寫進
OBS 的 scene collection JSON（那份檔案使用者常常分享）。

連線狀態要可見：`hello` 的 `model_state`、`session.started`、斷線與重連都反映到設定視窗與 source 的
狀態指示。重連採退避，**不要無限快速重連把失敗藏起來**。

### 3. 字幕狀態機

- `transcript.partial`：整段替換該 `segment_id` 的暫定文字。只會更新自己的預覽，不寫進已定稿的行。
- `transcript.final`：該段定稿，之後不再改字。
- `segment.skipped` / `segment.error`：清掉該段的暫定狀態，不要留著半句話。
- `session.cancelled`：清掉所有未定稿預覽。
- 以 `(session_id, segment_id)` 去重；只依 `revision` 遞增套用，terminal 之後拒絕 partial。

顯示模型：保留最近 N 行已定稿文字（預設 2），最後一行是目前這段（可能是 partial）。
partial 可用較低不透明度或不同顏色，讓觀眾知道還會變。**不要為了顯示 partial 而回刪已經定稿的字。**

`partial_transcripts` 目前預設 true，但使用者可以關掉。外掛仍要先讀 `hello` / `/v1/capabilities`，
只有在 server 宣告 true 時才要求 `transcript_mode="revisable"`；否則以 final-only 運作，
不要假設一定有預覽。

### 4. 文字算繪與外觀設定

不要自己寫 freetype 排版。OBS 已經有 `text-freetype2` 外掛，用 private child source 驅動它：

```c
// id 在 32.2.1 實際為 "text_ft2_source"；部分版本註冊為 "text_ft2_source_v2"。
// 啟動時依序嘗試 v2 再退回，取不到就明確報錯，不要靜默不顯示。
text = obs_source_create_private(id, "tea-asr-captions-text", settings);
```

它的 settings key（由安裝版本的 binary 確認）：
`text`、`font`（含 `face`/`size`/`flags`/`style`）、`color1`、`color2`、`outline`、`drop_shadow`、
`word_wrap`、`custom_width`、`antialiasing`。把這些原封不動透過 `obs_properties` 暴露給使用者，
外掛自己只再加：最大行數、partial 的顏色與透明度、行距、內距、對齊、定稿後保留秒數。

位置交給 OBS 原本的 scene item transform（使用者拖曳、對齊工具都能用），
**不要自己做一套座標設定**——那會和 OBS 的變換衝突，也不符合原生外掛的操作習慣。
只在 source 內部提供對齊（左/中/右）與內距。

render 時 `obs_source_video_render(text)`，`get_width`/`get_height` 轉發子 source 的尺寸。

### 5. 設定視窗

用 `obs_frontend_add_tools_menu_item("TEA ASR 字幕設定…", cb, nullptr)` 掛在 Tools 選單，開一個
`QDialog`（parent 用 `obs_frontend_get_main_window()`）。這裡放**全域**設定：server URL、token 路徑、
音訊來源選擇、連線狀態、掉幀統計、重連按鈕、協定版本與 capabilities 顯示。

**每個 source 的外觀**留在 OBS 原本的來源屬性面板（`obs_source_info::get_properties`），
不要把兩者混在同一個視窗——專業外掛的使用者預期屬性在屬性面板。

所有字串走 `obs_module_text()` 與 `data/locale/*.ini`，至少 en-US 與 zh-TW。

### 6. 直播用的真字幕（建議一併做）

疊在畫面上的是「燒進畫面」的字幕。OBS 另有真正的 CEA-608 closed caption 輸出：

```c
obs_output_output_caption_text2(output, text, display_duration);
```

（已確認 `obs_output_output_caption_text1` / `text2` 存在於 32.2.1 的 libobs。）
提供一個開關，把 final 文字同時送進串流的 caption 軌，讓平台端能開關字幕。
只送 final，不要送 partial——closed caption 沒有「整段替換」的語意。

## 執行緒與不可破壞的約束

1. 音訊 callback 只寫 ring buffer：不配置記憶體、不上鎖等待、不做網路 I/O。
2. WebSocket 與 resample 在自己的 worker thread；graphics callback 只讀已算好的字串。
3. 字幕字串以 mutex 或 double buffer 交給 render thread，render 不等網路。
4. 每層 buffer 有上限與可見的丟棄計數。
5. server 不可用時 source 顯示明確狀態，不要留著上一句假裝還在運作。
6. 不要在外掛裡重做 VAD 或斷句——那是 server 的職責，重做會讓兩邊的 sample clock 不一致。

## 建置與發行

從 [obsproject/obs-plugintemplate](https://github.com/obsproject/obs-plugintemplate) 開始，沿用它的
`buildspec.json` 與 CMake presets。macOS 官方流程需要**完整 Xcode**（template 的 macOS preset 使用
Xcode generator）。本機已備妥：Xcode 27.0，`xcode-select -p` 指向 `/Applications/Xcode.app/Contents/Developer`。

外掛裝到 `~/Library/Application Support/obs-studio/plugins/`。發給別人要 codesign ＋ notarize，
否則 Gatekeeper 會擋。CI 用 template 附的 workflow。

## 驗收條件

1. 直播中講話，字幕在畫面上出現並隨後文修訂，final 之後不再改字。
2. 改字體、大小、顏色、描邊、對齊、行數立即反映，OBS 重啟後設定保留。
3. 拔掉 server（或 server 未啟動）時 source 顯示明確狀態，OBS 不當機、不卡住、不掉幀。
4. 連續直播一小時：記憶體不持續成長，音訊無累積延遲，掉幀計數為 0 或有明確原因。
5. 切換場景、隱藏／顯示 source、重新選音訊來源都不會殘留 session 或洩漏 callback。
6. scene collection 匯出檔不含 token。
7. 與 server 的協定測試：flow control、seq 連續性、斷線重連後以新 session 開始（v0.1 不支援 resume）。

## 先做概念驗證的選項

若要先看效果再投入原生外掛：寫一個 Python bridge，連本機 WS 取 final，透過 obs-websocket
（OBS 已內建 `obs-websocket.plugin`）更新一個既有的文字來源。幾小時內就能在直播畫面看到字，
用來驗證延遲與排版是否可接受。它**不是**交付物，外觀與延遲都劣於原生外掛，驗證完就丟。
