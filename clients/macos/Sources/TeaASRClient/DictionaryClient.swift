import Foundation

/// Where and how to reach the service. Gathered from `Settings` at call time
/// so an address/token change on the Settings page applies immediately.
struct DictionaryEndpoint: Equatable {
    let host: String
    let port: Int
    let token: String?
}

/// Failure modes of the dictionary routes. A 409 and a 422 are not "errors"
/// in the same sense as an unreachable service — the editor reacts to each
/// differently (offer reload/overwrite; mark the offending rows) — so they
/// are their own cases rather than an HTTP status the UI has to decode.
enum DictionaryAPIError: LocalizedError, Equatable {
    case invalidEndpoint
    case unreachable(String)
    case unauthorized
    case forbidden(String?)
    case notFound
    /// 409. `currentRevision` is the server's `error.current_revision`:
    /// the file's revision now, or `nil` when it no longer exists.
    case conflict(currentRevision: DictionaryRevision?)
    case invalid(message: String, fields: [DictionaryFieldError])
    case unexpectedStatus(Int, String?)
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .invalidEndpoint:
            return "服務位址無效，請到設定頁確認。"
        case .unreachable(let message):
            return message.isEmpty ? "無法連到 TEA ASR 服務，請確認服務是否已啟動。" : "無法連到 TEA ASR 服務：\(message)"
        case .unauthorized:
            return "服務拒絕了 token，請到設定頁確認 token。"
        case .forbidden:
            // PUT/DELETE accept loopback callers only (unless the server sets
            // dictionary_remote_edit); the server's own message is English.
            return "服務拒絕了這個請求：字典只能在服務所在的這台電腦上修改。"
        case .notFound:
            return "伺服器上找不到這個字典，可能已被刪除或改名。"
        case .conflict:
            return "這個字典在別處被修改過。"
        case .invalid(let message, let fields):
            if fields.isEmpty { return message.isEmpty ? "伺服器拒絕了這份字典內容。" : message }
            return fields.map(\.localizedMessage).joined(separator: "\n")
        case .unexpectedStatus(let code, let message):
            if let message, !message.isEmpty { return "服務回應 HTTP \(code)：\(message)" }
            return "服務回應 HTTP \(code)。"
        case .invalidResponse:
            return "服務的 /v1/dictionaries 回應格式無法辨識。"
        }
    }

    /// The service itself could not be reached, as opposed to answering with
    /// a refusal — the page shows a different empty state for each.
    var isUnreachable: Bool {
        switch self {
        case .invalidEndpoint, .unreachable: return true
        default: return false
        }
    }
}

/// Testable seam for the dictionary page, like `LogsFetching` for the logs
/// page. Every completion is delivered on the main queue.
protocol DictionaryServing {
    func list(
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<[DictionarySummary], DictionaryAPIError>) -> Void
    )
    func get(
        name: String,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<DictionaryDocument, DictionaryAPIError>) -> Void
    )
    func put(
        name: String,
        content: DictionaryContent,
        condition: DictionaryWriteCondition,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<DictionaryDocument, DictionaryAPIError>) -> Void
    )
    func delete(
        name: String,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<Void, DictionaryAPIError>) -> Void
    )
    func preview(
        name: String,
        text: String,
        draft: DictionaryContent?,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<DictionaryPreviewResult, DictionaryAPIError>) -> Void
    )
}

/// `/v1/dictionaries` over plain `URLSession`, in the same shape as
/// `LogsClient`: request assembly and response decoding are static and pure
/// so they can be unit tested without a server.
final class DictionaryClient: DictionaryServing {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    // MARK: Request assembly

    static func buildRequest(
        method: String,
        path: [String],
        body: Data? = nil,
        endpoint: DictionaryEndpoint
    ) -> Result<URLRequest, DictionaryAPIError> {
        let host = endpoint.host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty, (1...65_535).contains(endpoint.port) else {
            return .failure(.invalidEndpoint)
        }
        // Each name becomes one path segment. A name that came from an
        // invalid file on disk may contain anything, so it is percent-encoded
        // with "/" excluded — it can never address a different route.
        var segmentAllowed = CharacterSet.urlPathAllowed
        segmentAllowed.remove(charactersIn: "/")
        let encoded = path.map { $0.addingPercentEncoding(withAllowedCharacters: segmentAllowed) ?? "" }
        var components = URLComponents()
        components.scheme = "http"
        components.host = host
        components.port = endpoint.port
        components.percentEncodedPath = "/v1/dictionaries" + encoded.map { "/" + $0 }.joined()
        guard let url = components.url else { return .failure(.invalidEndpoint) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 5.0
        if let token = endpoint.token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return .success(request)
    }

    // MARK: Response decoding

    /// Maps one HTTP response to either its body or a typed error. Shared by
    /// every route so 401/403/404/409/422 mean the same thing everywhere.
    static func check(
        data: Data?,
        response: URLResponse?,
        error: Error?,
        acceptable: Set<Int> = [200]
    ) -> Result<Data, DictionaryAPIError> {
        if let error {
            return .failure(.unreachable(error.localizedDescription))
        }
        guard let http = response as? HTTPURLResponse else {
            return .failure(.invalidResponse)
        }
        let body = data ?? Data()
        if acceptable.contains(http.statusCode) {
            return .success(body)
        }
        let envelope = ErrorEnvelope.decode(body)
        switch http.statusCode {
        case 401:
            return .failure(.unauthorized)
        case 403:
            return .failure(.forbidden(envelope?.message))
        case 404:
            return .failure(.notFound)
        case 409:
            return .failure(.conflict(currentRevision: envelope?.currentRevision))
        case 422:
            return .failure(.invalid(message: envelope?.message ?? "", fields: envelope?.fields ?? []))
        default:
            return .failure(.unexpectedStatus(http.statusCode, envelope?.message))
        }
    }

    static func decode<T: Decodable>(_ type: T.Type, from result: Result<Data, DictionaryAPIError>) -> Result<T, DictionaryAPIError> {
        result.flatMap { data in
            guard let decoded = try? JSONDecoder().decode(type, from: data) else {
                return .failure(.invalidResponse)
            }
            return .success(decoded)
        }
    }

    /// The server's error body: `{"error": {"code", "message", "retryable",
    /// "request_id", "details"?, "current_revision"?}}`. `details` comes with
    /// a 422 `invalid`, `current_revision` with a 409 `conflict`.
    struct ErrorEnvelope: Decodable, Equatable {
        let code: String?
        let message: String?
        let fields: [DictionaryFieldError]
        let currentRevision: DictionaryRevision?

        private struct Root: Decodable {
            let error: Body
        }

        private struct Body: Decodable {
            let code: String?
            let message: String?
            let details: [DictionaryFieldError]?
            let currentRevision: DictionaryRevision?

            enum CodingKeys: String, CodingKey {
                case code
                case message
                case details
                case currentRevision = "current_revision"
            }
        }

        init(code: String?, message: String?, fields: [DictionaryFieldError], currentRevision: DictionaryRevision?) {
            self.code = code
            self.message = message
            self.fields = fields
            self.currentRevision = currentRevision
        }

        static func decode(_ data: Data) -> ErrorEnvelope? {
            guard let body = try? JSONDecoder().decode(Root.self, from: data).error else { return nil }
            return ErrorEnvelope(
                code: body.code,
                message: body.message,
                fields: body.details ?? [],
                currentRevision: body.currentRevision
            )
        }
    }

    // MARK: Routes

    func list(
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<[DictionarySummary], DictionaryAPIError>) -> Void
    ) {
        send(method: "GET", path: [], body: nil, endpoint: endpoint) { result in
            completion(Self.decode([DictionarySummary].self, from: result))
        }
    }

    func get(
        name: String,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<DictionaryDocument, DictionaryAPIError>) -> Void
    ) {
        send(method: "GET", path: [name], body: nil, endpoint: endpoint) { result in
            completion(Self.decode(DictionaryDocument.self, from: result))
        }
    }

    func put(
        name: String,
        content: DictionaryContent,
        condition: DictionaryWriteCondition,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<DictionaryDocument, DictionaryAPIError>) -> Void
    ) {
        let body = try? JSONEncoder().encode(DictionaryPutBody(content: content, condition: condition))
        send(method: "PUT", path: [name], body: body, endpoint: endpoint) { result in
            completion(Self.decode(DictionaryDocument.self, from: result))
        }
    }

    func delete(
        name: String,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<Void, DictionaryAPIError>) -> Void
    ) {
        send(method: "DELETE", path: [name], body: nil, endpoint: endpoint, acceptable: [204]) { result in
            completion(result.map { _ in () })
        }
    }

    func preview(
        name: String,
        text: String,
        draft: DictionaryContent?,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<DictionaryPreviewResult, DictionaryAPIError>) -> Void
    ) {
        let body = try? JSONEncoder().encode(DictionaryPreviewBody(text: text, draft: draft))
        send(method: "POST", path: [name, "preview"], body: body, endpoint: endpoint) { result in
            completion(Self.decode(DictionaryPreviewResult.self, from: result))
        }
    }

    private func send(
        method: String,
        path: [String],
        body: Data?,
        endpoint: DictionaryEndpoint,
        acceptable: Set<Int> = [200],
        completion: @escaping (Result<Data, DictionaryAPIError>) -> Void
    ) {
        switch Self.buildRequest(method: method, path: path, body: body, endpoint: endpoint) {
        case .failure(let error):
            DispatchQueue.main.async { completion(.failure(error)) }
        case .success(let request):
            session.dataTask(with: request) { data, response, error in
                let result = Self.check(data: data, response: response, error: error, acceptable: acceptable)
                DispatchQueue.main.async { completion(result) }
            }.resume()
        }
    }
}
