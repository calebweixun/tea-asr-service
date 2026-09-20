import AVFoundation
import CoreAudio
import Foundation

/// Keeping RMS and peak together prevents the bar from mixing values from
/// different audio buffers.
struct AudioLevelSample: Equatable {
    let rms: Float
    let peak: Float

    static let zero = AudioLevelSample(rms: 0, peak: 0)
}

/// Pure level math keeps the audio callback small and makes the signal policy testable without hardware.
enum AudioLevelMath {
    private struct Accumulator {
        var sumOfSquares: Double = 0
        var peak: Float = 0
        var sampleCount = 0

        mutating func append(_ sample: Float) {
            guard sample.isFinite else { return }
            let clamped = max(-1, min(1, sample))
            sumOfSquares += Double(clamped) * Double(clamped)
            peak = max(peak, abs(clamped))
            sampleCount += 1
        }

        var result: AudioLevelSample? {
            guard sampleCount > 0 else { return nil }
            return AudioLevelSample(
                rms: Float(sqrt(sumOfSquares / Double(sampleCount))),
                peak: peak
            )
        }
    }

    static func measure(samples: [Float]) -> AudioLevelSample? {
        var accumulator = Accumulator()
        for sample in samples {
            accumulator.append(sample)
        }
        return accumulator.result
    }

    static func measure(buffer: AVAudioPCMBuffer) -> AudioLevelSample? {
        let frameCount = Int(buffer.frameLength)
        let channelCount = Int(buffer.format.channelCount)
        guard frameCount > 0, channelCount > 0 else { return nil }

        var accumulator = Accumulator()
        if let channels = buffer.floatChannelData {
            if buffer.format.isInterleaved {
                let samples = UnsafeBufferPointer(
                    start: channels[0],
                    count: frameCount * channelCount
                )
                for sample in samples {
                    accumulator.append(sample)
                }
            } else {
                for channel in 0..<channelCount {
                    let samples = UnsafeBufferPointer(
                        start: channels[channel],
                        count: frameCount
                    )
                    for sample in samples {
                        accumulator.append(sample)
                    }
                }
            }
            return accumulator.result
        }

        if let channels = buffer.int16ChannelData {
            let scale = Float(1 << 15)
            if buffer.format.isInterleaved {
                let samples = UnsafeBufferPointer(
                    start: channels[0],
                    count: frameCount * channelCount
                )
                for sample in samples {
                    accumulator.append(Float(sample) / scale)
                }
            } else {
                for channel in 0..<channelCount {
                    let samples = UnsafeBufferPointer(
                        start: channels[channel],
                        count: frameCount
                    )
                    for sample in samples {
                        accumulator.append(Float(sample) / scale)
                    }
                }
            }
            return accumulator.result
        }

        if let channels = buffer.int32ChannelData {
            let scale = Float(Int32.max)
            if buffer.format.isInterleaved {
                let samples = UnsafeBufferPointer(
                    start: channels[0],
                    count: frameCount * channelCount
                )
                for sample in samples {
                    accumulator.append(Float(sample) / scale)
                }
            } else {
                for channel in 0..<channelCount {
                    let samples = UnsafeBufferPointer(
                        start: channels[channel],
                        count: frameCount
                    )
                    for sample in samples {
                        accumulator.append(Float(sample) / scale)
                    }
                }
            }
            return accumulator.result
        }

        return nil
    }
}

/// Attack and release are deliberately separate so speech onset is visible quickly while silence settles gently.
struct AudioLevelSmoother {
    let attack: Float
    let release: Float
    private(set) var current = AudioLevelSample.zero

    init(attack: Float = 0.65, release: Float = 0.18) {
        self.attack = max(0, min(1, attack))
        self.release = max(0, min(1, release))
    }

    mutating func reset() {
        current = .zero
    }

    mutating func update(_ sample: AudioLevelSample) -> AudioLevelSample {
        current = AudioLevelSample(
            rms: smooth(current.rms, target: sample.rms),
            peak: smooth(current.peak, target: sample.peak)
        )
        return current
    }

    private func smooth(_ current: Float, target: Float) -> Float {
        let boundedTarget = max(0, min(1, target.isFinite ? target : 0))
        let coefficient = boundedTarget >= current ? attack : release
        return current + ((boundedTarget - current) * coefficient)
    }
}

/// The monitor and AudioCapture can share this process-local ownership gate during their handoff.
final class AudioInputLease {
    let deviceUID: String

    private let token: UUID
    private let releaseLock = NSLock()
    private var isReleased = false

    fileprivate init(deviceUID: String, token: UUID) {
        self.deviceUID = deviceUID
        self.token = token
    }

    func release() {
        releaseLock.lock()
        guard !isReleased else {
            releaseLock.unlock()
            return
        }
        isReleased = true
        releaseLock.unlock()
        AudioInputLeaseCoordinator.release(deviceUID: deviceUID, token: token)
    }

    deinit {
        release()
    }
}

enum AudioInputLeaseCoordinator {
    private static let lock = NSLock()
    private static var holder: (deviceUID: String, token: UUID)?

    static func acquire(deviceUID: String) throws -> AudioInputLease {
        let normalizedUID = deviceUID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedUID.isEmpty else {
            throw AudioLevelMonitorError.invalidDeviceUID
        }

        lock.lock()
        defer { lock.unlock() }
        guard holder == nil else {
            throw AudioLevelMonitorError.deviceBusy(uid: normalizedUID)
        }

        let token = UUID()
        holder = (normalizedUID, token)
        return AudioInputLease(deviceUID: normalizedUID, token: token)
    }

    fileprivate static func release(deviceUID: String, token: UUID) {
        lock.lock()
        defer { lock.unlock() }
        guard holder?.deviceUID == deviceUID, holder?.token == token else { return }
        holder = nil
    }
}

enum AudioLevelMonitorState: Equatable {
    case idle
    case permissionRequired
    case permissionDenied
    case starting
    case monitoring
    case noData
    case failed(String)
}

enum AudioLevelMonitorError: LocalizedError, Equatable {
    case invalidDeviceUID
    case permissionRequired
    case permissionDenied
    case deviceCatalogFailed(reason: String)
    case inputDeviceUnavailable(uid: String)
    case deviceBusy(uid: String)
    case audioUnitUnavailable(name: String, uid: String)
    case deviceConfigurationFailed(name: String, uid: String, reason: String)
    case invalidNativeFormat(name: String, uid: String, sampleRate: Double, channels: Int)
    case engineStartFailed(name: String, uid: String, reason: String)

    var errorDescription: String? {
        switch self {
        case .invalidDeviceUID:
            return "未指定有效的輸入裝置 UID。"
        case .permissionRequired:
            return "需要麥克風權限才能監看輸入電平。請允許 TEA ASR 使用麥克風。"
        case .permissionDenied:
            return "麥克風權限已被拒絕。請到「系統設定 → 隱私權與安全性 → 麥克風」允許 TEA ASR。"
        case .deviceCatalogFailed(let reason):
            return "無法查詢輸入裝置清單：\(reason)"
        case .inputDeviceUnavailable(let uid):
            return "找不到指定的輸入裝置（UID: \(uid)）；沒有改用系統預設裝置。"
        case .deviceBusy(let uid):
            return "輸入裝置目前由另一個錄音路徑使用中（UID: \(uid)）。請先停止錄音或電平監看。"
        case .audioUnitUnavailable(let name, let uid):
            return "輸入裝置「\(name)」（UID: \(uid)）沒有可用的音訊單元。"
        case .deviceConfigurationFailed(let name, let uid, let reason):
            return "無法開啟輸入裝置「\(name)」（UID: \(uid)）：\(reason)"
        case .invalidNativeFormat(let name, let uid, let sampleRate, let channels):
            return "輸入裝置「\(name)」（UID: \(uid)）回報無效原生格式（\(sampleRate) Hz、\(channels) 聲道）。"
        case .engineStartFailed(let name, let uid, let reason):
            return "無法啟動輸入裝置「\(name)」（UID: \(uid)）：\(reason)"
        }
    }
}

enum AudioLevelMonitorPermissionPolicy {
    static func error(for status: AVAuthorizationStatus) -> AudioLevelMonitorError? {
        switch status {
        case .authorized:
            return nil
        case .notDetermined:
            return .permissionRequired
        case .denied, .restricted:
            return .permissionDenied
        @unknown default:
            return .permissionDenied
        }
    }

    static func state(for status: AVAuthorizationStatus) -> AudioLevelMonitorState? {
        switch status {
        case .authorized:
            return nil
        case .notDetermined:
            return .permissionRequired
        case .denied, .restricted:
            return .permissionDenied
        @unknown default:
            return .permissionDenied
        }
    }
}

enum AudioLevelMonitorNoDataPolicy {
    static func shouldPublishNoData(hasReceivedSample: Bool) -> Bool {
        !hasReceivedSample
    }
}

final class AudioLevelMonitor {
    struct Configuration: Equatable {
        let deviceUID: String
        let deviceName: String?

        init(deviceUID: String, deviceName: String? = nil) {
            self.deviceUID = deviceUID.trimmingCharacters(in: .whitespacesAndNewlines)
            self.deviceName = deviceName
        }
    }

    private let callbackQueue: DispatchQueue
    private let controlQueue = DispatchQueue(label: "com.tea-asr.audio-level-monitor.control")
    private let levelQueue = DispatchQueue(label: "com.tea-asr.audio-level-monitor.level")
    private let stateLock = NSLock()
    private let levelQueueSlots = DispatchSemaphore(value: 2)
    private let deliveryQueueSlots = DispatchSemaphore(value: 2)
    private let noDataTimeout: DispatchTimeInterval

    private var stateValue: AudioLevelMonitorState = .idle
    private var stateHandler: ((AudioLevelMonitorState) -> Void)?
    private var levelHandler: ((AudioLevelSample) -> Void)?
    private var selectedUID: String?
    private var selectedName: String?
    private var generation: UInt64 = 0
    private var hasReceivedSample = false
    private var noDataWorkItem: DispatchWorkItem?
    private var smoother = AudioLevelSmoother()
    private var engine: AVAudioEngine?
    private var lease: AudioInputLease?
    private var tapInstalled = false
    private var isActive = false

    init(
        callbackQueue: DispatchQueue = .main,
        noDataTimeout: DispatchTimeInterval = .milliseconds(1_500)
    ) {
        self.callbackQueue = callbackQueue
        self.noDataTimeout = noDataTimeout
    }

    var onState: ((AudioLevelMonitorState) -> Void)? {
        get {
            stateLock.lock()
            defer { stateLock.unlock() }
            return stateHandler
        }
        set {
            stateLock.lock()
            stateHandler = newValue
            stateLock.unlock()
        }
    }

    var onLevel: ((AudioLevelSample) -> Void)? {
        get {
            stateLock.lock()
            defer { stateLock.unlock() }
            return levelHandler
        }
        set {
            stateLock.lock()
            levelHandler = newValue
            stateLock.unlock()
        }
    }

    var state: AudioLevelMonitorState {
        stateLock.lock()
        defer { stateLock.unlock() }
        return stateValue
    }

    var deviceUID: String? {
        stateLock.lock()
        defer { stateLock.unlock() }
        return selectedUID
    }

    var deviceName: String? {
        stateLock.lock()
        defer { stateLock.unlock() }
        return selectedName
    }

    func start(configuration: Configuration) throws {
        stop()

        let uid = configuration.deviceUID
        stateLock.lock()
        selectedUID = uid
        selectedName = configuration.deviceName
        stateLock.unlock()

        guard !uid.isEmpty else {
            let error = AudioLevelMonitorError.invalidDeviceUID
            fail(error)
            throw error
        }

        let permissionStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        if let permissionError = AudioLevelMonitorPermissionPolicy.error(for: permissionStatus) {
            setState(AudioLevelMonitorPermissionPolicy.state(for: permissionStatus) ?? .failed(permissionError.localizedDescription))
            throw permissionError
        }

        setState(.starting)

        let records: [AudioInputDeviceCatalog.Record]
        do {
            records = try AudioInputDeviceCatalog.recordsOrThrow()
        } catch {
            let monitorError = AudioLevelMonitorError.deviceCatalogFailed(
                reason: error.localizedDescription
            )
            fail(monitorError)
            throw monitorError
        }
        guard let record = records.first(where: { $0.descriptor.uid == uid }) else {
            let error = AudioLevelMonitorError.inputDeviceUnavailable(uid: uid)
            fail(error)
            throw error
        }

        let name = record.descriptor.name
        stateLock.lock()
        selectedName = name
        generation &+= 1
        let token = generation
        hasReceivedSample = false
        isActive = true
        stateLock.unlock()

        let acquiredLease: AudioInputLease
        do {
            acquiredLease = try AudioInputLeaseCoordinator.acquire(deviceUID: uid)
        } catch let error as AudioLevelMonitorError {
            fail(error)
            throw error
        } catch {
            let wrapped = AudioLevelMonitorError.deviceBusy(uid: uid)
            fail(wrapped)
            throw wrapped
        }

        let candidateEngine = AVAudioEngine()
        let input = candidateEngine.inputNode
        do {
            try AudioInputDeviceRouting.configure(deviceID: record.deviceID, on: input)
        } catch let routingError {
            acquiredLease.release()
            let error: AudioLevelMonitorError
            if let routingError = routingError as? AudioInputRoutingError,
               case .audioUnitUnavailable = routingError {
                error = .audioUnitUnavailable(name: name, uid: uid)
            } else {
                error = .deviceConfigurationFailed(
                    name: name,
                    uid: uid,
                    reason: routingError.localizedDescription
                )
            }
            fail(error)
            throw error
        }

        let nativeFormat = input.outputFormat(forBus: 0)
        let sampleRate = nativeFormat.sampleRate
        let channelCount = Int(nativeFormat.channelCount)
        do {
            try AudioInputFormatPolicy.validate(
                sampleRate: sampleRate,
                channelCount: channelCount,
                commonFormat: nativeFormat.commonFormat,
                channelPolicy: .mixdown
            )
        } catch {
            acquiredLease.release()
            let error = AudioLevelMonitorError.invalidNativeFormat(
                name: name,
                uid: uid,
                sampleRate: sampleRate,
                channels: channelCount
            )
            fail(error)
            throw error
        }

        stateLock.lock()
        engine = candidateEngine
        lease = acquiredLease
        stateLock.unlock()

        input.installTap(onBus: 0, bufferSize: 2_048, format: nativeFormat) { [weak self] buffer, _ in
            guard let self, let sample = AudioLevelMath.measure(buffer: buffer) else { return }
            self.enqueue(sample: sample, generation: token)
        }

        stateLock.lock()
        tapInstalled = true
        stateLock.unlock()

        candidateEngine.prepare()
        do {
            try candidateEngine.start()
        } catch {
            teardownResources()
            let wrapped = AudioLevelMonitorError.engineStartFailed(
                name: name,
                uid: uid,
                reason: error.localizedDescription
            )
            fail(wrapped)
            throw wrapped
        }

        setState(.monitoring)
        scheduleNoDataCheck(generation: token)
    }

    func stop() {
        teardownResources()
        setState(.idle)
    }

    private func enqueue(sample: AudioLevelSample, generation token: UInt64) {
        // A full level queue drops only a redundant display sample; it never drops audio frames used by ASR.
        guard levelQueueSlots.wait(timeout: .now()) == .success else { return }
        levelQueue.async { [weak self] in
            defer { self?.levelQueueSlots.signal() }
            guard let self, self.isGenerationActive(token) else { return }

            self.stateLock.lock()
            self.hasReceivedSample = true
            self.stateLock.unlock()

            let smoothed = self.smoother.update(sample)
            if self.state == .noData {
                self.setState(.monitoring)
            }
            self.deliver(smoothed, generation: token)
        }
    }

    private func deliver(_ sample: AudioLevelSample, generation token: UInt64) {
        guard deliveryQueueSlots.wait(timeout: .now()) == .success else { return }
        callbackQueue.async { [weak self] in
            guard let self else { return }
            defer { self.deliveryQueueSlots.signal() }
            guard self.isGenerationActive(token) else { return }
            self.levelHandlerSnapshot()?(sample)
        }
    }

    private func scheduleNoDataCheck(generation token: UInt64) {
        let workItem = DispatchWorkItem { [weak self] in
            guard let self, self.isGenerationActive(token) else { return }
            self.stateLock.lock()
            let shouldPublish = AudioLevelMonitorNoDataPolicy.shouldPublishNoData(
                hasReceivedSample: self.hasReceivedSample
            )
            self.stateLock.unlock()
            if shouldPublish {
                self.setState(.noData)
            }
        }
        stateLock.lock()
        noDataWorkItem?.cancel()
        noDataWorkItem = workItem
        stateLock.unlock()
        controlQueue.asyncAfter(deadline: .now() + noDataTimeout, execute: workItem)
    }

    private func teardownResources() {
        stateLock.lock()
        isActive = false
        generation &+= 1
        noDataWorkItem?.cancel()
        noDataWorkItem = nil
        let activeEngine = engine
        let hadTap = tapInstalled
        let activeLease = lease
        engine = nil
        tapInstalled = false
        lease = nil
        stateLock.unlock()

        // Invalidate callbacks before touching the engine so a queued tap cannot publish during teardown.
        if hadTap {
            activeEngine?.inputNode.removeTap(onBus: 0)
        }
        activeEngine?.stop()
        activeLease?.release()

        levelQueue.async { [weak self] in
            self?.smoother.reset()
        }
    }

    private func fail(_ error: AudioLevelMonitorError) {
        teardownResources()
        setState(.failed(error.localizedDescription))
    }

    private func isGenerationActive(_ token: UInt64) -> Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return isActive && generation == token
    }

    private func setState(_ newState: AudioLevelMonitorState) {
        stateLock.lock()
        stateValue = newState
        let handler = stateHandler
        stateLock.unlock()
        guard let handler else { return }
        callbackQueue.async {
            handler(newState)
        }
    }

    private func levelHandlerSnapshot() -> ((AudioLevelSample) -> Void)? {
        stateLock.lock()
        defer { stateLock.unlock() }
        return levelHandler
    }
}
