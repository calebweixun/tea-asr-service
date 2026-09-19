import Foundation

/// The authenticated status returned by GET /v1/status.
///
/// These types intentionally mirror the public HTTP contract instead of
/// exposing a dictionary to the UI. Unknown fields remain forward compatible
/// because Decodable ignores fields it does not know about.
struct ServerStatus: Decodable, Equatable {
    let modelState: String
    let model: String
    let modelRevision: String
    let workerGeneration: Int
    let workerLoadMs: Int?
    let lastError: String?
    let idleS: Int
    let activeSessions: Int
    let queue: QueueStatus

    enum CodingKeys: String, CodingKey {
        case modelState = "model_state"
        case model
        case modelRevision = "model_revision"
        case workerGeneration = "worker_generation"
        case workerLoadMs = "worker_load_ms"
        case lastError = "last_error"
        case idleS = "idle_s"
        case activeSessions = "active_sessions"
        case queue
    }
}

struct QueueStatus: Decodable, Equatable {
    let waitingTasks: Int
    let waitingSamples: Int
    let maxWaitingTasks: Int
    let maxWaitingSamples: Int

    enum CodingKeys: String, CodingKey {
        case waitingTasks = "waiting_tasks"
        case waitingSamples = "waiting_samples"
        case maxWaitingTasks = "max_waiting_tasks"
        case maxWaitingSamples = "max_waiting_samples"
    }
}

/// The authenticated capability document returned by GET /v1/capabilities.
struct Capabilities: Decodable, Equatable {
    let protocolVersion: String
    let audio: CapabilityAudio
    let profiles: [String]
    let features: CapabilityFeatures
    let limits: CapabilityLimits

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case audio
        case profiles
        case features
        case limits
    }
}

struct CapabilityAudio: Decodable, Equatable {
    let sampleRate: Int
    let channels: Int
    let format: String

    enum CodingKeys: String, CodingKey {
        case sampleRate = "sample_rate"
        case channels
        case format
    }
}

struct CapabilityFeatures: Decodable, Equatable {
    let nativeAudioStreaming: Bool
    let partialTranscripts: Bool
    let wordTimestamps: Bool
    let translation: Bool
    let diarization: Bool
    let hotwords: Bool
    let contextBiasing: Bool
    let durableSessions: Bool
    let durableRevisable: Bool
    let batchJobs: Bool

    enum CodingKeys: String, CodingKey {
        case nativeAudioStreaming = "native_audio_streaming"
        case partialTranscripts = "partial_transcripts"
        case wordTimestamps = "word_timestamps"
        case translation
        case diarization
        case hotwords
        case contextBiasing = "context_biasing"
        case durableSessions = "durable_sessions"
        case durableRevisable = "durable_revisable"
        case batchJobs = "batch_jobs"
    }
}

struct CapabilityLimits: Decodable, Equatable {
    let maxFramePCMBytes: Int
    let maxUtteranceMs: Int
    let maxContinuousSessions: Int
    let maxTotalConnections: Int

    enum CodingKeys: String, CodingKey {
        case maxFramePCMBytes = "max_frame_pcm_bytes"
        case maxUtteranceMs = "max_utterance_ms"
        case maxContinuousSessions = "max_continuous_sessions"
        case maxTotalConnections = "max_total_connections"
    }
}

struct ReadyStatus: Decodable, Equatable {
    let status: String
}

/// A consistent snapshot used by the menu bar and settings UI.
struct ServiceSnapshot: Equatable {
    let healthzOK: Bool
    let readyzOK: Bool
    let readyState: String
    let status: ServerStatus
    let capabilities: Capabilities
}

enum ServiceProbeError: LocalizedError, Equatable {
    case invalidEndpoint
    case unreachable(String)
    case unauthorized
    case forbidden
    case unexpectedStatus(Int)
    case invalidResponse(String)

    /// An HTTP response, even an authentication failure, proves that the
    /// process is reachable. This lets the UI distinguish offline from a bad
    /// token without ever displaying the token itself.
    var serviceReachable: Bool {
        switch self {
        case .invalidEndpoint, .unreachable:
            return false
        case .unauthorized, .forbidden, .unexpectedStatus, .invalidResponse:
            return true
        }
    }

    var retryable: Bool {
        switch self {
        case .invalidEndpoint, .unauthorized, .forbidden:
            return false
        case .unreachable, .unexpectedStatus, .invalidResponse:
            return true
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidEndpoint:
            return "服務位址無效。"
        case .unreachable(let message):
            return message.isEmpty ? "無法連到 TEA ASR 服務。" : "無法連到 TEA ASR 服務：\(message)"
        case .unauthorized:
            return "服務拒絕了 token，請確認服務設定。"
        case .forbidden:
            return "服務拒絕了這個請求。"
        case .unexpectedStatus(let code):
            return "服務回應 HTTP \(code)。"
        case .invalidResponse(let endpoint):
            return "服務的 \(endpoint) 回應格式無法辨識。"
        }
    }
}

/// Fetches all status endpoints needed by the client UI.
final class ServiceProbe {
    typealias Completion = (Result<ServiceSnapshot, ServiceProbeError>) -> Void

    private let session: URLSession
    private let callbackQueue = DispatchQueue(label: "tea-asr.service-probe")

    init(session: URLSession = .shared) {
        self.session = session
    }

    func refresh(
        host: String,
        port: Int,
        token: String?,
        completion: @escaping Completion
    ) {
        guard
            !host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            (1...65_535).contains(port),
            let baseURL = Self.baseURL(host: host, port: port)
        else {
            DispatchQueue.main.async { completion(.failure(.invalidEndpoint)) }
            return
        }

        request(path: "/healthz", baseURL: baseURL, token: nil, acceptable: [200]) { [weak self] health in
            guard let self else { return }
            switch health {
            case .failure(let error):
                self.finish(.failure(error), completion: completion)
            case .success:
                self.fetchDetails(baseURL: baseURL, token: token, completion: completion)
            }
        }
    }

    private func fetchDetails(
        baseURL: URL,
        token: String?,
        completion: @escaping Completion
    ) {
        var readyResult: Result<ReadyStatus, ServiceProbeError>?
        var statusResult: Result<ServerStatus, ServiceProbeError>?
        var capabilitiesResult: Result<Capabilities, ServiceProbeError>?
        let group = DispatchGroup()

        group.enter()
        request(path: "/readyz", baseURL: baseURL, token: nil, acceptable: [200, 503]) { [weak self] result in
            guard let self else { return }
            readyResult = result.flatMap { self.decode(ReadyStatus.self, from: $0, endpoint: "/readyz") }
            group.leave()
        }

        group.enter()
        request(path: "/v1/status", baseURL: baseURL, token: token, acceptable: [200]) { [weak self] result in
            guard let self else { return }
            statusResult = result.flatMap { self.decode(ServerStatus.self, from: $0, endpoint: "/v1/status") }
            group.leave()
        }

        group.enter()
        request(path: "/v1/capabilities", baseURL: baseURL, token: token, acceptable: [200]) { [weak self] result in
            guard let self else { return }
            capabilitiesResult = result.flatMap {
                self.decode(Capabilities.self, from: $0, endpoint: "/v1/capabilities")
            }
            group.leave()
        }

        group.notify(queue: callbackQueue) { [weak self] in
            guard let self else { return }
            guard
                let readyResult,
                let statusResult,
                let capabilitiesResult
            else {
                self.finish(.failure(.invalidResponse("status")), completion: completion)
                return
            }
            if case .failure(let error) = statusResult {
                self.finish(.failure(error), completion: completion)
                return
            }
            if case .failure(let error) = capabilitiesResult {
                self.finish(.failure(error), completion: completion)
                return
            }
            if case .failure(let error) = readyResult {
                self.finish(.failure(error), completion: completion)
                return
            }
            guard
                case .success(let ready) = readyResult,
                case .success(let status) = statusResult,
                case .success(let capabilities) = capabilitiesResult
            else {
                self.finish(.failure(.invalidResponse("status")), completion: completion)
                return
            }
            self.finish(
                .success(
                    ServiceSnapshot(
                        healthzOK: true,
                        readyzOK: ready.status == "ready",
                        readyState: ready.status,
                        status: status,
                        capabilities: capabilities
                    )
                ),
                completion: completion
            )
        }
    }

    private func request(
        path: String,
        baseURL: URL,
        token: String?,
        acceptable: Set<Int>,
        completion: @escaping (Result<Data, ServiceProbeError>) -> Void
    ) {
        var request = URLRequest(url: baseURL.appendingPathComponent(String(path.dropFirst())))
        request.timeoutInterval = 1.5
        if let token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        session.dataTask(with: request) { [callbackQueue] data, response, error in
            callbackQueue.async {
                if let error {
                    completion(.failure(.unreachable(error.localizedDescription)))
                    return
                }
                guard let response = response as? HTTPURLResponse else {
                    completion(.failure(.invalidResponse(path)))
                    return
                }
                guard acceptable.contains(response.statusCode) else {
                    switch response.statusCode {
                    case 401:
                        completion(.failure(.unauthorized))
                    case 403:
                        completion(.failure(.forbidden))
                    default:
                        completion(.failure(.unexpectedStatus(response.statusCode)))
                    }
                    return
                }
                completion(.success(data ?? Data()))
            }
        }.resume()
    }

    private func decode<T: Decodable>(
        _ type: T.Type,
        from data: Data,
        endpoint: String
    ) -> Result<T, ServiceProbeError> {
        do {
            return .success(try JSONDecoder().decode(type, from: data))
        } catch {
            // Do not include the response body in diagnostics: status endpoints
            // must never become an accidental transcript/token logging path.
            return .failure(.invalidResponse(endpoint))
        }
    }

    private func finish(_ result: Result<ServiceSnapshot, ServiceProbeError>, completion: @escaping Completion) {
        DispatchQueue.main.async {
            completion(result)
        }
    }

    private static func baseURL(host: String, port: Int) -> URL? {
        var components = URLComponents()
        components.scheme = "http"
        components.host = host.trimmingCharacters(in: .whitespacesAndNewlines)
        components.port = port
        return components.url
    }
}
