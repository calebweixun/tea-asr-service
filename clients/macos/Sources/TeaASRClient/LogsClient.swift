import Foundation

/// The minimum-severity filter accepted by `GET /v1/logs`. Selecting a level
/// returns entries at that severity *and above* (e.g. `.warning` includes
/// warning and error) — the client never invents client-side filtering on
/// top of what the server already guarantees.
enum LogLevel: String, CaseIterable, Equatable {
    case debug
    case info
    case warning
    case error

    /// Label for the level picker. Each spells out what selecting it
    /// actually returns, since "level" alone reads as "only this level" to
    /// a first-time user when the server semantics are "this and worse".
    var filterTitle: String {
        switch self {
        case .debug: return "Debug（全部等級）"
        case .info: return "Info（含以上等級）"
        case .warning: return "Warning（含 Error）"
        case .error: return "僅 Error"
        }
    }
}

/// A single JSON value inside a log entry's free-form `fields` object. The
/// server does not constrain field value types, so this mirrors arbitrary
/// JSON rather than assuming every field is a string.
indirect enum LogFieldValue: Decodable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([LogFieldValue])
    case object([String: LogFieldValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
            return
        }
        if let value = try? container.decode(Bool.self) {
            self = .bool(value)
            return
        }
        if let value = try? container.decode(Double.self) {
            self = .number(value)
            return
        }
        if let value = try? container.decode(String.self) {
            self = .string(value)
            return
        }
        if let value = try? container.decode([LogFieldValue].self) {
            self = .array(value)
            return
        }
        if let value = try? container.decode([String: LogFieldValue].self) {
            self = .object(value)
            return
        }
        throw DecodingError.dataCorruptedError(in: container, debugDescription: "不支援的日誌欄位型別")
    }

    /// A one-line, human-readable rendering used in the log list. Never
    /// pretty-printed: log rows are one line each so the list reads like a
    /// terminal/Console.app view instead of wrapping into a JSON blob.
    var displayString: String {
        switch self {
        case .string(let value): return value
        case .number(let value):
            if value == value.rounded() && abs(value) < 1e15 {
                return String(Int64(value))
            }
            return String(value)
        case .bool(let value): return value ? "true" : "false"
        case .null: return "null"
        case .array(let values):
            return "[" + values.map(\.displayString).joined(separator: ", ") + "]"
        case .object(let values):
            let inner = values.keys.sorted().map { "\($0): \(values[$0]!.displayString)" }
            return "{" + inner.joined(separator: ", ") + "}"
        }
    }
}

/// One row of `GET /v1/logs`'s `items` array.
struct LogEntry: Decodable, Equatable {
    let ts: String
    let level: String
    let logger: String
    let message: String
    let fields: [String: LogFieldValue]?
}

/// The full `GET /v1/logs` response body. There is deliberately no paging
/// here: the endpoint only ever returns a bounded recent window, and
/// `hasMore` is the server's own admission that older data exists but is not
/// reachable through this endpoint.
struct LogsResponse: Decodable, Equatable {
    let items: [LogEntry]
    let count: Int
    let limit: Int
    let hasMore: Bool

    enum CodingKeys: String, CodingKey {
        case items
        case count
        case limit
        case hasMore = "has_more"
    }
}

/// Failure modes surfaced by `LogsClient`. These are kept distinct from an
/// empty `items` array on purpose: "the service refused/could not be reached"
/// must never render the same as "there really are no matching log lines".
enum LogsFetchError: LocalizedError, Equatable {
    case invalidEndpoint
    case unreachable(String)
    case unauthorized
    case forbidden
    case invalidLevel
    case limitTooLarge
    case unexpectedStatus(Int)
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .invalidEndpoint:
            return "服務位址無效。"
        case .unreachable(let message):
            return message.isEmpty ? "無法連到 TEA ASR 服務，請確認服務是否已啟動。" : "無法連到 TEA ASR 服務：\(message)"
        case .unauthorized:
            return "服務拒絕了 token，請到設定頁確認 token。"
        case .forbidden:
            return "服務拒絕了這個請求。"
        case .invalidLevel:
            return "等級參數無效。"
        case .limitTooLarge:
            return "筆數上限無效（最多 500 筆）。"
        case .unexpectedStatus(let code):
            return "服務回應 HTTP \(code)。"
        case .invalidResponse:
            return "服務的 /v1/logs 回應格式無法辨識。"
        }
    }
}

/// The section's fetch lifecycle. Kept separate from `LogsResponse` so the
/// UI can distinguish "never asked yet" from "asked and got zero rows" from
/// "asked and failed" without overloading an empty array for all three.
enum LogsFetchState: Equatable {
    case idle
    case loading
    case loaded
    case failed(LogsFetchError)
}

/// Testable seam for the log section: `MainWindowController` depends on this
/// protocol rather than `LogsClient` directly, so tests can substitute a fake
/// that completes synchronously instead of making a real network call.
protocol LogsFetching {
    func fetch(
        host: String,
        port: Int,
        token: String?,
        level: LogLevel,
        limit: Int,
        completion: @escaping (Result<LogsResponse, LogsFetchError>) -> Void
    )
}

/// Fetches `GET /v1/logs`. Mirrors `ServiceProbe`'s style deliberately: plain
/// `URLSession` data task, a private callback queue, and a `finish` helper
/// that always hands the result back on the main queue.
final class LogsClient: LogsFetching {
    private let session: URLSession
    private let callbackQueue = DispatchQueue(label: "tea-asr.logs-client")

    init(session: URLSession = .shared) {
        self.session = session
    }

    /// Builds the request in isolation from networking so the query-string
    /// and header assembly can be unit tested without a server.
    static func buildRequest(
        host: String,
        port: Int,
        token: String?,
        level: LogLevel,
        limit: Int
    ) -> Result<URLRequest, LogsFetchError> {
        let trimmedHost = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedHost.isEmpty, (1...65_535).contains(port) else {
            return .failure(.invalidEndpoint)
        }
        var components = URLComponents()
        components.scheme = "http"
        components.host = trimmedHost
        components.port = port
        components.path = "/v1/logs"
        components.queryItems = [
            URLQueryItem(name: "level", value: level.rawValue),
            URLQueryItem(name: "limit", value: String(limit)),
        ]
        guard let url = components.url else {
            return .failure(.invalidEndpoint)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 3.0
        if let token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return .success(request)
    }

    func fetch(
        host: String,
        port: Int,
        token: String?,
        level: LogLevel,
        limit: Int,
        completion: @escaping (Result<LogsResponse, LogsFetchError>) -> Void
    ) {
        switch Self.buildRequest(host: host, port: port, token: token, level: level, limit: limit) {
        case .failure(let error):
            finish(.failure(error), completion: completion)
        case .success(let request):
            session.dataTask(with: request) { [weak self] data, response, error in
                guard let self else { return }
                self.callbackQueue.async {
                    self.finish(self.decodeResult(data: data, response: response, error: error), completion: completion)
                }
            }.resume()
        }
    }

    private func decodeResult(
        data: Data?,
        response: URLResponse?,
        error: Error?
    ) -> Result<LogsResponse, LogsFetchError> {
        if let error {
            return .failure(.unreachable(error.localizedDescription))
        }
        guard let http = response as? HTTPURLResponse else {
            return .failure(.invalidResponse)
        }
        switch http.statusCode {
        case 200:
            guard let data, let decoded = try? JSONDecoder().decode(LogsResponse.self, from: data) else {
                return .failure(.invalidResponse)
            }
            return .success(decoded)
        case 401:
            return .failure(.unauthorized)
        case 403:
            return .failure(.forbidden)
        case 422:
            return .failure(.invalidLevel)
        default:
            return .failure(.unexpectedStatus(http.statusCode))
        }
    }

    private func finish(
        _ result: Result<LogsResponse, LogsFetchError>,
        completion: @escaping (Result<LogsResponse, LogsFetchError>) -> Void
    ) {
        DispatchQueue.main.async {
            completion(result)
        }
    }
}

/// Pure text formatting for the log page, kept free of AppKit so it can be
/// unit tested directly. `MainWindowController` applies colour on top of
/// `line(for:)`'s plain text; it never encodes level in the string itself.
enum LogsPresentation {
    static func line(for entry: LogEntry) -> String {
        var text = "\(entry.ts)  [\(entry.level.uppercased())]  \(entry.logger)  \(entry.message)"
        if let fields = entry.fields, !fields.isEmpty {
            let rendered = fields.keys.sorted().map { "\($0)=\(fields[$0]!.displayString)" }
            text += "  " + rendered.joined(separator: " ")
        }
        return text
    }

    static func idleNotice() -> String {
        "尚未載入日誌。切換到此頁或按「重新整理」以取得最新資料。"
    }

    static func loadingNotice() -> String {
        "載入中…"
    }

    /// Only shown when the fetch *succeeded* and returned zero rows for the
    /// current level filter — never as a stand-in for a failed fetch.
    static func emptyNotice() -> String {
        "沒有符合目前等級篩選的日誌。"
    }

    static func loadedSummary(count: Int) -> String {
        "顯示最近 \(count) 筆。"
    }

    /// The server has no paging: this is the only honest way to say "there is
    /// more, but you cannot page to it from here."
    static func hasMoreNotice(shown: Int) -> String {
        "還有更舊的日誌未顯示；這裡只顯示最近 \(shown) 筆（伺服器不提供分頁）。"
    }
}
