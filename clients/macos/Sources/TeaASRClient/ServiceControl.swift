import Foundation

/// Starts and stops the local TEA ASR service from the menu bar.
///
/// The service stays a separate process rather than being embedded: it owns a
/// Python runtime and a 1.2 GB model, and docs/03 explicitly rules out shipping
/// those inside a client. What the app provides is the control surface.
enum ServiceControl {
    /// The result of looking for `tea-asr`: what was found (if anything) and
    /// every location that was actually tried, so a "not found" error can
    /// name its search rather than just restate the symptom.
    struct ExecutableSearch: Equatable {
        let executable: URL?
        let searchedPaths: [String]
    }

    /// Resolves the configured/auto-discovered executable and reports every
    /// location that was tried along the way.
    ///
    /// An explicit `configured` path is used as-is (its own existence is the
    /// entire search). An empty `configured` falls back to
    /// `ExecutableDiscovery`'s candidate list: fixed install locations,
    /// every `$PATH` directory, and a source checkout's `.venv/bin/tea-asr`
    /// found by climbing up from the running app's own location — the case
    /// that matters for a developer checkout where `tea-asr` was never
    /// `pip install`ed onto `$PATH` at all.
    static func search(configured: String) -> ExecutableSearch {
        let trimmed = configured.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            let url = URL(fileURLWithPath: trimmed)
            let ok = FileManager.default.isExecutableFile(atPath: url.path)
            return ExecutableSearch(executable: ok ? url : nil, searchedPaths: [trimmed])
        }
        let candidates = ExecutableDiscovery.candidates(
            pathEnvironment: ProcessInfo.processInfo.environment["PATH"],
            startingDirectory: Bundle.main.bundleURL.deletingLastPathComponent().path
        )
        let found = ExecutableDiscovery.firstExecutable(in: candidates) {
            FileManager.default.isExecutableFile(atPath: $0)
        }
        return ExecutableSearch(
            executable: found.map { URL(fileURLWithPath: $0.path) },
            searchedPaths: candidates.map(\.path)
        )
    }

    /// Convenience for callers that only care whether something was found.
    static func resolveExecutable(configured: String) -> URL? {
        search(configured: configured).executable
    }

    enum ControlError: LocalizedError, Equatable {
        /// Nothing at `configured`, and none of `searched` panned out either.
        case executableNotFound(searched: [String])
        /// `configured`/discovered path does not exist on disk at all.
        case executableMissing(path: String)
        /// The path exists but is not marked executable.
        case notExecutable(path: String)
        /// The process was launched but exited before it could be confirmed
        /// running — a crash-on-start, not a normal service lifecycle event.
        case exitedImmediately(status: Int32, message: String?)
        case failed(String)

        var errorDescription: String? {
            switch self {
            case .executableNotFound(let searched):
                guard !searched.isEmpty else {
                    return "找不到 tea-asr 執行檔。請在設定頁的「服務執行檔」欄位手動指定路徑"
                        + "（通常是專案的 .venv/bin/tea-asr）。"
                }
                let locations = searched.map { "• \($0)" }.joined(separator: "\n")
                return "找不到 tea-asr 執行檔，已找過以下位置：\n\(locations)\n\n"
                    + "請在設定頁的「服務執行檔」欄位手動指定路徑（通常是專案的 .venv/bin/tea-asr）。"
            case .executableMissing(let path):
                return "指定的執行檔不存在：\(path)"
            case .notExecutable(let path):
                return "指定的路徑沒有執行權限：\(path)"
            case .exitedImmediately(let status, let message):
                let detail = message.map { "：\($0)" } ?? ""
                return "服務啟動後立即結束（結束碼 \(status)）\(detail)"
            case .failed(let message):
                return message
            }
        }
    }

    /// Launch the service detached, so quitting the app does not kill it.
    ///
    /// Unlike the old version, this does not silently trust `Process.run()`
    /// succeeding: a missing file, a non-executable file, and a process that
    /// crashes immediately after starting (bad arguments, a corrupt model
    /// path, …) all used to look identical to "started fine" from here. The
    /// brief post-launch check trades a small, one-time, user-initiated delay
    /// for turning that silent failure into a message that says what broke.
    static func start(executable: URL) throws {
        guard FileManager.default.fileExists(atPath: executable.path) else {
            throw ControlError.executableMissing(path: executable.path)
        }
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw ControlError.notExecutable(path: executable.path)
        }
        let process = Process()
        process.executableURL = executable
        process.arguments = ["serve"]
        process.standardOutput = FileHandle.nullDevice
        let errorPipe = Pipe()
        process.standardError = errorPipe
        try process.run()
        // Give an immediate crash-on-start a moment to actually happen before
        // reporting success. A healthy `serve` process stays alive for the
        // service's whole lifetime, so anything that exits within this window
        // is a launch failure, not a normal lifecycle transition. Polled
        // rather than one fixed sleep so a crash is caught almost
        // immediately while a busy machine still gets the rest of the
        // window before this concludes the process stayed up on purpose
        // (see `launchService`'s identical reasoning below).
        let deadline = Date().addingTimeInterval(1.2)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        guard !process.isRunning else { return }
        let data = errorPipe.fileHandleForReading.availableData
        let message = String(decoding: data, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        throw ControlError.exitedImmediately(
            status: process.terminationStatus,
            message: message.isEmpty ? nil : message
        )
    }

    @discardableResult
    static func run(executable: URL, arguments: [String]) throws -> String {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let output = String(decoding: data, as: UTF8.self)
        guard process.terminationStatus == 0 else {
            throw ControlError.failed(output.isEmpty ? "指令失敗" : output)
        }
        return output
    }

    /// Whether anything is answering on the service's health endpoint.
    static func probeHealth(host: String, port: Int, completion: @escaping (Bool) -> Void) {
        guard let url = URL(string: "http://\(host):\(port)/healthz") else {
            completion(false)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { _, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { completion(ok) }
        }.resume()
    }

    static func agentInstalled(executable: URL) -> Bool {
        guard let output = try? run(executable: executable, arguments: ["service", "status"]) else {
            return false
        }
        return output.contains("\"agent_installed\": true")
    }

    // MARK: - Managed launch (start/stop control, service output tab)

    /// Starts `tea-asr serve` and keeps a live `Process` handle plus its
    /// captured combined stdout/stderr, unlike `start(executable:)` above
    /// which discards the `Process` the moment the crash-on-start check
    /// passes. Keeping the handle is what makes a later `stop` safe: calling
    /// `terminate()` on the returned `ManagedProcess` only ever signals this
    /// exact pid, never anything discovered by scanning for a process name
    /// or command line, so it cannot touch a `tea-asr serve` instance this
    /// call did not itself start (one launched from the menu bar's own
    /// fire-and-forget `start`, from a LaunchAgent, or from a developer's own
    /// terminal).
    static func launchService(executable: URL) throws -> ManagedProcess {
        let managed = try launchManaged(executable: executable, arguments: ["serve"])
        // Same idea as `start(executable:)`'s crash-on-start window, and for
        // the same reason: a healthy `serve` process stays alive for the
        // service's whole lifetime, so anything that exits within this
        // window is a launch failure, not a normal lifecycle transition.
        // Polled rather than one fixed `Thread.sleep` so an early crash (the
        // overwhelmingly common case here — a bad path or a missing model
        // asset) is reported almost immediately, while a machine too busy to
        // even schedule the child within the first tick still gets the rest
        // of the window before this concludes it stayed up on purpose.
        let deadline = Date().addingTimeInterval(1.2)
        while managed.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        guard !managed.isRunning else { return managed }
        let tail = managed.output.snapshot().lines.joined(separator: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        throw ControlError.exitedImmediately(
            status: managed.exitStatus ?? managed.process.terminationStatus,
            message: tail.isEmpty ? nil : tail
        )
    }

    /// Runs `tea-asr model-prepare` the same managed way, so its progress
    /// output can be shown and its completion observed instead of blocking
    /// the caller until it exits (a first download can take a while).
    static func launchModelPrepare(executable: URL) throws -> ManagedProcess {
        try launchManaged(executable: executable, arguments: ["model-prepare"])
    }

    private static func launchManaged(
        executable: URL,
        arguments: [String],
        outputCapacity: Int = 500
    ) throws -> ManagedProcess {
        guard FileManager.default.fileExists(atPath: executable.path) else {
            throw ControlError.executableMissing(path: executable.path)
        }
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw ControlError.notExecutable(path: executable.path)
        }
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        let managed = ManagedProcess(process: process, outputCapacity: outputCapacity)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak managed] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            managed?.output.append(String(decoding: data, as: UTF8.self))
        }
        process.terminationHandler = { [weak managed] finished in
            pipe.fileHandleForReading.readabilityHandler = nil
            managed?.output.flush()
            managed?.markExited(status: finished.terminationStatus)
        }
        try process.run()
        return managed
    }
}

/// Whether quitting the app should also stop the service.
///
/// This app *is* the service's main runtime, so stopping it on quit is
/// unconditional — there is no setting to opt out of. The safety boundary is
/// what this can ever act on, not whether it fires: only a process this app
/// launched and still holds a live `ManagedProcess` handle for. A service
/// started by a LaunchAgent, from a developer's terminal, or by the menu
/// bar's own fire-and-forget `start(executable:)` (which keeps no handle) has
/// no handle here, so it is left alone unconditionally too — this app never
/// looks for something that merely *resembles* `tea-asr serve`.
enum ServiceQuitPolicy {
    enum Action: Equatable {
        case stopManagedProcess
        case leaveRunning
    }

    /// - Parameters:
    ///   - hasManagedProcess: this app holds a handle to a service process.
    ///   - managedProcessIsRunning: that process has not already exited.
    static func action(
        hasManagedProcess: Bool,
        managedProcessIsRunning: Bool
    ) -> Action {
        guard hasManagedProcess, managedProcessIsRunning else {
            return .leaveRunning
        }
        return .stopManagedProcess
    }
}

/// A child process this app started and holds a live handle to, as opposed
/// to a `tea-asr serve` instance that might be reachable for any other
/// reason (the menu bar's own launch, a LaunchAgent, a developer's own
/// terminal). Everything this type exposes — `terminate()`, `output` — is
/// scoped to this exact `Process` instance and nothing else, which is the
/// whole point: it is what lets "stop" be implemented at all without ever
/// risking a process this app did not itself launch.
final class ManagedProcess {
    let process: Process
    let output: BoundedLineBuffer
    private let lock = NSLock()
    private var _exitStatus: Int32?

    /// Fired once the process has exited, on an arbitrary background queue
    /// (whatever `Process.terminationHandler` runs on) — callers that touch
    /// UI must dispatch back to the main queue themselves.
    var onExit: ((Int32) -> Void)?

    init(process: Process, outputCapacity: Int) {
        self.process = process
        self.output = BoundedLineBuffer(maxLines: outputCapacity)
    }

    var isRunning: Bool { process.isRunning }

    var exitStatus: Int32? {
        lock.lock()
        defer { lock.unlock() }
        return _exitStatus
    }

    fileprivate func markExited(status: Int32) {
        lock.lock()
        _exitStatus = status
        lock.unlock()
        onExit?(status)
    }

    /// Signals only this process (`SIGTERM` via `Process.terminate()`), and
    /// is a no-op if it has already exited. There is deliberately no
    /// force-kill fallback here: this app only ever asks the process it
    /// launched to shut down, it does not hunt for anything.
    func terminate() {
        guard process.isRunning else { return }
        process.terminate()
    }
}

/// A thread-safe line buffer with a hard cap on retained lines.
///
/// docs/06-handoff.md's bounded-buffer constraint requires every layer that
/// retains a child process's output to have both a cap and a *visible*
/// failure mode once that cap is hit. This keeps at most `maxLines` complete
/// lines and counts — never silently forgets — how many older lines were
/// dropped to stay under it, so a caller can render "…already truncated"
/// instead of quietly losing data or growing the buffer without bound.
final class BoundedLineBuffer {
    private let maxLines: Int
    private let lock = NSLock()
    private var lines: [String] = []
    private var pending = ""
    private var droppedLines = 0

    init(maxLines: Int) {
        self.maxLines = max(1, maxLines)
    }

    /// Appends a raw chunk of process output. `pending` holds whatever text
    /// after the last newline in the chunk so far — a process that writes in
    /// small increments does not get one logical line fragmented into many
    /// buffer entries.
    func append(_ chunk: String) {
        guard !chunk.isEmpty else { return }
        lock.lock()
        defer { lock.unlock() }
        var parts = (pending + chunk).components(separatedBy: "\n")
        pending = parts.removeLast()
        for line in parts {
            appendLine(line)
        }
    }

    /// Forces any incomplete trailing line into the buffer. Call once the
    /// producing process has exited, since a final line with no trailing
    /// newline would otherwise never surface.
    func flush() {
        lock.lock()
        defer { lock.unlock() }
        guard !pending.isEmpty else { return }
        appendLine(pending)
        pending = ""
    }

    private func appendLine(_ line: String) {
        lines.append(line)
        if lines.count > maxLines {
            lines.removeFirst()
            droppedLines += 1
        }
    }

    struct Snapshot: Equatable {
        let lines: [String]
        let droppedLines: Int
    }

    func snapshot() -> Snapshot {
        lock.lock()
        defer { lock.unlock() }
        return Snapshot(lines: lines, droppedLines: droppedLines)
    }
}
