import Foundation

/// Starts and stops the local TEA ASR service from the menu bar.
///
/// The service stays a separate process rather than being embedded: it owns a
/// Python runtime and a 1.2 GB model, and docs/03 explicitly rules out shipping
/// those inside a client. What the app provides is the control surface.
enum ServiceControl {
    /// Where `tea-asr` might be without a shell PATH to consult.
    private static let searchPaths = [
        "/opt/homebrew/bin/tea-asr",
        "/usr/local/bin/tea-asr",
    ]

    static func resolveExecutable(configured: String) -> URL? {
        if !configured.isEmpty {
            let url = URL(fileURLWithPath: configured)
            return FileManager.default.isExecutableFile(atPath: url.path) ? url : nil
        }
        for path in searchPaths where FileManager.default.isExecutableFile(atPath: path) {
            return URL(fileURLWithPath: path)
        }
        // A source checkout's virtualenv, found via the token file's sibling
        // config is not reliable, so fall back to asking the login shell once.
        return whichViaLoginShell()
    }

    private static func whichViaLoginShell() -> URL? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-lc", "command -v tea-asr"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let path = String(decoding: data, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty, FileManager.default.isExecutableFile(atPath: path) else {
            return nil
        }
        return URL(fileURLWithPath: path)
    }

    enum ControlError: LocalizedError {
        case executableNotFound
        case failed(String)

        var errorDescription: String? {
            switch self {
            case .executableNotFound:
                return "找不到 tea-asr 執行檔。請在設定裡指定它的位置"
                    + "（通常是專案的 .venv/bin/tea-asr）。"
            case .failed(let message):
                return message
            }
        }
    }

    /// Launch the service detached, so quitting the app does not kill it.
    static func start(executable: URL) throws {
        let process = Process()
        process.executableURL = executable
        process.arguments = ["serve"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
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
