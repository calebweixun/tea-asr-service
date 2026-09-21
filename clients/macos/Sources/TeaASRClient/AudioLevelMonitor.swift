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

/// Pure level math keeps the shared-source callback small and testable without
/// requiring a real microphone.
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
        for sample in samples { accumulator.append(sample) }
        return accumulator.result
    }

    static func measure(buffer: AVAudioPCMBuffer) -> AudioLevelSample? {
        measure(buffer: buffer, channelPolicy: .mixdown)
    }

    static func measure(
        buffer: AVAudioPCMBuffer,
        channelPolicy: AudioChannelPolicy
    ) -> AudioLevelSample? {
        let frameCount = Int(buffer.frameLength)
        let channelCount = Int(buffer.format.channelCount)
        guard frameCount > 0, channelCount > 0 else { return nil }

        let selectedChannel: Int?
        switch channelPolicy {
        case .mixdown:
            selectedChannel = nil
        case .channel(let index):
            guard (0..<channelCount).contains(index) else { return nil }
            selectedChannel = index
        }

        var accumulator = Accumulator()
        if let channels = buffer.floatChannelData {
            if buffer.format.isInterleaved {
                let samples = UnsafeBufferPointer(
                    start: channels[0],
                    count: frameCount * channelCount
                )
                if let selectedChannel {
                    for frame in 0..<frameCount {
                        accumulator.append(samples[frame * channelCount + selectedChannel])
                    }
                } else {
                    for sample in samples { accumulator.append(sample) }
                }
            } else if let selectedChannel {
                let samples = UnsafeBufferPointer(start: channels[selectedChannel], count: frameCount)
                for sample in samples { accumulator.append(sample) }
            } else {
                for channel in 0..<channelCount {
                    let samples = UnsafeBufferPointer(start: channels[channel], count: frameCount)
                    for sample in samples { accumulator.append(sample) }
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
                if let selectedChannel {
                    for frame in 0..<frameCount {
                        accumulator.append(Float(samples[frame * channelCount + selectedChannel]) / scale)
                    }
                } else {
                    for sample in samples { accumulator.append(Float(sample) / scale) }
                }
            } else if let selectedChannel {
                let samples = UnsafeBufferPointer(start: channels[selectedChannel], count: frameCount)
                for sample in samples { accumulator.append(Float(sample) / scale) }
            } else {
                for channel in 0..<channelCount {
                    let samples = UnsafeBufferPointer(start: channels[channel], count: frameCount)
                    for sample in samples { accumulator.append(Float(sample) / scale) }
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
                if let selectedChannel {
                    for frame in 0..<frameCount {
                        accumulator.append(Float(samples[frame * channelCount + selectedChannel]) / scale)
                    }
                } else {
                    for sample in samples { accumulator.append(Float(sample) / scale) }
                }
            } else if let selectedChannel {
                let samples = UnsafeBufferPointer(start: channels[selectedChannel], count: frameCount)
                for sample in samples { accumulator.append(Float(sample) / scale) }
            } else {
                for channel in 0..<channelCount {
                    let samples = UnsafeBufferPointer(start: channels[channel], count: frameCount)
                    for sample in samples { accumulator.append(Float(sample) / scale) }
                }
            }
            return accumulator.result
        }
        return nil
    }
}

enum AudioLevelScale {
    static let floorDB: Float = -60

    static func normalized(amplitude: Float) -> Float {
        guard amplitude.isFinite, amplitude > 0 else { return 0 }
        let bounded = min(1, amplitude)
        let decibels = 20 * log10(bounded)
        guard decibels > floorDB else { return 0 }
        return min(1, (decibels - floorDB) / -floorDB)
    }

    static func display(for sample: AudioLevelSample) -> AudioLevelSample {
        AudioLevelSample(
            rms: normalized(amplitude: sample.rms),
            peak: normalized(amplitude: sample.peak)
        )
    }
}

enum AudioLevelUpdatePolicy {
    static let minimumInterval: TimeInterval = 1.0 / 60.0
    static let minimumChange: Float = 0.002
    static let idleRefreshInterval: TimeInterval = 0.5

    static func shouldDeliver(
        pending: AudioLevelSample,
        lastDelivered: AudioLevelSample?,
        elapsed: TimeInterval,
        minimumInterval: TimeInterval = minimumInterval,
        minimumChange: Float = minimumChange,
        idleRefreshInterval: TimeInterval = idleRefreshInterval
    ) -> Bool {
        guard let lastDelivered else { return true }
        guard elapsed >= minimumInterval else { return false }
        let change = max(
            abs(pending.rms - lastDelivered.rms),
            abs(pending.peak - lastDelivered.peak)
        )
        if change >= minimumChange { return true }
        return elapsed >= idleRefreshInterval
    }

    static func effectiveRate(
        sourceInterval: TimeInterval,
        minimumInterval: TimeInterval = minimumInterval
    ) -> Double {
        guard sourceInterval > 0 else { return 0 }
        guard minimumInterval > 0 else { return 1 / sourceInterval }
        let stride = max(1, Int((minimumInterval / sourceInterval).rounded(.up)))
        return 1 / (Double(stride) * sourceInterval)
    }
}

struct AudioLevelSmoother {
    static let defaultAttackSeconds: TimeInterval = 0.012
    static let defaultReleaseSeconds: TimeInterval = 0.220
    static let defaultInterval: TimeInterval = 1.0 / 60.0

    let attackSeconds: TimeInterval
    let releaseSeconds: TimeInterval
    private(set) var current = AudioLevelSample.zero

    init(
        attackSeconds: TimeInterval = AudioLevelSmoother.defaultAttackSeconds,
        releaseSeconds: TimeInterval = AudioLevelSmoother.defaultReleaseSeconds
    ) {
        self.attackSeconds = max(0, attackSeconds)
        self.releaseSeconds = max(0, releaseSeconds)
    }

    static func coefficient(timeConstant: TimeInterval, interval: TimeInterval) -> Float {
        guard interval > 0 else { return 0 }
        guard timeConstant > 0 else { return 1 }
        return Float(1 - exp(-interval / timeConstant))
    }

    mutating func reset() { current = .zero }

    mutating func update(
        _ sample: AudioLevelSample,
        interval: TimeInterval = AudioLevelSmoother.defaultInterval
    ) -> AudioLevelSample {
        let bounded = min(max(interval.isFinite ? interval : 0, 0.001), 0.2)
        current = AudioLevelSample(
            rms: smooth(current.rms, target: sample.rms, interval: bounded),
            peak: smooth(current.peak, target: sample.peak, interval: bounded)
        )
        return current
    }

    private func smooth(_ current: Float, target: Float, interval: TimeInterval) -> Float {
        let boundedTarget = max(0, min(1, target.isFinite ? target : 0))
        let timeConstant = boundedTarget >= current ? attackSeconds : releaseSeconds
        let coefficient = Self.coefficient(timeConstant: timeConstant, interval: interval)
        return current + ((boundedTarget - current) * coefficient)
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
    case startCancelled
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
        case .startCancelled:
            return "輸入電平監看啟動已取消。"
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
        case .authorized: return nil
        case .notDetermined: return .permissionRequired
        case .denied, .restricted: return .permissionDenied
        @unknown default: return .permissionDenied
        }
    }

    static func state(for status: AVAuthorizationStatus) -> AudioLevelMonitorState? {
        switch status {
        case .authorized: return nil
        case .notDetermined: return .permissionRequired
        case .denied, .restricted: return .permissionDenied
        @unknown default: return .permissionDenied
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
        let channelPolicy: AudioChannelPolicy

        init(
            deviceUID: String,
            deviceName: String? = nil,
            channelPolicy: AudioChannelPolicy = .mixdown
        ) {
            self.deviceUID = deviceUID.trimmingCharacters(in: .whitespacesAndNewlines)
            self.deviceName = deviceName
            self.channelPolicy = channelPolicy
        }
    }

    private let callbackQueue: DispatchQueue
    private let controlQueue = DispatchQueue(label: "com.tea-asr.audio-level-monitor.control")
    private let startQueue = DispatchQueue(
        label: "com.tea-asr.audio-level-monitor.start",
        qos: .userInitiated
    )
    private let levelQueue = DispatchQueue(label: "com.tea-asr.audio-level-monitor.level")
    private let stateLock = NSLock()
    private let levelQueueSlots = DispatchSemaphore(value: 2)
    private let deliveryQueueSlots = DispatchSemaphore(value: 2)
    private let noDataTimeout: DispatchTimeInterval
    private let inputSource: AudioInputSource

    private var stateValue: AudioLevelMonitorState = .idle
    private var stateHandler: ((AudioLevelMonitorState) -> Void)?
    private var levelHandler: ((AudioLevelSample) -> Void)?
    private var selectedUID: String?
    private var selectedName: String?
    private var generation: UInt64 = 0
    private var hasReceivedSample = false
    private var noDataWorkItem: DispatchWorkItem?
    private var smoother = AudioLevelSmoother()
    private var lastDeliveredSample: AudioLevelSample?
    private var lastDeliveredAt: Date?
    private var subscription: AudioInputSource.Subscription?
    private var isActive = false
    private let startStateLock = NSLock()
    private var startGeneration: UInt64 = 0

    init(
        callbackQueue: DispatchQueue = .main,
        noDataTimeout: DispatchTimeInterval = .milliseconds(1_500),
        inputSource: AudioInputSource = .shared
    ) {
        self.callbackQueue = callbackQueue
        self.noDataTimeout = noDataTimeout
        self.inputSource = inputSource
    }

    var onState: ((AudioLevelMonitorState) -> Void)? {
        get { stateLock.withLock { stateHandler } }
        set { stateLock.withLock { stateHandler = newValue } }
    }

    var onLevel: ((AudioLevelSample) -> Void)? {
        get { stateLock.withLock { levelHandler } }
        set { stateLock.withLock { levelHandler = newValue } }
    }

    var state: AudioLevelMonitorState { stateLock.withLock { stateValue } }
    var deviceUID: String? { stateLock.withLock { selectedUID } }
    var deviceName: String? { stateLock.withLock { selectedName } }

    func start(configuration: Configuration) throws {
        let token = beginStartRequest()
        try startSynchronously(configuration: configuration, token: token)
    }

    func startAsync(
        configuration: Configuration,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        let token = beginStartRequest()
        startQueue.async { [weak self] in
            guard let self else { return }
            do {
                try self.startSynchronously(configuration: configuration, token: token)
                try self.ensureStartRequestIsCurrent(token)
                self.callbackQueue.async { completion(.success(())) }
            } catch {
                self.callbackQueue.async { completion(.failure(error)) }
            }
        }
    }

    private func startSynchronously(
        configuration: Configuration,
        token: UInt64
    ) throws {
        try ensureStartRequestIsCurrent(token)
        teardownResources()
        stateLock.withLock {
            selectedUID = configuration.deviceUID
            selectedName = configuration.deviceName
        }

        let permissionStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        if let permissionError = AudioLevelMonitorPermissionPolicy.error(for: permissionStatus) {
            setState(AudioLevelMonitorPermissionPolicy.state(for: permissionStatus) ?? .failed(permissionError.localizedDescription))
            throw permissionError
        }
        setState(.starting)
        try ensureStartRequestIsCurrent(token)

        let records: [AudioInputDeviceCatalog.Record]
        do {
            records = try AudioInputDeviceCatalog.recordsOrThrow()
        } catch {
            let monitorError = AudioLevelMonitorError.deviceCatalogFailed(reason: error.localizedDescription)
            fail(monitorError)
            throw monitorError
        }

        let record: AudioInputDeviceCatalog.Record
        if configuration.deviceUID.isEmpty {
            do {
                record = try AudioInputDeviceCatalog.defaultRecordOrThrow()
            } catch {
                let monitorError = AudioLevelMonitorError.deviceCatalogFailed(reason: error.localizedDescription)
                fail(monitorError)
                throw monitorError
            }
        } else if let selected = records.first(where: { $0.descriptor.uid == configuration.deviceUID }) {
            record = selected
        } else {
            let monitorError = AudioLevelMonitorError.inputDeviceUnavailable(uid: configuration.deviceUID)
            fail(monitorError)
            throw monitorError
        }

        stateLock.withLock {
            selectedUID = record.descriptor.uid
            selectedName = record.descriptor.name
            generation &+= 1
            hasReceivedSample = false
            isActive = true
        }
        let monitorToken = stateLock.withLock { generation }
        let sourceConfiguration = AudioInputSourceConfiguration(
            record: record,
            channelPolicy: configuration.channelPolicy
        )
        try ensureStartRequestIsCurrent(token)

        do {
            let newSubscription = try inputSource.subscribe(configuration: sourceConfiguration) {
                [weak self] buffer in
                self?.enqueue(
                    buffer: buffer,
                    duration: buffer.format.sampleRate > 0
                        ? Double(buffer.frameLength) / buffer.format.sampleRate
                        : AudioLevelSmoother.defaultInterval,
                    generation: monitorToken,
                    channelPolicy: configuration.channelPolicy
                )
            }
            try ensureStartRequestIsCurrent(token)
            stateLock.withLock { subscription = newSubscription }
        } catch let error as AudioInputSourceError {
            let monitorError = mapSourceError(error, record: record)
            fail(monitorError)
            throw monitorError
        } catch {
            let monitorError = AudioLevelMonitorError.deviceConfigurationFailed(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                reason: error.localizedDescription
            )
            fail(monitorError)
            throw monitorError
        }

        setState(.monitoring)
        scheduleNoDataCheck(generation: monitorToken)
    }

    func stop() {
        startStateLock.lock()
        startGeneration &+= 1
        startStateLock.unlock()
        teardownResources()
        setState(.idle)
    }

    private func beginStartRequest() -> UInt64 {
        startStateLock.lock()
        startGeneration &+= 1
        let token = startGeneration
        startStateLock.unlock()
        return token
    }

    private func ensureStartRequestIsCurrent(_ token: UInt64) throws {
        startStateLock.lock()
        let isCurrent = startGeneration == token
        startStateLock.unlock()
        guard isCurrent else { throw AudioLevelMonitorError.startCancelled }
    }

    private func enqueue(
        buffer: AVAudioPCMBuffer,
        duration: TimeInterval,
        generation token: UInt64,
        channelPolicy: AudioChannelPolicy
    ) {
        guard levelQueueSlots.wait(timeout: .now()) == .success else { return }
        guard let copied = buffer.copy() as? AVAudioPCMBuffer else {
            levelQueueSlots.signal()
            return
        }
        levelQueue.async { [weak self] in
            defer { self?.levelQueueSlots.signal() }
            guard let self, self.isGenerationActive(token),
                  let sample = AudioLevelMath.measure(buffer: copied, channelPolicy: channelPolicy)
            else { return }

            self.stateLock.withLock { self.hasReceivedSample = true }
            let smoothed = self.smoother.update(
                AudioLevelScale.display(for: sample),
                interval: duration
            )
            if self.state == .noData { self.setState(.monitoring) }
            let now = Date()
            let elapsed = self.lastDeliveredAt.map { now.timeIntervalSince($0) } ?? .infinity
            guard AudioLevelUpdatePolicy.shouldDeliver(
                pending: smoothed,
                lastDelivered: self.lastDeliveredSample,
                elapsed: elapsed
            ) else { return }
            self.lastDeliveredSample = smoothed
            self.lastDeliveredAt = now
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
            let shouldPublish = self.stateLock.withLock {
                AudioLevelMonitorNoDataPolicy.shouldPublishNoData(hasReceivedSample: self.hasReceivedSample)
            }
            if shouldPublish { self.setState(.noData) }
        }
        stateLock.withLock {
            noDataWorkItem?.cancel()
            noDataWorkItem = workItem
        }
        controlQueue.asyncAfter(deadline: .now() + noDataTimeout, execute: workItem)
    }

    private func teardownResources() {
        let activeSubscription: AudioInputSource.Subscription? = stateLock.withLock {
            isActive = false
            generation &+= 1
            noDataWorkItem?.cancel()
            noDataWorkItem = nil
            let active = subscription
            subscription = nil
            return active
        }
        activeSubscription?.cancel()
        levelQueue.async { [weak self] in
            self?.smoother.reset()
            self?.lastDeliveredSample = nil
            self?.lastDeliveredAt = nil
        }
    }

    private func mapSourceError(
        _ error: AudioInputSourceError,
        record: AudioInputDeviceCatalog.Record
    ) -> AudioLevelMonitorError {
        switch error {
        case .invalidFormat:
            return .invalidNativeFormat(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                sampleRate: 0,
                channels: record.descriptor.inputChannels
            )
        case .engineStartFailed(_, _, let reason):
            return .engineStartFailed(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                reason: reason
            )
        default:
            return .deviceConfigurationFailed(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                reason: error.localizedDescription
            )
        }
    }

    private func fail(_ error: AudioLevelMonitorError) {
        teardownResources()
        setState(.failed(error.localizedDescription))
    }

    private func isGenerationActive(_ token: UInt64) -> Bool {
        stateLock.withLock { isActive && generation == token }
    }

    private func setState(_ newState: AudioLevelMonitorState) {
        let handler = stateLock.withLock { () -> ((AudioLevelMonitorState) -> Void)? in
            stateValue = newState
            return stateHandler
        }
        if let handler {
            callbackQueue.async { handler(newState) }
        }
    }

    private func levelHandlerSnapshot() -> ((AudioLevelSample) -> Void)? {
        stateLock.withLock { levelHandler }
    }
}

private extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}
