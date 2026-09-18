import AVFoundation
import Foundation

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

    /// Called on the audio thread's behalf with exactly one frame of PCM.
    var onFrame: ((Data) -> Void)?

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
        try engine.start()
        isRunning = true
    }

    func stop() {
        guard isRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        isRunning = false
        lock.lock()
        pending.removeAll()
        lock.unlock()
    }

    private func consume(
        _ buffer: AVAudioPCMBuffer,
        converter: AVAudioConverter,
        target: AVAudioFormat
    ) {
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1_024
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else {
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
            return
        }

        let bytes = Int(output.frameLength) * 2
        let chunk = Data(bytes: channel[0], count: bytes)

        // Slice into exact frames. The tap runs on a real-time thread, so this
        // stays allocation-light and never blocks on the network.
        var frames: [Data] = []
        lock.lock()
        pending.append(chunk)
        while pending.count >= frameBytes {
            frames.append(pending.prefix(frameBytes))
            pending.removeFirst(frameBytes)
        }
        lock.unlock()

        for frame in frames {
            onFrame?(frame)
        }
    }
}
