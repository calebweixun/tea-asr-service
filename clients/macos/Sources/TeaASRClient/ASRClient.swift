import Foundation

/// Drives one continuous session against the local service.
///
/// Everything the wire contract requires of a client lives here: the flow
/// window is respected, sample clock stays contiguous, and only `final` text is
/// ever treated as committed.
final class ASRClient: NSObject {
    enum State: Equatable {
        case idle
        case connecting
        case loadingModel
        case listening(protocolVersion: String, preview: Bool)
        case failed(ConnectionIssue)
    }

    /// The service unloads the model when idle, and the first connection is what
    /// wakes it. Retrying beats telling the user to try again themselves.
    private static let modelWaitAttempts = 30
    private static let modelWaitDelay: TimeInterval = 2.0

    private let settings: Settings
    private var task: URLSessionWebSocketTask?
    private var session: URLSession?

    private var nextSeq: UInt64 = 0
    private var nextSample: UInt64 = 0
    private var sendUntilSample: UInt64 = 0
    private var started = false
    private var stopping = false

    /// Audio that arrived while the flow window was closed. Bounded: the mic
    /// keeps producing, so an unbounded queue would grow without limit.
    private var backlog: [Data] = []
    private let backlogLimit = 150  // 15 s at 100 ms per frame
    private var wantsPreview = false
    private var modelWaits = 0

    private let queue = DispatchQueue(label: "tea-asr.client")

    var onState: ((State) -> Void)?
    var onPartial: ((Wire.Transcript) -> Void)?
    var onFinal: ((Wire.Transcript) -> Void)?
    var onNotice: ((String) -> Void)?
    /// The service says the sample clock broke (usually the machine slept).
    /// Recoverable by starting a fresh session, so it is not a failure.
    var onTimelineGap: ((String) -> Void)?
    /// A session became live; the wall clock of its sample 0.
    var onSessionOrigin: ((Date) -> Void)?

    private(set) var state: State = .idle {
        didSet {
            let value = state
            DispatchQueue.main.async { [weak self] in self?.onState?(value) }
        }
    }

    init(settings: Settings) {
        self.settings = settings
        super.init()
    }

    func connect(wantsPreview: Bool) {
        queue.async {
            self.wantsPreview = wantsPreview
            self.modelWaits = 0
            self.reallyConnect()
        }
    }

    private func reallyConnect() {
        guard task == nil, !stopping else { return }
        stopping = false
        started = false
        nextSeq = 0
        nextSample = 0
        sendUntilSample = 0
        backlog.removeAll()
        state = .connecting

        let token: String
        do {
            token = try settings.token()
        } catch {
            state = .failed(
                ConnectionIssue(
                    code: "token_missing",
                    message: "找不到服務的 token：\(settings.tokenFile.path)\n請先啟動 tea-asr serve。",
                    retryable: false,
                    closeCode: nil
                )
            )
            return
        }

        var request = URLRequest(url: settings.streamURL)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: .ephemeral)
        let task = session.webSocketTask(with: request)
        self.session = session
        self.task = task
        task.resume()
        receive(on: task)
    }

    func stop() {
        queue.async {
            guard !self.stopping else { return }
            // Set this first: while waiting for the model to load there is no
            // task yet, and a pending reconnect would otherwise ignore the stop.
            self.stopping = true
            guard self.task != nil else {
                self.teardown()
                return
            }
            let through: Any = self.nextSeq == 0 ? NSNull() : Int(self.nextSeq - 1)
            self.send(json: [
                "type": "session.stop",
                "request_id": "mac-stop",
                "through_seq": through,
            ])
        }
    }

    func cancel() {
        queue.async {
            self.task?.cancel(with: .goingAway, reason: nil)
            self.teardown()
        }
    }

    /// Hand one frame of 16 kHz mono PCM16 to the session.
    func send(pcm: Data) {
        queue.async {
            guard self.started, !self.stopping else { return }
            self.backlog.append(pcm)
            if self.backlog.count > self.backlogLimit {
                // Dropping frames here would splice the audio while the sample
                // clock kept counting, so the server would see a continuous
                // timeline that never happened. docs/04 forbids that, so the
                // session ends loudly instead.
                self.fail(
                    ConnectionIssue(
                        code: "flow_control_backlog",
                        message: "送出速度跟不上服務的流量窗口，session 已停止（不會靜默丟掉音訊）。",
                        retryable: true,
                        closeCode: nil
                    )
                )
                return
            }
            self.drain()
        }
    }

    private func drain() {
        guard let task else { return }
        while let frame = backlog.first {
            let samples = UInt64(frame.count / 2)
            guard nextSample + samples <= sendUntilSample else { return }
            backlog.removeFirst()
            let message = Wire.frame(seq: nextSeq, startSample: nextSample, pcm: frame)
            nextSeq += 1
            nextSample += samples
            task.send(.data(message)) { [weak self] error in
                guard let self, let error else { return }
                let issue = ConnectionIssue(
                    code: "audio_send_failed",
                    message: "送出音訊失敗：\(error.localizedDescription)",
                    retryable: true,
                    closeCode: nil
                )
                self.queue.async { [weak self] in
                    guard let self, self.task === task else { return }
                    self.fail(issue)
                }
            }
        }
    }

    private func send(json: [String: Any]) {
        guard
            let task,
            let data = try? JSONSerialization.data(withJSONObject: json),
            let text = String(data: data, encoding: .utf8)
        else { return }
        task.send(.string(text)) { [weak self] error in
            guard let self, let error else { return }
            let issue = ConnectionIssue(
                code: "control_send_failed",
                message: "送出控制訊息失敗：\(error.localizedDescription)",
                retryable: true,
                closeCode: nil
            )
            self.queue.async { [weak self] in
                guard let self, self.task === task else { return }
                self.fail(issue)
            }
        }
    }

    private func receive(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.queue.async {
                    // A socket we deliberately replaced (stop, or a reconnect
                    // while the model loads) reports a failure on the way out.
                    // That is not the current connection's problem.
                    guard self.task === task else { return }
                    guard !self.stopping else {
                        self.teardown()
                        return
                    }
                    self.fail(
                        ConnectionIssue.fromCloseCode(
                            task.closeCode.rawValue,
                            message: "連線中斷：\(error.localizedDescription)"
                        )
                    )
                }
            case .success(let message):
                if case .string(let text) = message {
                    self.queue.async {
                        guard self.task === task else { return }
                        self.handle(text: text)
                    }
                }
                self.receive(on: task)
            }
        }
    }

    private func handle(text: String) {
        guard
            let data = text.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let type = object["type"] as? String
        else { return }

        let decoder = JSONDecoder()
        switch type {
        case "hello":
            guard let hello = try? decoder.decode(Wire.Hello.self, from: data) else { return }
            guard hello.modelState == "ready" else {
                // Connecting is itself what asks the service to load the model,
                // so wait for it instead of handing the user an error.
                guard
                    hello.modelState == "loading" || hello.modelState == "idle_unloaded",
                    modelWaits < Self.modelWaitAttempts
                else {
                    fail(
                        ConnectionIssue(
                            code: "model_unavailable",
                            message: "模型無法使用（\(hello.modelState)）。",
                            retryable: false,
                            closeCode: nil
                        )
                    )
                    return
                }
                modelWaits += 1
                state = .loadingModel
                task?.cancel(with: .normalClosure, reason: nil)
                teardown(keepState: true)
                queue.asyncAfter(deadline: .now() + Self.modelWaitDelay) { [weak self] in
                    guard let self, !self.stopping else { return }
                    self.reallyConnect()
                }
                return
            }
            startSession(preview: wantsPreview && hello.protocolVersion == "1.1")
        case "session.started":
            guard let started = try? decoder.decode(Wire.SessionStarted.self, from: data) else {
                return
            }
            sendUntilSample = UInt64(started.sendUntilSample)
            self.started = true
            let origin = Date()
            DispatchQueue.main.async { [weak self] in self?.onSessionOrigin?(origin) }
            state = .listening(
                protocolVersion: started.transcriptMode == "revisable" ? "1.1" : "1.0",
                preview: started.transcriptMode == "revisable"
            )
            drain()
        case "flow.control":
            guard let flow = try? decoder.decode(Wire.FlowControl.self, from: data) else { return }
            sendUntilSample = max(sendUntilSample, UInt64(flow.sendUntilSample))
            drain()
        case "transcript.partial":
            guard let item = try? decoder.decode(Wire.Transcript.self, from: data) else { return }
            DispatchQueue.main.async { [weak self] in self?.onPartial?(item) }
        case "transcript.final":
            guard let item = try? decoder.decode(Wire.Transcript.self, from: data) else { return }
            DispatchQueue.main.async { [weak self] in self?.onFinal?(item) }
        case "segment.error":
            guard let item = try? decoder.decode(Wire.SegmentTerminal.self, from: data) else {
                return
            }
            notify("片段辨識失敗：\(item.code ?? "unknown")")
        case "error":
            guard let item = try? decoder.decode(Wire.ErrorEvent.self, from: data) else { return }
            if item.code == "timeline_gap" {
                // Keep the state: going idle here would look like a clean stop
                // to the caller, which is exactly what this is not.
                teardown(keepState: true)
                let message = item.message
                DispatchQueue.main.async { [weak self] in self?.onTimelineGap?(message) }
                return
            }
            fail(ConnectionIssue.fromWire(code: item.code, message: item.message, retryable: item.retryable))
        case "session.stopped", "session.cancelled":
            teardown()
        default:
            break
        }
    }

    private func startSession(preview: Bool) {
        send(json: [
            "type": "session.start",
            "request_id": "mac-start",
            "profile": "continuous",
            "audio": ["sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"],
            "language": "Chinese",
            "durable": false,
            "transcript_mode": preview ? "revisable" : "final_only",
        ])
    }

    private func notify(_ message: String) {
        DispatchQueue.main.async { [weak self] in self?.onNotice?(message) }
    }

    private func fail(_ issue: ConnectionIssue) {
        state = .failed(issue)
        task?.cancel(with: .normalClosure, reason: nil)
        teardown(keepState: true)
    }

    private func teardown(keepState: Bool = false) {
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
        started = false
        backlog.removeAll()
        if !keepState { state = .idle }
    }
}
