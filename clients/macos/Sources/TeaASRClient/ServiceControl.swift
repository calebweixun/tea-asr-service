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
        // is a launch failure, not a normal lifecycle transition.
        Thread.sleep(forTimeInterval: 0.3)
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
}
