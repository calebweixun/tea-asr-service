import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

struct AudioDiagnostics: Equatable {
    let inputDeviceName: String?
    let isRunning: Bool
    let hasFrames: Bool
    let rms: Double?
    let framesProduced: UInt64
    let framesDropped: UInt64
    let lastError: String?
}

struct AudioCaptureFailureGate {
    private(set) var didReportFailure = false

    mutating func beginFailure() -> Bool {
        guard !didReportFailure else { return false }
        didReportFailure = true
        return true
    }

    mutating func reset() {
        didReportFailure = false
    }
}

/// Shared teardown arithmetic kept pure so a failure path cannot accidentally
/// leave partial PCM in the next session.  The real capture teardown uses this
/// helper while tests can exercise it without starting AVAudioEngine.
enum AudioCaptureTeardownPolicy {
    static func clearPending(_ pending: inout Data, frameBytes: Int) -> UInt64 {
        guard frameBytes > 0 else {
            pending.removeAll()
            return 0
        }
        let dropped = UInt64(pending.count / frameBytes)
        pending.removeAll()
        return dropped
    }
}

/// Microphone capture resampled to the 16 kHz mono PCM16 the service accepts.
///
/// AVAudioConverter is used rather than a hand-rolled resampler: docs/03 asks
/// clients not to use naive linear downsampling as a quality baseline.
final class AudioCapture {
    private enum RuntimeAudioChangeSource {
        case deviceList
        case defaultDevice
        case deviceAlive
    }

    enum CaptureError: LocalizedError {
        case converterUnavailable(name: String, uid: String, sourceSampleRate: Double)
        case permissionDenied
        case noDefaultInputDevice
        case inputDeviceUnavailable(uid: String)
        case deviceCatalogFailed(uid: String?, reason: String)
        case deviceConfigurationFailed(name: String, uid: String, reason: String)
        case formatUnavailable(name: String, uid: String, sampleRate: Double, channels: Int, reason: String)
        case channelUnavailable(name: String, uid: String, policy: AudioChannelPolicy, available: Int)
        case engineStartFailed(name: String, uid: String, reason: String)
        case processingQueueOverflow(name: String, uid: String)
        case deviceMonitoringUnavailable(reason: String)

        var errorDescription: String? {
            switch self {
            case .converterUnavailable(let name, let uid, let sampleRate):
                return "無法使用輸入裝置「\(name)」（UID: \(uid)）建立音訊轉換器；原生取樣率為 \(sampleRate) Hz。"
            case .permissionDenied:
                return "沒有麥克風權限。請到「系統設定 → 隱私權與安全性 → 麥克風」允許 TEA ASR。"
            case .noDefaultInputDevice:
                return "找不到系統預設輸入裝置。請在「系統設定 → 聲音 → 輸入」選擇麥克風，或在 TEA ASR 設定指定裝置。"
            case .inputDeviceUnavailable(let uid):
                return "找不到已選取的輸入裝置（UID: \(uid)）。請在設定改選「系統預設」或重新插入該裝置。"
            case .deviceCatalogFailed(let uid, let reason):
                if let uid, !uid.isEmpty {
                    return "無法查詢指定輸入裝置（UID: \(uid)）：\(reason)"
                }
                return "無法查詢輸入裝置：\(reason)"
            case .deviceConfigurationFailed(let name, let uid, let reason):
                return "無法使用輸入裝置「\(name)」（UID: \(uid)）：\(reason) 請改選其他裝置後再試。"
            case .formatUnavailable(let name, let uid, let sampleRate, let channels, let reason):
                return "輸入裝置「\(name)」（UID: \(uid)）回報無法使用的格式（\(sampleRate) Hz、\(channels) 聲道）：\(reason)"
            case .channelUnavailable(let name, let uid, let policy, let available):
                return "輸入裝置「\(name)」（UID: \(uid)）無法使用「\(policy.title)」；目前只有 \(available) 個聲道。請改用自動混音或重新選擇聲道。"
            case .engineStartFailed(let name, let uid, let reason):
                return "輸入裝置「\(name)」（UID: \(uid)）啟動音訊引擎失敗：\(reason)"
            case .processingQueueOverflow(let name, let uid):
                return "輸入裝置「\(name)」（UID: \(uid)）的音訊處理佇列已滿；為避免靜默丟失音訊，錄音已停止。"
            case .deviceMonitoringUnavailable(let reason):
                return "無法監測輸入裝置變更：\(reason) 請重新啟動 TEA ASR 後再試。"
            }
        }
    }

    private let engine = AVAudioEngine()
    private var activeInputNode: AVAudioInputNode?
    private var converter: AVAudioConverter?
    private var tapInstalled = false
    private var inputLease: AudioInputLease?
    private var pending = Data()
    private let lock = NSLock()
    private let frameBytes = Wire.frameSamples * 2
    private let processingQueue = DispatchQueue(
        label: "com.tea-asr.audio-capture.processing",
        qos: .userInitiated
    )
    private let processingQueueKey = DispatchSpecificKey<Void>()
    private let processingQueueSlots = DispatchSemaphore(value: 4)
    private var processingGeneration: UInt64 = 0
    private var processingActive = false
    private var framesProduced: UInt64 = 0
    private var framesDropped: UInt64 = 0
    private var lastError: String?
    private var lastRMS: Double?
    private var hasFrames = false
    private var inputDeviceName: String?
    private var activeConfiguration: AudioInputConfiguration?
    private var activeDevice: AudioInputDeviceCatalog.Record?
    private var failureGate = AudioCaptureFailureGate()
    private var configurationChangeObserver: NSObjectProtocol?
    private var deviceListListener: AudioObjectPropertyListenerBlock?
    private var deviceListAddress: AudioObjectPropertyAddress?
    private var defaultDeviceListener: AudioObjectPropertyListenerBlock?
    private var defaultDeviceAddress: AudioObjectPropertyAddress?
    private var deviceAliveListener: AudioObjectPropertyListenerBlock?
    private var deviceAliveAddress: AudioObjectPropertyAddress?
    private var monitoredDeviceID: AudioDeviceID?

    /// Called from the capture processing queue with exactly one frame of PCM.
    var onFrame: ((Data) -> Void)?
    /// Called from the capture processing queue. Consumers must dispatch UI work to main.
    var onDiagnostics: ((AudioDiagnostics) -> Void)?
    /// Called once when the selected input disappears or the engine becomes
    /// unusable while recording. Consumers should stop the ASR session and
    /// surface the message in the management UI.
    var onError: ((String) -> Void)?

    private(set) var isRunning = false

    init() {
        processingQueue.setSpecific(key: processingQueueKey, value: ())
    }

    static func requestPermission(_ completion: @escaping (Bool) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            completion(true)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async { completion(granted) }
            }
        default:
            completion(false)
        }
    }

    func start(configuration: AudioInputConfiguration = .default) throws {
        guard !isRunning else { return }
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            throw CaptureError.permissionDenied
        }
        // A prior engine/observer failure may have already flipped
        // `isRunning` to false.  Clean that state before resolving the next
        // device so no stale tap, converter, listener, or PCM can leak into a
        // new session.
        teardownCapture()
        lock.lock()
        failureGate.reset()
        lastError = nil
        lock.unlock()

        let records: [AudioInputDeviceCatalog.Record]
        do {
            records = try AudioInputDeviceCatalog.recordsOrThrow()
        } catch {
            throw CaptureError.deviceCatalogFailed(
                uid: configuration.deviceUID,
                reason: error.localizedDescription
            )
        }
        let available = records.map(\.descriptor)
        let resolution = AudioInputDeviceSelection.resolve(
            storedUID: configuration.deviceUID,
            available: available
        )
        let record: AudioInputDeviceCatalog.Record
        switch resolution {
        case .systemDefault:
            do {
                record = try AudioInputDeviceCatalog.defaultRecordOrThrow()
            } catch let error as AudioInputDeviceCatalog.Error {
                if case .defaultDeviceNotInCatalog = error {
                    throw CaptureError.noDefaultInputDevice
                }
                throw CaptureError.deviceCatalogFailed(
                    uid: configuration.deviceUID,
                    reason: error.localizedDescription
                )
            } catch {
                throw CaptureError.deviceCatalogFailed(
                    uid: configuration.deviceUID,
                    reason: error.localizedDescription
                )
            }
        case .selected(let device):
            guard let selectedRecord = records.first(where: { $0.descriptor.uid == device.uid }) else {
                throw CaptureError.inputDeviceUnavailable(uid: device.uid)
            }
            record = selectedRecord
        case .missingStoredDevice(let uid):
            throw CaptureError.inputDeviceUnavailable(uid: uid)
        }

        // This catalog-level check keeps zero-channel/output-only objects away
        // from AVFAudio's inputNode path. The native format is checked again
        // below because a device can change its format after enumeration.
        guard record.descriptor.inputChannels > 0 else {
            throw CaptureError.formatUnavailable(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                sampleRate: 0,
                channels: record.descriptor.inputChannels,
                reason: "CoreAudio 裝置沒有可用的輸入 stream"
            )
        }
        if case .channel(let index) = configuration.channelPolicy,
           !(0..<record.descriptor.inputChannels).contains(index) {
            throw CaptureError.channelUnavailable(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                policy: configuration.channelPolicy,
                available: record.descriptor.inputChannels
            )
        }

        switch Self.queryDeviceLiveness(record.deviceID) {
        case .alive:
            break
        case .dead:
            throw CaptureError.deviceConfigurationFailed(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                reason: "CoreAudio 回報裝置已失效"
            )
        case .queryFailed(let status):
            throw CaptureError.deviceConfigurationFailed(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                reason: "無法確認裝置存活狀態（CoreAudio OSStatus: \(status)）"
            )
        }

        // Do not touch AVAudioEngine.inputNode until the strict CoreAudio
        // preflight above has found a real device.  On a host with no audio
        // objects AVFAudio raises an Objective-C exception here instead of a
        // Swift error, so this ordering is part of the failure policy.
        let input = engine.inputNode

        let acquiredLease: AudioInputLease
        do {
            acquiredLease = try AudioInputLeaseCoordinator.acquire(
                deviceUID: record.descriptor.uid
            )
        } catch {
            throw CaptureError.deviceConfigurationFailed(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                reason: error.localizedDescription
            )
        }
        inputLease = acquiredLease
        var leaseCommitted = false
        defer {
            if !leaseCommitted {
                teardownCapture()
                acquiredLease.release()
            }
        }
        let processingToken = beginProcessingSession()

        // Apply this even for System Default. The same AVAudioEngine is reused
        // after stop(), and an earlier explicit device selection must not leak
        // into a later default-device session.
        do {
            try AudioInputDeviceRouting.configure(deviceID: record.deviceID, on: input)
        } catch {
            throw CaptureError.deviceConfigurationFailed(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                reason: error.localizedDescription
            )
        }

        let inputFormat = input.outputFormat(forBus: 0)
        let inputChannelCount = Int(inputFormat.channelCount)
        do {
            try AudioInputFormatPolicy.validate(
                sampleRate: inputFormat.sampleRate,
                channelCount: inputChannelCount,
                commonFormat: inputFormat.commonFormat,
                channelPolicy: configuration.channelPolicy
            )
        } catch let error as AudioInputFormatError {
            if case .channelOutOfBounds = error {
                throw CaptureError.channelUnavailable(
                    name: record.descriptor.name,
                    uid: record.descriptor.uid,
                    policy: configuration.channelPolicy,
                    available: inputChannelCount
                )
            }
            throw CaptureError.formatUnavailable(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                sampleRate: inputFormat.sampleRate,
                channels: inputChannelCount,
                reason: error.localizedDescription
            )
        } catch {
            throw CaptureError.formatUnavailable(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                sampleRate: inputFormat.sampleRate,
                channels: inputChannelCount,
                reason: error.localizedDescription
            )
        }
        inputDeviceName = record.descriptor.name

        guard let target = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: Double(Wire.sampleRate),
            channels: 1,
            interleaved: true
        ) else {
            throw CaptureError.formatUnavailable(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                sampleRate: inputFormat.sampleRate,
                channels: inputChannelCount,
                reason: "無法建立服務需要的 16 kHz mono PCM16 目標格式"
            )
        }
        guard let monoFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: inputFormat.sampleRate,
            channels: 1,
            interleaved: false
        ) else {
            throw CaptureError.formatUnavailable(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                sampleRate: inputFormat.sampleRate,
                channels: inputChannelCount,
                reason: "無法建立裝置原生取樣率的 mono Float32 格式"
            )
        }
        guard let converter = AVAudioConverter(from: monoFormat, to: target) else {
            throw CaptureError.converterUnavailable(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                sourceSampleRate: inputFormat.sampleRate
            )
        }
        converter.sampleRateConverterQuality = AVAudioQuality.high.rawValue
        self.converter = converter

        input.installTap(onBus: 0, bufferSize: 4_096, format: inputFormat) { [weak self] buffer, _ in
            self?.enqueue(
                buffer: buffer,
                converter: converter,
                target: target,
                channelPolicy: configuration.channelPolicy,
                generation: processingToken,
                deviceName: record.descriptor.name,
                deviceUID: record.descriptor.uid
            )
        }
        activeInputNode = input
        tapInstalled = true
        engine.prepare()
        do {
            try engine.start()
        } catch {
            // installTap mutates the input node even when engine.start() fails
            // (for example when an audio device disappears). Remove it before
            // returning so a permission/device retry does not hit "tap already
            // installed" on the next start attempt.
            teardownCapture()
            publishDiagnostics()
            let nsError = error as NSError
            throw CaptureError.engineStartFailed(
                name: record.descriptor.name,
                uid: record.descriptor.uid,
                reason: "\(nsError.localizedDescription)（domain: \(nsError.domain), code: \(nsError.code)）"
            )
        }
        isRunning = true
        activeConfiguration = configuration
        activeDevice = record
        monitoredDeviceID = record.deviceID
        leaseCommitted = true
        do {
            try installRuntimeObservers(for: record)
        } catch {
            teardownCapture()
            publishDiagnostics()
            throw error
        }
        publishDiagnostics()
    }

    func stop() {
        teardownCapture()
        publishDiagnostics()
    }

    private func beginProcessingSession() -> UInt64 {
        processingQueue.sync {
            processingGeneration &+= 1
            processingActive = true
            return processingGeneration
        }
    }

    private func invalidateProcessingSession() {
        let invalidate = {
            self.processingActive = false
            self.processingGeneration &+= 1
        }
        if DispatchQueue.getSpecific(key: processingQueueKey) != nil {
            invalidate()
        } else {
            processingQueue.sync(execute: invalidate)
        }
    }

    private func enqueue(
        buffer: AVAudioPCMBuffer,
        converter: AVAudioConverter,
        target: AVAudioFormat,
        channelPolicy: AudioChannelPolicy,
        generation: UInt64,
        deviceName: String,
        deviceUID: String
    ) {
        // A full queue ends the session loudly instead of dropping a buffer and
        // quietly compressing the sample timeline sent to the server.
        guard processingQueueSlots.wait(timeout: .now()) == .success else {
            DispatchQueue.main.async { [weak self] in
                self?.failRuntime(
                    .processingQueueOverflow(name: deviceName, uid: deviceUID)
                )
            }
            return
        }

        guard let copiedBuffer = buffer.copy() as? AVAudioPCMBuffer else {
            processingQueueSlots.signal()
            DispatchQueue.main.async { [weak self] in
                self?.failRuntime(
                    .deviceConfigurationFailed(
                        name: deviceName,
                        uid: deviceUID,
                        reason: "無法保留音訊 buffer；為避免靜默丟失音訊，錄音已停止"
                    )
                )
            }
            return
        }

        processingQueue.async { [weak self] in
            defer { self?.processingQueueSlots.signal() }
            guard let self,
                  self.processingActive,
                  self.processingGeneration == generation
            else { return }
            self.consume(
                copiedBuffer,
                converter: converter,
                target: target,
                channelPolicy: channelPolicy
            )
        }
    }

    /// Stop every capture resource even when a previous start/failure already
    /// flipped `isRunning` to false.  In particular, pending PCM and the
    /// converter must not survive into the next session.
    private func teardownCapture() {
        // Flip the running flag before removing listeners/stopping the engine,
        // so a callback queued during teardown cannot start a second failure
        // path or observe a half-torn-down capture.
        isRunning = false
        removeRuntimeObservers()
        invalidateProcessingSession()

        if tapInstalled {
            activeInputNode?.removeTap(onBus: 0)
            tapInstalled = false
        }
        activeInputNode = nil
        engine.stop()
        converter = nil
        let lease = inputLease
        inputLease = nil
        lease?.release()

        lock.lock()
        framesDropped += AudioCaptureTeardownPolicy.clearPending(
            &pending,
            frameBytes: frameBytes
        )
        lock.unlock()

        activeConfiguration = nil
        activeDevice = nil
        monitoredDeviceID = nil
    }

    private func consume(
        _ buffer: AVAudioPCMBuffer,
        converter: AVAudioConverter,
        target: AVAudioFormat,
        channelPolicy: AudioChannelPolicy
    ) {
        let mono: AVAudioPCMBuffer
        do {
            mono = try AudioChannelMixer.makeMonoBuffer(from: buffer, policy: channelPolicy)
        } catch {
            recordError(error.localizedDescription)
            return
        }

        let ratio = target.sampleRate / mono.format.sampleRate
        let capacity = AVAudioFrameCount(Double(mono.frameLength) * ratio) + 1_024
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else {
            recordError("無法配置音訊轉換 buffer。")
            return
        }

        var supplied = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return mono
        }
        guard error == nil, output.frameLength > 0, let channel = output.int16ChannelData else {
            recordError(error?.localizedDescription ?? "音訊轉換沒有產生資料。")
            return
        }

        let bytes = Int(output.frameLength) * 2
        let chunk = Data(bytes: channel[0], count: bytes)
        let sampleCount = Int(output.frameLength)
        var sumSquares = 0.0
        for index in 0..<sampleCount {
            let sample = Double(channel[0][index]) / 32_768.0
            sumSquares += sample * sample
        }
        let rms = sampleCount > 0 ? sqrt(sumSquares / Double(sampleCount)) : nil

        // Slice into exact frames. The tap runs on a real-time thread, so this
        // stays allocation-light and never blocks on the network.
        var frames: [Data] = []
        lock.lock()
        pending.append(chunk)
        while pending.count >= frameBytes {
            frames.append(pending.prefix(frameBytes))
            pending.removeFirst(frameBytes)
        }
        framesProduced += UInt64(frames.count)
        hasFrames = !frames.isEmpty
        lastRMS = rms
        lastError = nil
        lock.unlock()

        publishDiagnostics()

        for frame in frames {
            onFrame?(frame)
        }
    }

    private func recordError(_ message: String) {
        lock.lock()
        framesDropped += 1
        lastError = message
        lock.unlock()
        publishDiagnostics()
    }

    private func publishDiagnostics() {
        lock.lock()
        let diagnostics = AudioDiagnostics(
            inputDeviceName: inputDeviceName,
            isRunning: isRunning,
            hasFrames: hasFrames,
            rms: lastRMS,
            framesProduced: framesProduced,
            framesDropped: framesDropped,
            lastError: lastError
        )
        lock.unlock()
        onDiagnostics?(diagnostics)
    }

    private func installRuntimeObservers(
        for record: AudioInputDeviceCatalog.Record
    ) throws {
        configurationChangeObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self] _ in
            self?.handleEngineConfigurationChange()
        }

        var devicesAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let devicesListener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            self?.handleRuntimeAudioChange(source: .deviceList)
        }
        guard AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject),
            &devicesAddress,
            .main,
            devicesListener
        ) == noErr else {
            removeRuntimeObservers()
            throw CaptureError.deviceMonitoringUnavailable(reason: "無法註冊 CoreAudio 裝置清單監聽器")
        }
        deviceListAddress = devicesAddress
        deviceListListener = devicesListener

        var defaultAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let defaultListener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            self?.handleRuntimeAudioChange(source: .defaultDevice)
        }
        guard AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject),
            &defaultAddress,
            .main,
            defaultListener
        ) == noErr else {
            removeRuntimeObservers()
            throw CaptureError.deviceMonitoringUnavailable(reason: "無法註冊系統預設輸入監聽器")
        }
        defaultDeviceAddress = defaultAddress
        defaultDeviceListener = defaultListener

        var aliveAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsAlive,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let aliveListener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            self?.handleRuntimeAudioChange(source: .deviceAlive)
        }
        guard AudioObjectAddPropertyListenerBlock(
            record.deviceID,
            &aliveAddress,
            .main,
            aliveListener
        ) == noErr else {
            removeRuntimeObservers()
            throw CaptureError.deviceMonitoringUnavailable(reason: "無法註冊選定裝置存活狀態監聽器")
        }
        deviceAliveAddress = aliveAddress
        deviceAliveListener = aliveListener
    }

    private func handleEngineConfigurationChange() {
        guard isRunning else { return }
        failRuntime(
            .deviceConfigurationFailed(
                name: activeDevice?.descriptor.name ?? "",
                uid: activeDevice?.descriptor.uid ?? "",
                reason: "音訊引擎設定已變更；為避免靜默改用其他裝置，請重新開始錄音"
            )
        )
    }

    private func removeRuntimeObservers() {
        if let observer = configurationChangeObserver {
            NotificationCenter.default.removeObserver(observer)
            configurationChangeObserver = nil
        }
        if let listener = deviceListListener, var address = deviceListAddress {
            _ = AudioObjectRemovePropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                .main,
                listener
            )
        }
        deviceListListener = nil
        deviceListAddress = nil
        if let listener = defaultDeviceListener, var address = defaultDeviceAddress {
            _ = AudioObjectRemovePropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                .main,
                listener
            )
        }
        defaultDeviceListener = nil
        defaultDeviceAddress = nil
        if let listener = deviceAliveListener,
           let deviceID = monitoredDeviceID,
           var address = deviceAliveAddress {
            _ = AudioObjectRemovePropertyListenerBlock(deviceID, &address, .main, listener)
        }
        deviceAliveListener = nil
        deviceAliveAddress = nil
    }

    private func handleRuntimeAudioChange(source: RuntimeAudioChangeSource) {
        guard isRunning, let configuration = activeConfiguration else { return }
        guard engine.isRunning else {
            failRuntime(
                .deviceConfigurationFailed(
                    name: activeDevice?.descriptor.name ?? "",
                    uid: activeDevice?.descriptor.uid ?? "",
                    reason: "音訊引擎已停止"
                )
            )
            return
        }

        if case .deviceAlive = source {
            guard let deviceID = monitoredDeviceID else {
                failRuntime(
                    .deviceConfigurationFailed(
                        name: activeDevice?.descriptor.name ?? "",
                        uid: activeDevice?.descriptor.uid ?? "",
                        reason: "找不到要監測的輸入裝置 ID；為避免靜默切換，請重新開始錄音"
                    )
                )
                return
            }
            switch AudioRuntimeMonitorPolicy.deviceLivenessDecision(
                Self.queryDeviceLiveness(deviceID)
            ) {
            case .unchanged:
                break
            case .deviceUnavailable:
                failRuntime(
                    .deviceConfigurationFailed(
                        name: activeDevice?.descriptor.name ?? "",
                        uid: activeDevice?.descriptor.uid ?? "",
                        reason: "裝置已失效（CoreAudio 回報不可用）；為避免靜默改用其他裝置，請重新開始錄音"
                    )
                )
                return
            case .deviceStateQueryFailed(let status):
                failRuntime(
                    .deviceConfigurationFailed(
                        name: activeDevice?.descriptor.name ?? "",
                        uid: activeDevice?.descriptor.uid ?? "",
                        reason: "無法確認裝置存活狀態（CoreAudio 錯誤碼 \(status)）；為避免靜默切換，請重新開始錄音"
                    )
                )
                return
            case .defaultDeviceChanged:
                assertionFailure("default-device decision is not valid for a liveness query")
            }
        }

        let records: [AudioInputDeviceCatalog.Record]
        do {
            records = try AudioInputDeviceCatalog.recordsOrThrow()
        } catch {
            failRuntime(
                .deviceCatalogFailed(
                    uid: configuration.deviceUID,
                    reason: error.localizedDescription
                )
            )
            return
        }
        switch AudioInputDeviceSelection.resolve(
            storedUID: configuration.deviceUID,
            available: records.map(\.descriptor)
        ) {
        case .missingStoredDevice(let uid):
            failRuntime(.inputDeviceUnavailable(uid: uid))
        case .systemDefault:
            let defaultRecord: AudioInputDeviceCatalog.Record
            do {
                defaultRecord = try AudioInputDeviceCatalog.defaultRecordOrThrow()
            } catch let error as AudioInputDeviceCatalog.Error {
                if case .defaultDeviceNotInCatalog = error {
                    failRuntime(.noDefaultInputDevice)
                } else {
                    failRuntime(
                        .deviceCatalogFailed(
                            uid: configuration.deviceUID,
                            reason: error.localizedDescription
                        )
                    )
                }
                return
            } catch {
                failRuntime(
                    .deviceCatalogFailed(
                        uid: configuration.deviceUID,
                        reason: error.localizedDescription
                    )
                )
                return
            }
            if case .defaultDevice = source {
                switch AudioRuntimeMonitorPolicy.defaultDeviceDecision(
                    activeDeviceID: activeDevice?.deviceID,
                    currentDefaultDeviceID: defaultRecord.deviceID
                ) {
                case .unchanged:
                    break
                case .defaultDeviceChanged:
                    failRuntime(
                        .deviceConfigurationFailed(
                            name: activeDevice?.descriptor.name ?? defaultRecord.descriptor.name,
                            uid: activeDevice?.descriptor.uid ?? defaultRecord.descriptor.uid,
                            reason: "系統預設輸入裝置已變更；為避免靜默改用另一支麥克風，請重新開始錄音"
                        )
                    )
                    return
                case .deviceUnavailable, .deviceStateQueryFailed(_):
                    assertionFailure("liveness decision is not valid for a default-device comparison")
                }
            }
            if case .channel(let index) = configuration.channelPolicy,
               !(0..<defaultRecord.descriptor.inputChannels).contains(index) {
                failRuntime(
                    .channelUnavailable(
                        name: defaultRecord.descriptor.name,
                        uid: defaultRecord.descriptor.uid,
                        policy: configuration.channelPolicy,
                        available: defaultRecord.descriptor.inputChannels
                    )
                )
            }
        case .selected(let descriptor):
            guard let current = records.first(where: { $0.descriptor.uid == descriptor.uid }) else {
                failRuntime(.inputDeviceUnavailable(uid: descriptor.uid))
                return
            }
            guard current.deviceID == activeDevice?.deviceID else {
                failRuntime(
                    .deviceConfigurationFailed(
                        name: descriptor.name,
                        uid: descriptor.uid,
                        reason: "裝置已重新連線，為避免靜默切換，請重新開始錄音"
                    )
                )
                return
            }
            if case .channel(let index) = configuration.channelPolicy,
               !(0..<current.descriptor.inputChannels).contains(index) {
                failRuntime(
                    .channelUnavailable(
                        name: current.descriptor.name,
                        uid: current.descriptor.uid,
                        policy: configuration.channelPolicy,
                        available: current.descriptor.inputChannels
                    )
                )
            }
        }
    }

    private static func queryDeviceLiveness(
        _ deviceID: AudioDeviceID
    ) -> AudioDeviceLiveness {
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
        guard status == noErr else {
            return .queryFailed(status)
        }
        return alive == 0 ? .dead : .alive
    }

    private func failRuntime(_ error: CaptureError) {
        let message = error.localizedDescription
        lock.lock()
        guard isRunning, failureGate.beginFailure() else {
            lock.unlock()
            return
        }
        lastError = message
        lock.unlock()

        teardownCapture()
        publishDiagnostics()
        onError?(message)
    }

}
