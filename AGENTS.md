# Project Agent Instructions

## Collaboration and ownership

- For all future development work and file modifications, delegate implementation to a subagent whenever subagent execution is available. The root agent is the high-level technical lead: it owns planning, task decomposition, coordination, review, integration, verification, and status reporting.
- The root agent may perform read-only inspection and the necessary Git integration steps, including staging, committing, and pushing changes. Implementation changes should be made by the delegated subagent, unless a tool limitation makes that impossible and the user explicitly approves an exception.
- Prefer delegating implementation to `gpt-5.6-luna` with `max` reasoning when that model/reasoning combination is available. Do not use the Terra model for this project.
- Keep delegated work scoped, independently verifiable, and reported back to the root agent before integration. Do not claim completion without running appropriate validation.

## Codebase discovery

This project uses codebase-memory-mcp to maintain a knowledge graph of the codebase. Prefer MCP graph tools over grep/glob/file-search for code discovery.

Priority order:

1. `search_graph` — find functions, classes, routes, and variables by pattern
2. `trace_path` — trace who calls a function or what it calls
3. `get_code_snippet` — read specific function/class source code
4. `query_graph` — run Cypher queries for complex patterns
5. `get_architecture` — high-level project summary

Fall back to `rg`/glob for string literals, error messages, configuration values, non-code files, or when graph tools are insufficient. Run `index_repository` first if the project is not indexed.

## Communication

The project owner has ADHD. Keep plans, progress updates, blockers, and handoffs concise, concrete, and easy to scan.

## 開發迴圈與驗收

每一次改動都要跑完整個迴圈才算一輪；沒跑完不要回報完成，也不要開始下一件事。

### 迴圈

1. **先讀當前狀態** — `git status`、相關 docs 段落、要改的檔案。不要從記憶或前一輪的假設開始。
2. **記錄基準** — 動手前先跑一次該層的測試並記下數字（例如「65 tests, 0 failures」）。沒有基準就無法證明自己沒有弄壞東西。
3. **小步改動** — 一次一個可獨立描述的改動。同時改三件事時，失敗了就無法定位是哪一件。
4. **跑驗證** — 見下方「各層的驗證指令」。以退出碼與輸出為準，不以「看起來對」為準。
5. **紅燈就停** — build 或測試失敗時，先修到綠再繼續；不要堆下一個改動上去。
6. **同一種做法最多重試兩輪** — 第三輪強制換路：換做法、升級模型、或回來問人。症狀完全不變（錯誤訊息一字不差）代表改的地方不在因果鏈上，重試沒有意義。
7. **收尾同步文件** — 改變了「能做什麼」就要更新 `docs/06-handoff.md`、`docs/10-session-handoff.md` 與對應的 README。文件與實作不一致等同未完成。

### 各層的驗證指令

| 改到哪裡 | 一定要跑 |
|---|---|
| `src/tea_asr/` | `uv run pytest tests/unit tests/integration -q` |
| `src/tea_asr/`（提交前） | `uv run ruff check .` |
| `clients/macos/` | `cd clients/macos && swift build && swift test` |
| `clients/macos/` 的 app bundle 行為 | `cd clients/macos && ./scripts/build-app.sh` |
| 需要真實模型的行為 | `uv run pytest -m hardware`（預設不跑，Apple Silicon 才有意義） |

沒有測試涵蓋你改的行為時，先補一個最小煙霧測試再改，不要靠人工目視當作驗證。

### 驗收：什麼叫做完成

同時滿足才算完成，缺一條就是未完成：

1. 驗收條件**逐條**核銷，每條標 PASS/FAIL 並附證據（`檔案:行號` 或指令輸出）。
2. 產出經過**非產出者**驗證——由 fresh context 的 agent 驗收，或至少 read-back 重讀全檔確認。同一個 context 會繼承同樣的盲點。
3. 沒有隱藏的「暫時先這樣」：所有 TODO、hack、跳過的 case 明列在回報裡。
4. `git diff --stat` 與意圖一致，沒有意外改到別的檔。

「改好了，應該可以」一律視為未完成。

### 不准假裝驗過的事

這個 repo 有幾類行為 agent 在 CLI 環境**無法**驗證，只能設計成請使用者跑的步驟：

- macOS GUI 的視覺結果（排版、跳動、焦點、overlay 是否搶前景 app）
- 全域快捷鍵與 push-to-talk 跨 app 的實際行為
- TCC 權限（麥克風、輔助使用、輸入監控）在真機上的授權流程
- 真實模型的延遲與辨識品質

碰到這些，回報裡要開一個「需要使用者用眼睛確認」清單，寫清楚該看什麼、怎麼看，並明說這部分未驗證。**不得**因為程式碼看起來正確就標成已驗收；`docs/06-handoff.md` 的完成狀態也要照這個標準寫，實作完成與實機驗收要分開記。

### 派工時要附的三件套

委派給 subagent 時，缺一不發：

1. **目標與動機** — 做什麼＋為什麼。動機讓 agent 遇到規格沒寫到的歧義時能自行做對取捨。
2. **驗收條件** — 可機械核銷。判準是「一個沒看過這段對話的人能機械地判 pass/fail」。「功能正常運作」不合格；「`swift test` 綠且數量 ≥65」合格。
3. **回報格式** — 明確規定回什麼、不回什麼。預設：結論 ≤10 行、關鍵證據附 `檔案:行號`、長產物落檔只給路徑、「未解決／不確定」清單（可為空，欄位必留）；禁止貼整段檔案或完整工具輸出。

驗收 agent 要拿到**原始派工的原文**而不是產出者的自述，否則驗收者會被產出者的框架帶著走。
