import Foundation

/// Pure presentation logic for the Settings page's service start/stop
/// control. Kept free of AppKit/`Process` so every state combination can be
/// asserted directly, without launching a real child process.
enum ServiceRuntimeControl {
    /// The three situations the control can be in. `reachableElsewhere`
    /// exists because a service answering `/healthz` is not necessarily one
    /// this app started — a duplicate `serve` process could not even bind
    /// the port, and this app has no way to tell such an instance apart
    /// from any other kind of "already running" — so both starting a
    /// second copy and offering to stop something this handle never
    /// launched are avoided rather than guessed at.
    enum State: Equatable {
        case managedRunning
        case reachableElsewhere
        case stopped(executableFound: Bool, exitStatus: Int32?)
    }

    static func state(
        managedRunning: Bool,
        reachableElsewhere: Bool,
        executableFound: Bool,
        exitStatus: Int32?
    ) -> State {
        if managedRunning { return .managedRunning }
        if reachableElsewhere { return .reachableElsewhere }
        return .stopped(executableFound: executableFound, exitStatus: exitStatus)
    }

    struct Presentation: Equatable {
        let buttonTitle: String
        let buttonEnabled: Bool
        let statusText: String
    }

    /// The button's title is always "重新啟動服務": there is no separate
    /// start/stop pair any more, only one action that always ends with the
    /// service running — stopping first when something this page manages is
    /// already up, or simply starting when nothing is. Only whether that
    /// action is currently possible, and why not, varies by state.
    static let buttonTitle = "重新啟動服務"

    static func presentation(for state: State) -> Presentation {
        switch state {
        case .managedRunning:
            return Presentation(
                buttonTitle: buttonTitle,
                buttonEnabled: true,
                statusText: "執行中（由此頁啟動）。"
            )
        case .reachableElsewhere:
            return Presentation(
                buttonTitle: buttonTitle,
                buttonEnabled: false,
                statusText: "服務已在執行，但不是由這個頁面啟動的，無法從這裡重新啟動（也不會再啟動第二份）。"
            )
        case .stopped(let executableFound, let exitStatus):
            guard executableFound else {
                return Presentation(
                    buttonTitle: buttonTitle,
                    buttonEnabled: false,
                    statusText: "找不到執行檔，請先在下方指定路徑。"
                )
            }
            let statusText: String
            if let exitStatus, exitStatus != 0 {
                statusText = "已停止（結束碼 \(exitStatus)）。"
            } else {
                statusText = "已停止。"
            }
            return Presentation(buttonTitle: buttonTitle, buttonEnabled: true, statusText: statusText)
        }
    }
}

/// Decides what the app-launch auto-start path should do, before it ever
/// touches `Process` or the filesystem — kept pure so every combination is
/// directly testable.
///
/// This app is the service's main runtime (see `ServiceQuitPolicy`), so the
/// symmetric half of "quitting the app stops the service it launched" is
/// "launching the app starts the service" — but only when nothing is
/// already answering. A LaunchAgent-started service, a developer's own
/// terminal-launched one, or one left over from a previous run all answer
/// `/healthz`, and this app must never start a second copy next to any of
/// them (`ServiceRuntimeControl.State.reachableElsewhere` already encodes
/// the same "never touch it, never duplicate it" rule for the Settings
/// page's button; this is that same rule at launch time, before a
/// `ManagedProcess` handle exists to reason about).
enum ServiceAutoStartPolicy {
    enum Decision: Equatable {
        /// Something already answers on the configured host/port — do not
        /// start a second copy, managed or not.
        case skipAlreadyRunning
        /// Nothing answers, and no `tea-asr` executable was found either —
        /// this must be surfaced in the UI, never dropped silently.
        case skipExecutableNotFound
        /// Nothing answers, and an executable was found: go ahead and start
        /// it the same way the Settings page's own button would.
        case start
    }

    static func decide(reachable: Bool, executableFound: Bool) -> Decision {
        if reachable { return .skipAlreadyRunning }
        if !executableFound { return .skipExecutableNotFound }
        return .start
    }
}

/// Pure presentation logic for the Logs page's "服務輸出" tab: the service
/// process's own stdout/stderr, as opposed to the structured `/v1/logs`
/// feed shown in the other tab. Kept separate from `LogsPresentation` (which
/// covers the structured feed) since the two have unrelated failure modes —
/// this one is never about a rejected token or an unreachable server, only
/// about whether *this app* is the one that started the process.
enum ServiceOutputPresentation {
    /// Whether the reader is looking at output for a process this app
    /// itself launched, one that appears to be running somewhere else, or
    /// nothing at all yet.
    static func statusText(isManaged: Bool, isRunning: Bool, reachableElsewhere: Bool) -> String {
        guard isManaged else {
            if reachableElsewhere {
                return "服務不是由這個 app 啟動，看不到它的輸出。"
            }
            return "尚未啟動服務。到設定頁的「本機服務」按「啟動服務」以檢視輸出。"
        }
        return isRunning ? "執行中。" : "已結束（以下是它結束前的輸出）。"
    }

    /// The buffered output body, with the truncation from `BoundedLineBuffer`
    /// made visible rather than silently absorbed.
    static func body(lines: [String], droppedLines: Int) -> String {
        guard !lines.isEmpty else { return "尚無輸出。" }
        var text = lines.joined(separator: "\n")
        if droppedLines > 0 {
            text = "…（已捨棄 \(droppedLines) 行較舊的輸出，僅保留最近 \(lines.count) 行）\n" + text
        }
        return text
    }
}
