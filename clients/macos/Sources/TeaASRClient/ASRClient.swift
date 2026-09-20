import Foundation

/// Serial-queue owned token used to reject callbacks from a replaced socket.
/// Keeping this tiny policy value separate makes the stale-callback contract
/// unit-testable without requiring a live URLSession server.
struct SessionGeneration {
    private(set) var current: UInt64 = 0

    @discardableResult
    mutating func begin() -> UInt64 {
        current &+= 1
        return current
    }

    @discardableResult
    mutating func invalidate() -> UInt64 {
        current &+= 1
        return current
    }

    func accepts(_ token: UInt64) -> Bool {
        token == current
    }
}

/// Why a session that still *looks* live stopped making progress.
enum SessionStall: Equatable {
    /// No PCM frame reached the client for this long: the microphone tap, the
    /// audio engine or the capture queue stopped feeding us.
    case audioStopped(seconds: TimeInterval)
    /// Nothing at all arrived from the service for this long. A live session
    /// gets one `audio.ack` per frame plus a `pong` per keepalive, so this
    /// means the socket died without reporting it.
    case serverSilent(seconds: TimeInterval)

    var issue: ConnectionIssue {
        switch self {
        case .audioStopped(let seconds):
            return ConnectionIssue(
                code: "audio_stalled",
                message: "已經 \(Int(seconds.rounded())) 秒沒有收到麥克風音訊，錄音已停止。請重新開始聆聽。",
                retryable: true,
                closeCode: nil
            )
        case .serverSilent(let seconds):
            return ConnectionIssue(
                code: "connection_stalled",
                message: "已經 \(Int(seconds.rounded())) 秒沒有收到服務的回應，連線已中斷。請重新開始聆聽。",
                retryable: true,
                closeCode: nil
            )
        }
    }
}

/// Liveness bookkeeping for one live session.
///
/// docs/06 constraint 4 (visible failure) is the whole point: a session that
/// quietly stops working must never keep showing "listening". Nothing here
/// touches URLSession or AVAudioEngine, so the whole policy — including the
/// keepalive cadence that keeps a *silent* session alive — is unit-testable.
struct SessionWatchdog {
    enum Action: Equatable {
        case none
        /// Send an application-level `ping`. A long pause still carries audio
        /// frames, but a keepalive proves the socket in both directions
        /// without depending on the audio path being healthy.
        case keepalive
        case fail(SessionStall)
    }

    /// Frames arrive every 100 ms, so 4 s is 40 missed frames: far outside
    /// normal scheduling jitter, still fast enough that the user sees the
    /// overlay change instead of talking into a void.
    static let audioStallTimeout: TimeInterval = 4.0
    /// An idle session still produces one `audio.ack` per frame plus a `pong`
    /// per keepalive. Two missed keepalive round-trips is the budget.
    static let serverSilenceTimeout: TimeInterval = 20.0
    /// Comfortably inside the service's own 120 s idle timeout, so a session
    /// whose audio path hiccups does not lose the connection as well.
    static let keepaliveInterval: TimeInterval = 10.0
    /// How often the owner should call `tick`.
    static let checkInterval: TimeInterval = 1.0

    let audioStallTimeout: TimeInterval
    let serverSilenceTimeout: TimeInterval
    let keepaliveInterval: TimeInterval

    private(set) var isArmed = false
    private(set) var lastFrameAt: Date?
    private(set) var lastServerEventAt: Date?
    private(set) var lastKeepaliveAt: Date?

    init(
        audioStallTimeout: TimeInterval = SessionWatchdog.audioStallTimeout,
        serverSilenceTimeout: TimeInterval = SessionWatchdog.serverSilenceTimeout,
        keepaliveInterval: TimeInterval = SessionWatchdog.keepaliveInterval
    ) {
        self.audioStallTimeout = audioStallTimeout
        self.serverSilenceTimeout = serverSilenceTimeout
        self.keepaliveInterval = keepaliveInterval
    }

    mutating func arm(at now: Date) {
        isArmed = true
        lastFrameAt = now
        lastServerEventAt = now
        lastKeepaliveAt = now
    }

    mutating func disarm() {
        isArmed = false
        lastFrameAt = nil
        lastServerEventAt = nil
        lastKeepaliveAt = nil
    }

    mutating func noteFrame(at now: Date) {
        guard isArmed else { return }
        lastFrameAt = now
    }

    mutating func noteServerEvent(at now: Date) {
        guard isArmed else { return }
        lastServerEventAt = now
    }

    /// The server is checked first: when the socket dies the audio path often
    /// stalls too, and "the service stopped answering" is the more actionable
    /// of the two messages.
    mutating func tick(now: Date) -> Action {
        guard isArmed else { return .none }
        if let lastServerEventAt {
            let silent = now.timeIntervalSince(lastServerEventAt)
            if silent >= serverSilenceTimeout { return .fail(.serverSilent(seconds: silent)) }
        }
        if let lastFrameAt {
            let stalled = now.timeIntervalSince(lastFrameAt)
            if stalled >= audioStallTimeout { return .fail(.audioStopped(seconds: stalled)) }
        }
        if let lastKeepaliveAt, now.timeIntervalSince(lastKeepaliveAt) >= keepaliveInterval {
            self.lastKeepaliveAt = now
            return .keepalive
        }
        return .none
    }
}

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
    /// Monotonically identifies the socket/session currently owned by this
    /// client. URLSession can still deliver callbacks after cancellation, so
    /// task identity alone is not enough when a new task is created quickly.
    private var generation = SessionGeneration()
    private var pendingConnectPreview: Bool?

    /// Audio that arrived before the session became ready, or while the flow
    /// window was closed. Bounded: an unavailable service must not grow memory
    /// without limit, and a full buffer is reported instead of dropping audio.
    private var backlog = AudioPreRollBuffer(capacity: 150)  // 15 s at 100 ms
    private var wantsPreview = false
    private var modelWaits = 0

    /// Liveness for the running session. Owned by `queue` like everything else.
    private var watchdog = SessionWatchdog()
    private var watchdogTimer: DispatchSourceTimer?
    private var keepaliveCounter: UInt64 = 0

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
            if self.stopping {
                // A caller may request the next session while the previous
                // one is draining its final transcript. Defer it until the
                // server's session.stopped (or the socket failure) completes
                // the old generation.
                self.pendingConnectPreview = wantsPreview
                return
            }
            self.reallyConnect()
        }
    }

    private func reallyConnect() {
        guard task == nil, !stopping else { return }
        pendingConnectPreview = nil
        stopping = false
        started = false
        nextSeq = 0
        nextSample = 0
        sendUntilSample = 0
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
        let generation = self.generation.begin()
        self.session = session
        self.task = task
        task.resume()
        receive(on: task, generation: generation)
    }

    func stop() {
        queue.async {
            guard !self.stopping else { return }
            // Set this first: while waiting for the model to load there is no
            // task yet, and a pending reconnect would otherwise ignore the stop.
            self.stopping = true
            // The caller stops the microphone before stopping us, and the
            // service may take a while to drain the last segment. Both look
            // exactly like a stall from here, so the watchdog stands down;
            // "停止中…等待最後一句" is already an honest description of it.
            self.stopWatchdog()
            guard self.task != nil else {
                self.teardown()
                return
            }
            let through: Any = self.nextSeq == 0 ? NSNull() : Int(self.nextSeq - 1)
            self.send(json: [
                "type": "session.stop",
                "request_id": "mac-stop",
                "through_seq": through,
            ], task: self.task, generation: self.generation.current)
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
            guard !self.stopping else { return }
            self.watchdog.noteFrame(at: Date())
            guard self.backlog.append(pcm) else {
                // Dropping frames here would splice the audio while the sample
                // clock kept counting, so the server would see a continuous
                // timeline that never happened. docs/04 forbids that, so the
                // session ends loudly instead. This also covers a model that
                // remains loading before `session.started`.
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
        let generation = self.generation.current
        while let frame = backlog.removeFirst() {
            let samples = UInt64(frame.count / 2)
            guard nextSample + samples <= sendUntilSample else {
                var pending = AudioPreRollBuffer(capacity: backlog.capacity)
                _ = pending.append(frame)
                for queued in backlog.frames { _ = pending.append(queued) }
                backlog = pending
                return
            }
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
                    guard
                        let self,
                        self.task === task,
                        self.generation.accepts(generation)
                    else { return }
                    self.fail(issue)
                }
            }
        }
    }

    private func send(
        json: [String: Any],
        task expectedTask: URLSessionWebSocketTask? = nil,
        generation expectedGeneration: UInt64? = nil
    ) {
        guard
            let task = expectedTask ?? self.task,
            let data = try? JSONSerialization.data(withJSONObject: json),
            let text = String(data: data, encoding: .utf8)
        else { return }
        let generation = expectedGeneration ?? self.generation.current
        task.send(.string(text)) { [weak self] error in
            guard let self, let error else { return }
            let issue = ConnectionIssue(
                code: "control_send_failed",
                message: "送出控制訊息失敗：\(error.localizedDescription)",
                retryable: true,
                closeCode: nil
            )
            self.queue.async { [weak self] in
                guard
                    let self,
                    self.task === task,
                    self.generation.accepts(generation)
                else { return }
                self.fail(issue)
            }
        }
    }

    private func receive(on task: URLSessionWebSocketTask, generation: UInt64) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.queue.async {
                    // A socket we deliberately replaced (stop, or a reconnect
                    // while the model loads) reports a failure on the way out.
                    // That is not the current connection's problem.
                    guard self.task === task, self.generation.accepts(generation) else { return }
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
                self.queue.async {
                    guard self.task === task, self.generation.accepts(generation) else { return }
                    // Any frame from the service — including the per-audio-frame
                    // `audio.ack` and the keepalive `pong` — proves the socket
                    // is still carrying traffic.
                    self.watchdog.noteServerEvent(at: Date())
                    if case .string(let text) = message {
                        self.handle(text: text)
                    }
                    guard self.task === task, self.generation.accepts(generation) else { return }
                    self.receive(on: task, generation: generation)
                }
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
                teardown(keepState: true, preserveBacklog: true)
                let waitingGeneration = self.generation.current
                queue.asyncAfter(deadline: .now() + Self.modelWaitDelay) { [weak self] in
                    guard
                        let self,
                        !self.stopping,
                        self.task == nil,
                        self.generation.accepts(waitingGeneration)
                    else { return }
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
            let bufferedSamples = backlog.frames.reduce(UInt64(0)) { total, frame in
                total + UInt64(frame.count / 2)
            }
            let origin = Date().addingTimeInterval(-Double(bufferedSamples) / Double(Wire.sampleRate))
            DispatchQueue.main.async { [weak self] in self?.onSessionOrigin?(origin) }
            state = .listening(
                protocolVersion: started.transcriptMode == "revisable" ? "1.1" : "1.0",
                preview: started.transcriptMode == "revisable"
            )
            startWatchdog()
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

    // MARK: - Liveness

    /// Arm the liveness watchdog for the session that just became live.
    ///
    /// Two jobs, and both are required: the keepalive keeps a *silent* session
    /// connected (the service drops a connection that sends nothing for 120 s),
    /// and the stall checks make sure that when the session dies anyway the
    /// state leaves `.listening` at once instead of lying to the user.
    private func startWatchdog() {
        stopWatchdog()
        watchdog.arm(at: Date())
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(
            deadline: .now() + SessionWatchdog.checkInterval,
            repeating: SessionWatchdog.checkInterval,
            leeway: .milliseconds(200)
        )
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            switch self.watchdog.tick(now: Date()) {
            case .none:
                return
            case .keepalive:
                self.keepaliveCounter &+= 1
                self.send(json: [
                    "type": "ping",
                    "request_id": "mac-keepalive-\(self.keepaliveCounter)",
                ])
            case .fail(let stall):
                self.fail(stall.issue)
            }
        }
        watchdogTimer = timer
        timer.resume()
    }

    private func stopWatchdog() {
        watchdogTimer?.cancel()
        watchdogTimer = nil
        watchdog.disarm()
    }

    private func notify(_ message: String) {
        DispatchQueue.main.async { [weak self] in self?.onNotice?(message) }
    }

    private func fail(_ issue: ConnectionIssue) {
        state = .failed(issue)
        pendingConnectPreview = nil
        stopping = false
        task?.cancel(with: .normalClosure, reason: nil)
        teardown(keepState: true)
    }

    private func teardown(keepState: Bool = false, preserveBacklog: Bool = false) {
        // Invalidate callbacks from the socket being torn down before clearing
        // the references. A new connection can then safely use the same client
        // without an old receive/send completion changing its state.
        generation.invalidate()
        // Must come before the socket goes away: a tick that fires during
        // teardown would otherwise report a stall for a session that is
        // already ending on purpose.
        stopWatchdog()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
        started = false
        if !preserveBacklog {
            backlog.removeAll()
        }
        if !keepState {
            state = .idle
            stopping = false
            if let wantsPreview = pendingConnectPreview {
                pendingConnectPreview = nil
                self.wantsPreview = wantsPreview
                modelWaits = 0
                queue.async { [weak self] in self?.reallyConnect() }
            }
        }
    }
}
