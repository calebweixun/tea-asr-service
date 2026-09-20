# CLAUDE.md

這個專案的 agent 指示集中在 [AGENTS.md](AGENTS.md)，適用於所有 coding agent，包含 Claude Code。

開始工作前先讀 AGENTS.md，特別是：

- **Collaboration and ownership** — 實作要委派給 subagent，root agent 負責規劃、協調、審查、整合與回報。
- **Codebase discovery** — 探索程式碼優先用 codebase-memory-mcp 的圖查詢，而不是 grep／glob。
- **開發迴圈與驗收** — 每次改動的迴圈、各層的驗證指令、完成的定義，以及哪些行為 agent 無法驗證、只能請使用者確認。
- **怎麼呼叫 gpt-5.6-luna／gemini-3.8-flash** 與 **誰做什麼** — codex 與 agy 兩個 CLI 的實際指令、沙箱選項，以及「高階模型只規劃派工驗收、實作交給 luna/gemini」這條分工。

`docs/06-handoff.md` 是階段狀態與驗收條件的單一真實來源；改變了「能做什麼」就要同步它。
