import Foundation

enum AppMode: Equatable {
    case idle
    case dictation
    case meeting
}

struct ConnectionIssue: Equatable, LocalizedError {
    let code: String
    let message: String
    let retryable: Bool
    let closeCode: Int?

    var errorDescription: String? { message }

    static func fromWire(code: String, message: String, retryable: Bool) -> ConnectionIssue {
        let closeCode: Int?
        switch code {
        case "concurrent_session_limit":
            closeCode = 4029
        case "queue_full", "session_limit", "slow_client":
            closeCode = 1013
        case "timeline_gap":
            closeCode = 1012
        default:
            closeCode = nil
        }
        return ConnectionIssue(code: code, message: message, retryable: retryable, closeCode: closeCode)
    }

    static func fromCloseCode(_ closeCode: Int, message: String) -> ConnectionIssue {
        switch closeCode {
        case 4029:
            return ConnectionIssue(
                code: "concurrent_session_limit",
                message: message,
                retryable: true,
                closeCode: closeCode
            )
        case 1013:
            return ConnectionIssue(
                code: "server_backpressure",
                message: message,
                retryable: true,
                closeCode: closeCode
            )
        case 1008:
            return ConnectionIssue(
                code: "protocol_or_authentication",
                message: message,
                retryable: false,
                closeCode: closeCode
            )
        case 1012, 1006:
            return ConnectionIssue(
                code: "connection_interrupted",
                message: message,
                retryable: true,
                closeCode: closeCode
            )
        default:
            return ConnectionIssue(
                code: "connection_closed",
                message: message,
                retryable: true,
                closeCode: closeCode > 0 ? closeCode : nil
            )
        }
    }
}

enum AppDisplayStatus: Equatable {
    case checking
    case offline
    case connecting
    case loading
    case ready
    case listening(mode: AppMode, preview: Bool)
    case retryable(ConnectionIssue)
    case failed(ConnectionIssue)

    var title: String {
        switch self {
        case .checking:
            return "檢查服務中…"
        case .offline:
            return "服務未執行"
        case .connecting:
            return "連線中…"
        case .loading:
            return "模型載入中…"
        case .ready:
            return "服務就緒，可開始聆聽"
        case .listening(_, let preview):
            return preview ? "聆聽中（含串流預覽）" : "聆聽中"
        case .retryable(let issue):
            return "可重試：\(issue.message.prefix(60))"
        case .failed(let issue):
            return "錯誤：\(issue.message.prefix(60))"
        }
    }
}

/// Shared, UI-facing state for the existing AppKit menu and windows.
///
/// It deliberately uses a callback instead of requiring SwiftUI/Combine. This
/// keeps the menu-bar executable's current AppKit architecture intact while
/// giving future SwiftUI views one stable observable source of truth.
final class AppState {
    private(set) var mode: AppMode = .idle
    private(set) var clientState: ASRClient.State = .idle
    private(set) var serviceSnapshot: ServiceSnapshot?
    private(set) var serviceError: ServiceProbeError?
    private(set) var serviceReachable: Bool?
    private(set) var lastText: String?

    var onChange: (() -> Void)?

    var displayStatus: AppDisplayStatus {
        Self.displayStatus(
            clientState: clientState,
            mode: mode,
            serviceSnapshot: serviceSnapshot,
            serviceReachable: serviceReachable,
            serviceError: serviceError
        )
    }

    func setMode(_ mode: AppMode) {
        self.mode = mode
        notify()
    }

    func updateClientState(_ state: ASRClient.State) {
        clientState = state
        notify()
    }

    func updateService(_ result: Result<ServiceSnapshot, ServiceProbeError>) {
        switch result {
        case .success(let snapshot):
            serviceSnapshot = snapshot
            serviceError = nil
            serviceReachable = true
        case .failure(let error):
            serviceSnapshot = nil
            serviceError = error
            serviceReachable = error.serviceReachable
        }
        notify()
    }

    func updateLastText(_ text: String) {
        lastText = text
        notify()
    }

    static func displayStatus(
        clientState: ASRClient.State,
        mode: AppMode,
        serviceSnapshot: ServiceSnapshot?,
        serviceReachable: Bool?,
        serviceError: ServiceProbeError?
    ) -> AppDisplayStatus {
        switch clientState {
        case .failed(let issue):
            return issue.retryable ? .retryable(issue) : .failed(issue)
        case .listening(_, let preview):
            return .listening(mode: mode, preview: preview)
        case .loadingModel:
            return .loading
        case .connecting:
            return serviceReachable == false ? .offline : .connecting
        case .idle:
            if let serviceError {
                let issue = ConnectionIssue(
                    code: "service_\(serviceError.serviceReachable ? "error" : "offline")",
                    message: serviceError.localizedDescription,
                    retryable: serviceError.retryable,
                    closeCode: nil
                )
                if !serviceError.serviceReachable {
                    return .offline
                }
                return issue.retryable ? .retryable(issue) : .failed(issue)
            }
            guard let serviceSnapshot else {
                return serviceReachable == false ? .offline : .checking
            }
            switch serviceSnapshot.status.modelState {
            case "loading", "idle_unloaded", "unprepared":
                return .loading
            case "recovering":
                return .retryable(Self.workerIssue(from: serviceSnapshot.status, retryable: true))
            case "failed":
                return .failed(Self.workerIssue(from: serviceSnapshot.status, retryable: false))
            case "ready":
                if serviceSnapshot.readyzOK {
                    return .ready
                }
                return .retryable(
                    ConnectionIssue(
                        code: "service_not_ready",
                        message: "服務尚未就緒，請稍候。",
                        retryable: true,
                        closeCode: nil
                    )
                )
            default:
                return .failed(
                    ConnectionIssue(
                        code: "unknown_worker_state",
                        message: "服務回報未知的 worker 狀態，請重新啟動服務。",
                        retryable: false,
                        closeCode: nil
                    )
                )
            }
        }
    }

    private static func workerIssue(from status: ServerStatus, retryable: Bool) -> ConnectionIssue {
        let fallback = retryable
            ? "ASR worker 正在恢復，請稍候。"
            : "ASR worker 無法使用，請檢查服務設定或重新啟動服務。"
        let message = safeWorkerMessage(from: status.lastError) ?? fallback
        return ConnectionIssue(
            code: retryable ? "worker_recovering" : "model_unavailable",
            message: message,
            retryable: retryable,
            closeCode: nil
        )
    }

    /// `last_error` is a diagnostic field, not a safe-to-display error
    /// envelope. The worker currently emits the allow-listed prefixes below;
    /// anything else may be a raw worker response or a future format carrying
    /// credentials, so keep it out of the menu and meeting window.
    private static func safeWorkerMessage(from raw: String?) -> String? {
        guard var normalized = raw?
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !normalized.isEmpty
        else {
            return nil
        }

        let lowercased = normalized.lowercased()
        let sensitiveMarkers = [
            "token",
            "authorization",
            "bearer",
            "password",
            "secret",
            "api_key",
            "api-key",
            "cookie",
            "set-cookie",
            "response body",
            "response_body",
            "http body",
            "http_body",
            "<html",
            "<!doctype",
            "{\"",
            "[{"
        ]
        guard !sensitiveMarkers.contains(where: { lowercased.contains($0) }) else {
            return nil
        }

        let safePrefixes = [
            "Worker did not become ready:",
            "Worker connection lost:",
            "Worker exited with code ",
            "Inference timed out",
            "Worker restarted "
        ]
        guard safePrefixes.contains(where: { normalized.hasPrefix($0) }) else {
            return nil
        }

        normalized = String(normalized.prefix(160))
        return normalized.isEmpty ? nil : normalized
    }

    private func notify() {
        onChange?()
    }
}
