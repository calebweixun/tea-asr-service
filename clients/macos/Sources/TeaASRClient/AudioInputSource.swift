import AVFoundation
import CoreAudio
import Foundation

/// The configuration that determines which native input stream is open.
/// Channel selection is included deliberately: changing it while a consumer is
/// active must be an explicit reconfiguration, never an accidental fallback.
struct AudioInputSourceConfiguration: Equatable {
    let deviceUID: String
    let deviceName: String
    let deviceID: AudioDeviceID
    let inputChannels: Int
    let channelPolicy: AudioChannelPolicy

    init(record: AudioInputDeviceCatalog.Record, channelPolicy: AudioChannelPolicy) {
        self.deviceUID = record.descriptor.uid
        self.deviceName = record.descriptor.name
        self.deviceID = record.deviceID
        self.inputChannels = record.descriptor.inputChannels
        self.channelPolicy = channelPolicy
    }

    /// Names are presentation data.  The audio resource identity is the
    /// stable UID/CoreAudio ID plus the channel policy that determines how the
    /// shared native stream is consumed.
    func matchesInput(_ other: Self) -> Bool {
        deviceUID == other.deviceUID
            && deviceID == other.deviceID
            && inputChannels == other.inputChannels
            && channelPolicy == other.channelPolicy
    }
}

enum AudioInputSourceError: LocalizedError {
    case invalidDeviceUID
    case deviceUnavailable(name: String, uid: String, reason: String)
    case configurationConflict(
        activeName: String,
        activeUID: String,
        requestedName: String,
        requestedUID: String
    )
    case routingFailed(name: String, uid: String, reason: String)
    case invalidFormat(name: String, uid: String, reason: String)
    case engineStartFailed(name: String, uid: String, reason: String)

    var errorDescription: String? {
        switch self {
        case .invalidDeviceUID:
            return "未指定有效的輸入裝置 UID。"
        case .deviceUnavailable(let name, let uid, let reason):
            return "無法使用輸入裝置「\(name)」（UID: \(uid)）：\(reason)"
        case .configurationConflict(let activeName, let activeUID, let requestedName, let requestedUID):
            return "輸入裝置目前已由「\(activeName)」（UID: \(activeUID)）開啟，不能靜默切換到「\(requestedName)」（UID: \(requestedUID)）。"
        case .routingFailed(let name, let uid, let reason):
            return "無法設定輸入裝置「\(name)」（UID: \(uid)）：\(reason)"
        case .invalidFormat(let name, let uid, let reason):
            return "輸入裝置「\(name)」（UID: \(uid)）回報無效格式：\(reason)"
        case .engineStartFailed(let name, let uid, let reason):
            return "無法啟動輸入裝置「\(name)」（UID: \(uid)）：\(reason)"
        }
    }
}

/// A small, hardware-free lifecycle model used by tests and by design review.
/// The production source below follows the same rules: the first consumer
/// opens the device, consumers with a different configuration are rejected,
/// and the last consumer closes it. Handlers are called synchronously, so a
/// production handler must only enqueue bounded work and return immediately.
final class AudioInputSourceLifecycle {
    struct Configuration: Equatable {
        let deviceUID: String
        let channelPolicy: AudioChannelPolicy
    }

    enum Error: Swift.Error, Equatable {
        case configurationConflict(active: Configuration, requested: Configuration)
        case unknownConsumer
    }

    private struct Consumer {
        let configuration: Configuration
        let handler: (Data) -> Void
    }

    private let open: (Configuration) throws -> Void
    private let close: () -> Void
    private let reconfigure: (Configuration) throws -> Void
    private var consumers: [UUID: Consumer] = [:]
    private(set) var activeConfiguration: Configuration?

    init(
        open: @escaping (Configuration) throws -> Void,
        close: @escaping () -> Void,
        reconfigure: @escaping (Configuration) throws -> Void
    ) {
        self.open = open
        self.close = close
        self.reconfigure = reconfigure
    }

    var consumerCount: Int { consumers.count }
    var isOpen: Bool { activeConfiguration != nil }

    @discardableResult
    func attach(
        configuration: Configuration,
        handler: @escaping (Data) -> Void
    ) throws -> UUID {
        if let activeConfiguration {
            guard activeConfiguration == configuration else {
                throw Error.configurationConflict(
                    active: activeConfiguration,
                    requested: configuration
                )
            }
        } else {
            try open(configuration)
            activeConfiguration = configuration
        }

        let id = UUID()
        consumers[id] = Consumer(configuration: configuration, handler: handler)
        return id
    }

    func update(_ id: UUID, configuration: Configuration) throws {
        guard consumers[id] != nil else { throw Error.unknownConsumer }
        guard consumers.count == 1 else {
            throw Error.configurationConflict(
                active: activeConfiguration ?? configuration,
                requested: configuration
            )
        }
        guard activeConfiguration != configuration else { return }

        let oldConfiguration = activeConfiguration
        close()
        activeConfiguration = nil
        do {
            try reconfigure(configuration)
        } catch {
            if let oldConfiguration {
                try? open(oldConfiguration)
                activeConfiguration = oldConfiguration
            }
            throw error
        }
        activeConfiguration = configuration
        let handler = consumers[id]?.handler
        consumers[id] = handler.map {
            Consumer(configuration: configuration, handler: $0)
        }
    }

    func detach(_ id: UUID) throws {
        guard consumers.removeValue(forKey: id) != nil else {
            throw Error.unknownConsumer
        }
        guard consumers.isEmpty else { return }
        close()
        activeConfiguration = nil
    }

    func publish(_ data: Data) {
        for consumer in consumers.values {
            consumer.handler(data)
        }
    }
}

/// One AVAudioEngine/input node for the whole process. AudioLevelMonitor and
/// AudioCapture subscribe to this source instead of opening independent input
/// units. Control work is serialized on a private queue; the tap only takes a
/// snapshot and calls nonblocking consumer enqueue functions.
final class AudioInputSource {
    static let shared = AudioInputSource()

    final class Subscription {
        let id: UUID
        private(set) var configuration: AudioInputSourceConfiguration
        private(set) var inputFormat: AVAudioFormat
        private weak var source: AudioInputSource?
        private let lock = NSLock()
        private var cancelled = false

        fileprivate init(
            id: UUID,
            configuration: AudioInputSourceConfiguration,
            inputFormat: AVAudioFormat,
            source: AudioInputSource
        ) {
            self.id = id
            self.configuration = configuration
            self.inputFormat = inputFormat
            self.source = source
        }

        func cancel() {
            lock.lock()
            guard !cancelled else {
                lock.unlock()
                return
            }
            cancelled = true
            lock.unlock()
            source?.removeConsumer(id, synchronously: false)
        }

        fileprivate func cancelSynchronously() {
            lock.lock()
            guard !cancelled else {
                lock.unlock()
                return
            }
            cancelled = true
            lock.unlock()
            source?.removeConsumer(id, synchronously: true)
        }

        fileprivate func update(
            configuration: AudioInputSourceConfiguration,
            inputFormat: AVAudioFormat
        ) {
            lock.lock()
            self.configuration = configuration
            self.inputFormat = inputFormat
            lock.unlock()
        }

        deinit { cancel() }
    }

    private struct Consumer {
        let configuration: AudioInputSourceConfiguration
        let handler: (AVAudioPCMBuffer) -> Void
    }

    private let controlQueue = DispatchQueue(
        label: "com.tea-asr.audio-input-source.control",
        qos: .userInitiated
    )
    private let controlQueueKey = DispatchSpecificKey<Void>()
    private let stateLock = NSLock()
    private var engine: AVAudioEngine?
    private var inputNode: AVAudioInputNode?
    private var inputFormat: AVAudioFormat?
    private var configuration: AudioInputSourceConfiguration?
    private var consumers: [UUID: Consumer] = [:]

    init() {
        controlQueue.setSpecific(key: controlQueueKey, value: ())
    }

    var consumerCount: Int {
        stateLock.lock()
        defer { stateLock.unlock() }
        return consumers.count
    }

    var isOpen: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return engine != nil
    }

    var currentEngine: AVAudioEngine? {
        stateLock.lock()
        defer { stateLock.unlock() }
        return engine
    }

    var currentInputNode: AVAudioInputNode? {
        stateLock.lock()
        defer { stateLock.unlock() }
        return inputNode
    }

    var currentConfiguration: AudioInputSourceConfiguration? {
        stateLock.lock()
        defer { stateLock.unlock() }
        return configuration
    }

    func subscribe(
        configuration requested: AudioInputSourceConfiguration,
        prepare: ((AVAudioFormat) throws -> Void)? = nil,
        handler: @escaping (AVAudioPCMBuffer) -> Void
    ) throws -> Subscription {
        try performOnControlQueue {
            guard !requested.deviceUID.isEmpty else {
                throw AudioInputSourceError.invalidDeviceUID
            }

            let id = UUID()
            let consumer = Consumer(configuration: requested, handler: handler)
            if let active = self.configuration {
                guard active.matchesInput(requested) else {
                    throw AudioInputSourceError.configurationConflict(
                        activeName: active.deviceName,
                        activeUID: active.deviceUID,
                        requestedName: requested.deviceName,
                        requestedUID: requested.deviceUID
                    )
                }
                stateLock.withLock { consumers[id] = consumer }
                if let nativeFormat = self.inputFormat {
                    do {
                        try prepare?(nativeFormat)
                    } catch {
                        _ = stateLock.withLock { consumers.removeValue(forKey: id) }
                        throw error
                    }
                }
            } else {
                // Install the consumer before starting the engine.  A tap can
                // deliver its first buffer immediately after start(); adding
                // it afterwards would make that first buffer disappear from
                // the recording path.
                stateLock.withLock { consumers[id] = consumer }
                do {
                    try self.openEngine(for: requested, prepare: prepare)
                } catch {
                    _ = stateLock.withLock { consumers.removeValue(forKey: id) }
                    throw error
                }
            }

            return Subscription(
                id: id,
                configuration: requested,
                inputFormat: self.inputFormat ?? requestedFallbackFormat(),
                source: self
            )
        }
    }

    func reconfigure(
        _ subscription: Subscription,
        to requested: AudioInputSourceConfiguration
    ) throws {
        try performOnControlQueue {
            guard consumers[subscription.id] != nil else { return }
            guard consumers.count == 1 else {
                throw AudioInputSourceError.configurationConflict(
                    activeName: self.configuration?.deviceName ?? "",
                    activeUID: self.configuration?.deviceUID ?? "",
                    requestedName: requested.deviceName,
                    requestedUID: requested.deviceUID
                )
            }
            guard let oldConfiguration = self.configuration,
                  !oldConfiguration.matchesInput(requested) else { return }
            let handler = stateLock.withLock { consumers[subscription.id]?.handler }
            self.closeEngine()
            do {
                try self.openEngine(for: requested)
            } catch {
                // Reopen the old stream when possible so a failed switch does
                // not silently leave the existing consumer detached.
                try? self.openEngine(for: oldConfiguration)
                throw error
            }
            stateLock.withLock {
                consumers[subscription.id] = Consumer(
                    configuration: requested,
                    handler: handler ?? { _ in }
                )
            }
            subscription.update(
                configuration: requested,
                inputFormat: self.inputFormat ?? requestedFallbackFormat()
            )
        }
    }

    fileprivate func removeConsumer(_ id: UUID, synchronously: Bool) {
        let remove = {
            let shouldClose = self.stateLock.withLock {
                self.consumers.removeValue(forKey: id) != nil && self.consumers.isEmpty
            }
            if shouldClose { self.closeEngine() }
        }
        if DispatchQueue.getSpecific(key: controlQueueKey) != nil {
            remove()
        } else if synchronously {
            controlQueue.sync(execute: remove)
        } else {
            controlQueue.async(execute: remove)
        }
    }

    private func performOnControlQueue<T>(_ work: () throws -> T) rethrows -> T {
        if DispatchQueue.getSpecific(key: controlQueueKey) != nil {
            return try work()
        }
        return try controlQueue.sync(execute: work)
    }

    private func openEngine(
        for requested: AudioInputSourceConfiguration,
        prepare: ((AVAudioFormat) throws -> Void)? = nil
    ) throws {
        guard requested.inputChannels > 0 else {
            throw AudioInputSourceError.invalidFormat(
                name: requested.deviceName,
                uid: requested.deviceUID,
                reason: "CoreAudio 裝置沒有可用的輸入 stream"
            )
        }
        switch Self.queryDeviceLiveness(requested.deviceID) {
        case .alive:
            break
        case .dead:
            throw AudioInputSourceError.deviceUnavailable(
                name: requested.deviceName,
                uid: requested.deviceUID,
                reason: "CoreAudio 回報裝置已失效"
            )
        case .queryFailed(let status):
            throw AudioInputSourceError.deviceUnavailable(
                name: requested.deviceName,
                uid: requested.deviceUID,
                reason: "無法確認裝置存活狀態（CoreAudio OSStatus: \(status)）"
            )
        }

        let candidateEngine = AVAudioEngine()
        let candidateInput = candidateEngine.inputNode
        do {
            try AudioInputDeviceRouting.configure(
                deviceID: requested.deviceID,
                on: candidateInput
            )
        } catch {
            throw AudioInputSourceError.routingFailed(
                name: requested.deviceName,
                uid: requested.deviceUID,
                reason: error.localizedDescription
            )
        }

        let nativeFormat = candidateInput.outputFormat(forBus: 0)
        do {
            try AudioInputFormatPolicy.validate(
                sampleRate: nativeFormat.sampleRate,
                channelCount: Int(nativeFormat.channelCount),
                commonFormat: nativeFormat.commonFormat,
                channelPolicy: requested.channelPolicy
            )
        } catch {
            throw AudioInputSourceError.invalidFormat(
                name: requested.deviceName,
                uid: requested.deviceUID,
                reason: error.localizedDescription
            )
        }

        try prepare?(nativeFormat)

        candidateInput.installTap(onBus: 0, bufferSize: 512, format: nativeFormat) {
            [weak self] buffer, _ in
            self?.publish(buffer)
        }
        candidateEngine.prepare()
        do {
            try candidateEngine.start()
        } catch {
            candidateInput.removeTap(onBus: 0)
            candidateEngine.stop()
            throw AudioInputSourceError.engineStartFailed(
                name: requested.deviceName,
                uid: requested.deviceUID,
                reason: error.localizedDescription
            )
        }

        stateLock.lock()
        engine = candidateEngine
        inputNode = candidateInput
        inputFormat = nativeFormat
        configuration = requested
        stateLock.unlock()
    }

    private func closeEngine() {
        stateLock.lock()
        let activeEngine = engine
        let activeInput = inputNode
        engine = nil
        inputNode = nil
        inputFormat = nil
        configuration = nil
        stateLock.unlock()

        activeInput?.removeTap(onBus: 0)
        activeEngine?.stop()
    }

    private func publish(_ buffer: AVAudioPCMBuffer) {
        stateLock.lock()
        let handlers = consumers.values.map(\.handler)
        stateLock.unlock()
        for handler in handlers {
            handler(buffer)
        }
    }

    private static func queryDeviceLiveness(_ deviceID: AudioDeviceID) -> AudioDeviceLiveness {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsAlive,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var alive: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &size,
            &alive
        )
        guard status == noErr else { return .queryFailed(status) }
        return alive == 0 ? .dead : .alive
    }
}

private extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}

private func requestedFallbackFormat() -> AVAudioFormat {
    // This is only reachable after a successful open; the fallback keeps the
    // subscription type total if a future backend supplies the format lazily.
    AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: 16_000,
        channels: 1,
        interleaved: false
    )!
}
