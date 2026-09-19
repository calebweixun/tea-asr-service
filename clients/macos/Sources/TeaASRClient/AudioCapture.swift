import AVFoundation
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

/// Microphone capture resampled to the 16 kHz mono PCM16 the service accepts.
///
/// AVAudioConverter is used rather than a hand-rolled resampler: docs/03 asks
/// clients not to use naive linear downsampling as a quality baseline.
final class AudioCapture {
    enum CaptureError: LocalizedError {
        case converterUnavailable
        case permissionDenied

        var errorDescription: String? {
            switch self {
            case .converterUnavailable:
                return "無法建立音訊轉換器（來源格式不支援）。"
            case .permissionDenied:
                return "沒有麥克風權限。請到「系統設定 → 隱私權與安全性 → 麥克風」允許 TEA ASR。"
            }
        }
    }

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var pending = Data()
    private let lock = NSLock()
    private let frameBytes = Wire.frameSamples * 2
    private var framesProduced: UInt64 = 0
    private var framesDropped: UInt64 = 0
    private var lastError: String?
    private var lastRMS: Double?
    private var hasFrames = false
    private var inputDeviceName: String?

    /// Called on the audio thread's behalf with exactly one frame of PCM.
    var onFrame: ((Data) -> Void)?
    /// Called on the audio thread. Consumers must dispatch UI work to main.
    var onDiagnostics: ((AudioDiagnostics) -> Void)?

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

    func start() throws {
        guard !isRunning else { return }
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            throw CaptureError.permissionDenied
        }

        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        inputDeviceName = Self.defaultInputDeviceName()
        guard
            let target = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: Double(Wire.sampleRate),
                channels: 1,
                interleaved: true
            ),
            let converter = AVAudioConverter(from: inputFormat, to: target)
        else {
            throw CaptureError.converterUnavailable
        }
        converter.sampleRateConverterQuality = AVAudioQuality.high.rawValue
        self.converter = converter

        input.installTap(onBus: 0, bufferSize: 4_096, format: inputFormat) { [weak self] buffer, _ in
            self?.consume(buffer, converter: converter, target: target)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            // installTap mutates the input node even when engine.start() fails
            // (for example when an audio device disappears). Remove it before
            // returning so a permission/device retry does not hit "tap already
            // installed" on the next start attempt.
            input.removeTap(onBus: 0)
            engine.stop()
            self.converter = nil
            isRunning = false
            publishDiagnostics()
            throw error
        }
        isRunning = true
        publishDiagnostics()
    }

    func stop() {
        guard isRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        isRunning = false
        lock.lock()
        if !pending.isEmpty {
            framesDropped += UInt64(pending.count / frameBytes)
        }
        pending.removeAll()
        lock.unlock()
        publishDiagnostics()
    }

    private func consume(
        _ buffer: AVAudioPCMBuffer,
        converter: AVAudioConverter,
        target: AVAudioFormat
    ) {
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1_024
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
            return buffer
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

    private static func defaultInputDeviceName() -> String? {
        var deviceID = AudioDeviceID(kAudioObjectUnknown)
        var deviceAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var deviceSize = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject),
                &deviceAddress,
                0,
                nil,
                &deviceSize,
                &deviceID
            ) == noErr,
            deviceID != AudioDeviceID(kAudioObjectUnknown)
        else {
            return nil
        }

        var nameAddress = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var name: Unmanaged<CFString>?
        var nameSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard
            AudioObjectGetPropertyData(
                deviceID,
                &nameAddress,
                0,
                nil,
                &nameSize,
                &name
            ) == noErr,
            let name
        else {
            return nil
        }
        return name.takeUnretainedValue() as String
    }
}
