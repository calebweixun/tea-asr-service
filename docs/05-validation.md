# 05｜模型驗證與驗收

> **目前沒有本案實測數據。** 以下是測試方法、初始目標與開發關卡。未達標要報告原因與調整，不可把目標寫成成果。

## P0：最先解除的技術風險

在原生Apple Silicon、arm64 Python 3.12環境做spike，不先開發完整server。

1. 記錄chip、RAM、macOS、Python、電源模式、mlx/mlx-audio版本、模型revision與其他lock版本。參考目標先採M1/M2級16GB；使用者實機未知，不能代填通過。
2. 固定 [02](02-research.md) 的模型snapshot與mlx-audio候選版本，產生下載資產hash manifest。只下載必要權重／tokenizer／config，不執行不明遠端程式。
3. 比較原版loader與局部predicate修正；strict load、keys/shapes與量化位元檢查。檢查tokenizer本機載入路徑，確保不依賴未審閱remote code。失敗保留確切例外，不以strict=False繞過。
4. 以numpy float32 16kHz輸入中文、中英混合、短詞、長句；驗證generate結果及錯誤處理。确认模型輸出不含提示模板／控制token。
5. 量測冷載入、warmup、5秒／15秒／30秒推論，RTF、API外的worker峰值、統一記憶體壓力；至少每組30次暖推論。
6. 驗證ONNX VAD資產、16kHz輸入尺寸、recurrent state reset；跨session資料不得污染。
7. 執行最小lock解析，記錄安裝體積、downloads、實際import鏈；Torch若被間接引入，查原因並移除不必要依賴，或提出明確體積取捨。
8. 關網路後用已準備模型成功轉錄。worker停止後確認GPU／程序資源可回收。
9. 為P2a提前做累積音訊重辨識spike：同一句逐次增加0.8秒，直到8秒並再跑final。記錄每次文字修訂與總RTF，確認後文消歧能力、preview非搶占耗時與記憶體。不可只用單次推論速度推定串流體驗；完整驗收見 [07](07-contextual-streaming.md)。

產出：`docs/benchmarks/p0-report.md`、`benchmarks/results/p0.json`、`uv.lock`、`models.lock.json`、可重跑benchmark script。報告分「來源宣稱」「本機結果」「未測」。P0通過前，API與UI只能做mock契約，不得宣稱指定模型可用。

`models.lock.json` 至少記錄repo、revision、必要檔名與sha256、license來源、mlx-audio版本、相容層版本、VAD來源/hash。它不是虛構的預填通過證書。

## 測試音訊

建立取得使用授權的固定corpus，含人工校訂逐字稿、來源、語言、採樣率、hash；私人會議音訊不進Git。至少：

| 類型 | 最少數量／長度 | 要檢查什麼 |
|---|---|---|
| 台灣華語 | 30段、每段3–15秒 | 繁體字、詞彙、人名、數字 |
| 中英混用 | 30段、每段3–15秒 | PR/merge/client/API等英文保留 |
| 極短詞 | 20段、0.2–1秒 | 好／對／OK，不被VAD吃掉 |
| 靜音／風扇／鍵盤／音樂 | 每類至少60秒 | VAD false positive、ASR幻覺 |
| 長句與停頓 | 10段、30–90秒 | hard split邊界漏字、重複字 |
| 會議 | 60分鐘連續流 | 缓衝、記憶體、checkpoint與gap |
| 影片 | 至少30分鐘 | 抽音後時間軸、字幕起迄、句尾 |

不要宣稱TEA天然免疫幻覺。無聲抑制與VAD可降低錯誤，但仍要測弱音、背景人聲與樂曲。

## 初始性能目標

數值為產品目標，需P0以指定硬體校正。未指定硬體的數字不能跨Mac宣傳。

| 指標 | 初始門檻 | 量測方式 |
|---|---|---|
| warm短句RTF | p95≤0.5 | inference elapsed/audio duration；5秒與15秒分組 |
| 5秒utterance end-to-final | p95≤1.5秒（無其他負載） | client最後樣本送出至final收到，不含說話時間 |
| continuous end-of-speech-to-final | p95≤2秒（單session） | 含500ms端點等待、queue、inference、傳輸 |
| ready模型閒置RAM | 目標≤3GiB | API＋worker程序RSS、MLX active/cache另列，不重複相加作實體總RAM |
| 峰值memory | 目標≤4GiB、无持續swap增長 | 30秒最長片段＋continuous壓力測試 |
| idle無音訊CPU | 目標平均<2%單核心 | 60秒，明示工具百分比定義 |
| 卸載後idle服務RAM | 目標≤150MiB | API仍可status，worker不存在；若ORT使其超過須紀錄 |
| API responsiveness | p95<100ms | worker推論中health/ping仍可回應 |
| 連續一小時 | backlog不持續成長 | RTF含VAD、排程、持久化；queue曲線与final lag |

冷下載與冷啟動分開記錄，不混入warm p95。記錄個別sample latency、p50/p95/max，不只平均值。對1秒短詞不能只看RTF，還要看絕對延遲與辨識成功率。

多路能力：各音訊來源的有效推論成本相加，持續使用率須留餘裕（初始目標<70%）；單路RTF<1不足以證明兩路可用。先測一場會議＋每30秒一次5秒utterance＋一個batch job；輸入延遲不合格時減少admission或batch片長。

## 品質指標

- 中文CER、英文WER與混合MER分別報告；保存評估script、正規化規則、樣本數。
- 同時報保留繁體與標點的raw品質，以及內容正規化後品質；不能為了CER好看，把簡繁與數字差異全部隱藏。
- 量化品質最好與同corpus的上游／未量化結果比較。若沒有基線，明示沒有，先保存MLX結果作後續回歸基線。
- 初始回歸門檻：固定corpus整體MER退步不超過1個百分點，任何關鍵英文／短詞案例變差需個別檢視。這不是承諾模型絕對MER低於某值。
- 分段版對整段版比較boundary附近漏字／重複字；不得只驗證JSON schema就宣稱字幕品質合格。
- 人工抽查至少30個cue的起迄，記錄誤差分布；若超出字幕可用要求，降級為粗字幕並啟動alignment研究，不能生成假的word timestamps。

## 可靠性測試與必要證據

| 場景 | 必須看到的結果 |
|---|---|
| 模型缺失／quant不相容 | ready=false、具體錯誤；沒有mock fallback |
| 48kHz假裝16k／stereo／WAV header | 可驗的metadata不符拒絕；無法從raw可靠推斷的錯誤需client測試，不宣稱能自動偵測所有謊報 |
| WS沒start就送binary、seq跳號、odd bytes | 明確protocol error與close，不污染其他session |
| 連續說話超8秒／手動超30秒 | 前者安全切段；後者拒絕超上限且不無限RAM |
| 請求塞滿queue | 有backpressure/429，所有accepted資料有final/error/skipped或明確取消 |
| 慢reader | bounded outgoing queue，ephemeral斷開；durable可重播 |
| ASR卡住／殺掉worker | API仍能health；當前段error、其他段待恢復；只重啟一份模型 |
| 快速cancel與final競態 | 終局狀態一致；cancelled後不冒出final |
| 連續段＋語音輸入 | session不串音、不亂序；無全域hotword污染 |
| macOS sleep/wake | 明確interrupted/gap；不把睡眠期間算成已錄音 |
| durable ACK後server crash | 音訊仍可讀；final資料與event交易一致 |
| fsync前crash／磁碟滿 | 不發虛假durable ACK；client持有可重送音訊 |
| resume重送／不同內容同seq | 相同內容去重、不同內容conflict |
| 反覆worker重啟／idle卸載 | 無殭屍程序；記憶體沒有階梯式永久增長 |
| LaunchAgent與手動同時啟動 | 只有一份模型，第二份報service已運行 |

在mock測試可以控制inference延遲與故障；真模型測試須由Apple Silicon執行，不以Linux CI綠燈取代。真模型不可用時可以提交mock測試，但交接明示未驗證項目，不能把階段標成完成。

## 不達標時的決策順序

P2a另依07驗收首次可見延遲、文字抖動、錯改對／對改錯率、final品質與混合負載。先做final-only對照；revisable開啟後的900ms端點等待需計入final lag，不能沿用500ms端點測試來宣稱相同延遲。

1. 先查錯誤載入、重複模型、非arm Python、過長片段、token上限與無效VAD。
2. 校準切段與降低同時工作數；先保證單路可靠。
3. 檢視MLX／依賴版本；升版必須重跑固定corpus與記憶體測試。
4. 若指定模型仍不符合目標，提出TEA-mini或其他量化的比較實測與品質取捨，讓產品擁有者決定；不靜默換模型。
5. 只有證據顯示Python/API層占比顯著，才評估Rust／Swift重寫；優先處理模型與排程成本。
