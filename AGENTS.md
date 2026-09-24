# Project Agent Instructions

## Collaboration and ownership

- For all future development work and file modifications, delegate implementation to a subagent whenever subagent execution is available. The root agent is the high-level technical lead: it owns planning, task decomposition, coordination, review, integration, verification, and status reporting.
- The root agent may perform read-only inspection and the necessary Git integration steps, including staging, committing, and pushing changes. Implementation changes should be made by the delegated subagent, unless a tool limitation makes that impossible and the user explicitly approves an exception.
- Prefer delegating implementation to `gpt-5.6-luna` with `max` reasoning when that model/reasoning combination is available. Do not use the Terra model for this project.

### 怎麼呼叫 gpt-5.6-luna

這台機器裝了 codex CLI（`~/.local/bin/codex`，已用 ChatGPT 登入，預設 `gpt-5.6-sol` / medium）。非互動派工：

```bash
codex exec -m gpt-5.6-luna -c model_reasoning_effort=max -s workspace-write "<派工內容>"
```

- `-s read-only` 只讀不改，`-s workspace-write` 可改工作區檔案。不要用 `--dangerously-bypass-approvals-and-sandbox`。
- 派工內容長的時候寫成檔案再 `- < file.md`，避免 shell 引號問題。
- 跑很久，用背景執行並把輸出導到檔案。
- codex 是獨立 context，正好可以當「不自驗」的產出方；驗收仍由上游自己重跑 build／test 與截圖確認，不直接採信它的回報。
- 多個 agent 併行時，在派工單裡明確列出**允許修改**與**嚴禁修改**的檔案清單，否則會互相覆蓋。

### 怎麼呼叫 gemini-3.8-flash

這台機器也裝了 agy CLI（`~/.local/bin/agy`，v1.2.7）。模型名稱把 effort 內建在名字裡，用 `agy models` 看完整清單，常用的是 `gemini-3.8-flash-high` / `-medium` / `-low`。

```bash
agy --model gemini-3.8-flash-high --mode accept-edits -p="<派工內容>"
```

- **flag 順序有講究**：`-p` 要用 `-p="..."` 附值，`--model` 放在 `-p` 前面，否則 `-p` 會把 `--model` 當成 prompt。
- `--mode accept-edits` 讓它可以改檔案，`--mode plan` 只規劃不動手。
- headless（`-p`）模式下無法互動核准工具權限，**需要事先在 agy 的 `settings.json` 的 `permissions.allow` 加白名單**；否則工具呼叫會被自動拒絕，結果是「no output produced」。不要用 `--dangerously-skip-permissions` 繞過。

### 誰做什麼

- **高階模型（主對話／orchestrator）負責規劃、派工、驗收、整合、回報，不下場實作。**
- **實作交給 `gpt-5.6-luna`（codex，max reasoning）或 `gemini-3.8-flash`（agy）。**
- 選誰：需要推理的（診斷 bug、架構取捨、需求有歧義）給 luna max；模式已知的機械修改（改名、套用既有慣例、批次調整）給 gemini flash。
- 驗收一律由派工方自己重跑 build／test／截圖確認，不直接採信實作方的回報。
- **只有 root agent 可以派工。被派出來的 subagent 必須自己實作，不得再用 `codex exec`、`agy` 或任何 CLI 往下委派。** 巢狀派工會變成上游看不到、也控制不了的工作：2026-09-24 發生過一次，subagent 讀到上面「實作交給 luna」就自己派了 codex 任務，而 codex 額度早已用完，任務一啟動就失敗，subagent 卻一直等一個不存在的結果，worktree 什麼都沒改。派工單裡要明寫這一條。

### 派工一律在獨立 worktree + 分支上進行

**檔案清單只是請求，worktree 才是隔離。** 2026-09-20 實際踩到兩次：一個 agent 改了派工單明文禁止的檔案並回報全數 PASS；另一個在上游還原檔案後又寫回去，等於跟上游搶同一份工作區，最後只能終止它。兩次都是因為所有 agent 共用 `main` 的工作區。

標準流程：

```bash
# 1. 開 worktree 與分支
git worktree add ../tea-asr-wt/<task> -b agent/<task>

# 2. 派工，cwd 指向那個 worktree
cd ../tea-asr-wt/<task> && codex exec -m gpt-5.6-luna -c model_reasoning_effort=max -s workspace-write - < task.md
cd ../tea-asr-wt/<task> && agy --model gemini-3.8-flash-high --mode accept-edits -p="$(cat task.md)"

# 3. 驗收：上游自己在該 worktree 重跑
cd ../tea-asr-wt/<task> && swift build && swift test

# 4. 合併與清理
git -C . merge --no-ff agent/<task>
git worktree remove ../tea-asr-wt/<task>
git branch -d agent/<task>
```

Claude 自己的 subagent 用 Agent 工具的 `isolation: "worktree"` 即可，不必手動開。

這樣換來的好處：agent 之間不可能互相覆蓋；衝突從「靜默競爭」變成「合併衝突」，看得見也審得了；每一路的 diff 天然乾淨，`git diff main...agent/<task>` 就是它的全部產出；出事直接砍分支，不必一個檔案一個檔案還原。

要注意的代價與限制：

- **`.build/` 每個 worktree 各一份**，第一次建置要重跑（Swift 約 25 秒），可接受。
- **GUI 驗證仍然無法併行**。`./scripts/build-app.sh` 產出的 `TEA ASR.app` 與實際啟動的行程是全機唯一的，同時只有一路能做「build → 完全結束 → 重開 → 截圖」。所以需要看畫面的工作要排隊，不要同時派兩個。
- **派工單的檔案清單還是要寫**，但用途變成「界定審查範圍與意圖」，不再是安全機制。agent 越界時看 diff 就知道，不會傷到別人。
- **合併順序要自己排**。同時改 `MainWindowController.swift` 的兩路仍會在合併時衝突，只是衝突是可見且可審的。真的會重疊的工作，還是排隊比較省事。

### 各 agent 的實際能力與使用感受（2026-09-20 實戰記錄）

| Agent | 實測感受 | 適合 | 不適合 |
|---|---|---|---|
| `gpt-5.6-luna`（codex, max） | 診斷能力強，會自己讀第三方開源碼找證據、寫 spike 驗假設。誠實度高：查不到會明說「未證實」而不編造。但**容易越界**，會順手做沒要求的事 | 根因診斷、架構取捨、需求有歧義的實作 | 需要 GUI／螢幕／真實硬體的驗證 |
| `gemini-3.8-flash`（agy） | 樣本還少。headless 權限設定繁瑣（見下） | 模式已知的機械修改 | 需要長鏈推理的診斷 |

### 沙箱是最大的驗證盲區

codex 的沙箱會擋掉一整類操作，而且**失敗的樣子像是「這台機器沒有」而不是「我被擋住」**，agent 很容易誤判：

- `screencapture` 回 `could not create image from display` → 不是沒有螢幕，是沙箱
- `system_profiler SPAudioDataType` 回空、CoreAudio `devices bytes=0` → 不是沒有音訊裝置，是沙箱
- `open` 回 `kLSNoExecutableErr`
- `swift build` 撞 `~/.cache/clang/ModuleCache` 權限；agent 會自己繞出一個 wrapper 檔案，收工要清掉

**所以：凡是要看畫面、碰硬體、驗 GUI 行為的，派工方自己做，不要交給 subagent。** 主對話直接跑 `screencapture` 是可行的，實測有效。已經發生兩次：agent 憑推論做了修法並標成完成，主對話一截圖就證明無效。

### agy 的 headless 權限

- 讀的是 `~/.gemini/antigravity-cli/settings.json`，**不是** `~/.gemini/settings.json`。加錯檔案時 log 會印 `permissions=<nil>`，用 `~/.gemini/antigravity-cli/log/cli-*.log` 確認。
- 規則寫 `command(swift)` 這種裸命令形式，**不要加 `*`**。官方說明是「`git` matches `git add` but NOT `github`」，加了 `*` 反而比對不到。
- 檔案工具要另外開：`read_file(*)`、`write_file(*)`、`edit_file(*)`、`replace(*)` 等。
- 帶 pipe 或 redirection 的指令仍會被拒，派工單要叫它跑單一命令。

### 派工的教訓

1. **先開 worktree，再談檔案清單**。清單界定意圖與審查範圍，隔離靠 worktree。收工仍要 `git diff main...agent/<task>` 逐檔核對。
2. **越界產出不要因為「測試綠」就照單全收**，也不要無腦還原。逐檔審閱，合理就收下並在 commit message 寫清楚來歷。
3. **把推測與實證分開要求**。驗收條件裡明確寫「查不出來就誠實說查不到，列出已排除的可能」，agent 就真的會照做；不寫，它會給一個聽起來合理的原因。
4. **不要讓 agent 自己宣告視覺驗收**。派工單直接寫「這個環境無法驗證視覺結果，不准聲稱已驗收」，並要求它產出「需要使用者用眼睛確認」清單。
5. **背景執行的輸出不要自己重導**到別的檔案，會讓使用者在 CLI 看不到進度。
6. **前一輪的結論被推翻時，新派工單要明講「不要再往那個方向查」**，否則它會重走一遍老路。
7. **踩到 `docs/06-handoff.md` 的既有約束時，先講出來再做**，不要默默繞過。已發生：模型下載、LAN 開放兩件事都撞到明文條件。

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
