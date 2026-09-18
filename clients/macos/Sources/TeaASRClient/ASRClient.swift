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
        case failed(String)
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
            state = .failed("找不到服務的 token：\(settings.tokenFile.path)\n請先啟動 tea-asr serve。")
            return
        }

        var request = URLRequest(url: settings.streamURL)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: .ephemeral)
        let task = session.webSocketTask(with: request)
        self.session = session
        self.task = task
        task.resume()
        receive()
    }

    func stop() {
        queue.async {
            guard let task = self.task, !self.stopping else { return }
            self.stopping = true
            let through: Any = self.nextSeq == 0 ? NSNull() : Int(self.nextSeq - 1)
            let payload: [String: Any] = [
                "type": "session.stop",
                "request_id": "mac-stop",
                "through_seq": through,
            ]
            self.send(json: payload)
            _ = task
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
                self.fail("送出速度跟不上服務的流量窗口，session 已停止（不會靜默丟掉音訊）。")
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
                if let error { self?.fail("送出音訊失敗：\(error.localizedDescription)") }
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
            if let error { self?.fail("送出控制訊息失敗：\(error.localizedDescription)") }
        }
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.queue.async {
                    guard !self.stopping else {
                        self.teardown()
                        return
                    }
                    self.fail("連線中斷：\(error.localizedDescription)")
                }
            case .success(let message):
                if case .string(let text) = message {
                    self.queue.async { self.handle(text: text) }
                }
                self.receive()
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
                    fail("模型無法使用（\(hello.modelState)）。")
                    return
                }
                modelWaits += 1
                state = .loadingModel
                task?.cancel(with: .normalClosure, reason: nil)
                teardown(keepState: true)
                queue.asyncAfter(deadline: .now() + Self.modelWaitDelay) { [weak self] in
                    self?.reallyConnect()
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
            fail("\(item.code)：\(item.message)")
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

    private func fail(_ message: String) {
        state = .failed(message)
        task?.cancel(with: .normalClosure, reason: nil)
        teardown(keepState: true)
    }

    private func teardown(keepState: Bool = false) {
        task = nil
        session?.invalidateAndCancel()
        session = nil
        started = false
        backlog.removeAll()
        if !keepState { state = .idle }
    }
}
