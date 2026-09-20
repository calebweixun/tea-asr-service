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
        case converterUnavailable
        case permissionDenied
        case noDefaultInputDevice
        case inputDeviceUnavailable(uid: String)
        case deviceConfigurationFailed(name: String, reason: String)
        case channelUnavailable(policy: AudioChannelPolicy, available: Int)
        case deviceMonitoringUnavailable(reason: String)

        var errorDescription: String? {
            switch self {
            case .converterUnavailable:
                return "無法建立音訊轉換器（來源格式不支援）。"
            case .permissionDenied:
                return "沒有麥克風權限。請到「系統設定 → 隱私權與安全性 → 麥克風」允許 TEA ASR。"
            case .noDefaultInputDevice:
                return "找不到系統預設輸入裝置。請在「系統設定 → 聲音 → 輸入」選擇麥克風，或在 TEA ASR 設定指定裝置。"
            case .inputDeviceUnavailable(let uid):
                return "找不到已選取的輸入裝置（UID: \(uid)）。請在設定改選「系統預設」或重新插入該裝置。"
            case .deviceConfigurationFailed(let name, let reason):
                return "無法使用輸入裝置「\(name)」：\(reason) 請改選其他裝置後再試。"
            case .channelUnavailable(let policy, let available):
                return "輸入裝置無法使用「\(policy.title)」；目前只有 \(available) 個聲道。請改用自動混音或重新選擇聲道。"
            case .deviceMonitoringUnavailable(let reason):
                return "無法監測輸入裝置變更：\(reason) 請重新啟動 TEA ASR 後再試。"
            }
        }
    }

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var tapInstalled = false
    private var pending = Data()
    private let lock = NSLock()
    private let frameBytes = Wire.frameSamples * 2
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

    /// Called on the audio thread's behalf with exactly one frame of PCM.
    var onFrame: ((Data) -> Void)?
    /// Called on the audio thread. Consumers must dispatch UI work to main.
    var onDiagnostics: ((AudioDiagnostics) -> Void)?
    /// Called once when the selected input disappears or the engine becomes
    /// unusable while recording. Consumers should stop the ASR session and
    /// surface the message in the management UI.
    var onError: ((String) -> Void)?

    private(set) var isRunning = false

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

        let input = engine.inputNode
        let records = AudioInputDeviceCatalog.records()
        let available = records.map(\.descriptor)
        let resolution = AudioInputDeviceSelection.resolve(
            storedUID: configuration.deviceUID,
            available: available
        )
        let record: AudioInputDeviceCatalog.Record
        switch resolution {
        case .systemDefault:
            guard let defaultRecord = AudioInputDeviceCatalog.defaultRecord() else {
                throw CaptureError.noDefaultInputDevice
            }
            record = defaultRecord
        case .selected(let device):
            guard let selectedRecord = records.first(where: { $0.descriptor.uid == device.uid }) else {
                throw CaptureError.inputDeviceUnavailable(uid: device.uid)
            }
            record = selectedRecord
        case .missingStoredDevice(let uid):
            throw CaptureError.inputDeviceUnavailable(uid: uid)
        }

        // Apply this even for System Default. The same AVAudioEngine is reused
        // after stop(), and an earlier explicit device selection must not leak
        // into a later default-device session.
        if let routingError = Self.setInputDevice(record.deviceID, on: input) {
            throw CaptureError.deviceConfigurationFailed(
                name: record.descriptor.name,
                reason: routingError
            )
        }

        let inputFormat = input.outputFormat(forBus: 0)
        let inputChannelCount = Int(inputFormat.channelCount)
        guard inputChannelCount > 0 else {
            throw CaptureError.deviceConfigurationFailed(
                name: record.descriptor.name,
                reason: "裝置沒有可用的輸入聲道"
            )
        }
        if case .channel(let index) = configuration.channelPolicy,
           !(0..<inputChannelCount).contains(index) {
            throw CaptureError.channelUnavailable(
                policy: configuration.channelPolicy,
                available: inputChannelCount
            )
        }
        inputDeviceName = record.descriptor.name

        guard
            let target = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: Double(Wire.sampleRate),
                channels: 1,
                interleaved: true
            ),
            let monoFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: inputFormat.sampleRate,
                channels: 1,
                interleaved: false
            ),
            let converter = AVAudioConverter(from: monoFormat, to: target)
        else {
            throw CaptureError.converterUnavailable
        }
        converter.sampleRateConverterQuality = AVAudioQuality.high.rawValue
        self.converter = converter

        input.installTap(onBus: 0, bufferSize: 4_096, format: inputFormat) { [weak self] buffer, _ in
            self?.consume(
                buffer,
                converter: converter,
                target: target,
                channelPolicy: configuration.channelPolicy
            )
        }
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
            throw error
        }
        isRunning = true
        activeConfiguration = configuration
        activeDevice = record
        monitoredDeviceID = record.deviceID
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

    /// Stop every capture resource even when a previous start/failure already
    /// flipped `isRunning` to false.  In particular, pending PCM and the
    /// converter must not survive into the next session.
    private func teardownCapture() {
        // Flip the running flag before removing listeners/stopping the engine,
        // so a callback queued during teardown cannot start a second failure
        // path or observe a half-torn-down capture.
        isRunning = false
        removeRuntimeObservers()

        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        engine.stop()
        converter = nil

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
                        reason: "裝置已失效（CoreAudio 回報不可用）；為避免靜默改用其他裝置，請重新開始錄音"
                    )
                )
                return
            case .deviceStateQueryFailed(let status):
                failRuntime(
                    .deviceConfigurationFailed(
                        name: activeDevice?.descriptor.name ?? "",
                        reason: "無法確認裝置存活狀態（CoreAudio 錯誤碼 \(status)）；為避免靜默切換，請重新開始錄音"
                    )
                )
                return
            case .defaultDeviceChanged:
                assertionFailure("default-device decision is not valid for a liveness query")
            }
        }

        let records = AudioInputDeviceCatalog.records()
        switch AudioInputDeviceSelection.resolve(
            storedUID: configuration.deviceUID,
            available: records.map(\.descriptor)
        ) {
        case .missingStoredDevice(let uid):
            failRuntime(.inputDeviceUnavailable(uid: uid))
        case .systemDefault:
            guard let defaultRecord = AudioInputDeviceCatalog.defaultRecord() else {
                failRuntime(.noDefaultInputDevice)
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
                        reason: "裝置已重新連線，為避免靜默切換，請重新開始錄音"
                    )
                )
                return
            }
            if case .channel(let index) = configuration.channelPolicy,
               !(0..<current.descriptor.inputChannels).contains(index) {
                failRuntime(
                    .channelUnavailable(
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

    private static func setInputDevice(
        _ deviceID: AudioDeviceID,
        on input: AVAudioInputNode
    ) -> String? {
        guard let audioUnit = input.audioUnit else {
            return "找不到音訊輸入單元"
        }
        var deviceID = deviceID
        let status = AudioUnitSetProperty(
            audioUnit,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &deviceID,
            UInt32(MemoryLayout<AudioDeviceID>.size)
        )
        guard status == noErr else {
            return "CoreAudio 無法切換裝置（錯誤碼 \(status)）"
        }
        return nil
    }

}
