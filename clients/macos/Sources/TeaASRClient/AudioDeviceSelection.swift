import AVFoundation
import CoreAudio
import Foundation

/// How an input with more than one channel is reduced to the mono stream
/// expected by the server.  The raw value is persisted; a CoreAudio device ID
/// is deliberately not persisted because it can change when devices are
/// reconnected.
enum AudioChannelPolicy: Equatable, Hashable {
    case mixdown
    case channel(Int)

    static let defaultValue: AudioChannelPolicy = .mixdown

    init(rawValue: String?) {
        guard let rawValue, rawValue.hasPrefix("channel:") else {
            self = .mixdown
            return
        }
        let suffix = rawValue.dropFirst("channel:".count)
        guard let index = Int(suffix), index >= 0 else {
            self = .mixdown
            return
        }
        self = .channel(index)
    }

    var rawValue: String {
        switch self {
        case .mixdown:
            return "mixdown"
        case .channel(let index):
            return "channel:\(index)"
        }
    }

    var title: String {
        switch self {
        case .mixdown:
            return "自動混音（所有聲道）"
        case .channel(let index):
            return "只使用聲道 \(index + 1)"
        }
    }
}

/// Stable, user-visible information about an input device.  The runtime
/// AudioDeviceID is intentionally not part of this value: it is volatile and
/// must never be written to preferences.
struct AudioInputDevice: Equatable, Identifiable {
    static let systemDefaultUID = ""
    static let systemDefaultName = "系統預設"

    let uid: String
    let name: String
    let inputChannels: Int

    var id: String { uid }
    var isSystemDefault: Bool { uid.isEmpty }

    static var systemDefault: AudioInputDevice {
        AudioInputDevice(uid: systemDefaultUID, name: systemDefaultName, inputChannels: 0)
    }
}

enum AudioInputDeviceResolution: Equatable {
    case systemDefault
    case selected(AudioInputDevice)
    case missingStoredDevice(String)
}

enum AudioInputDeviceSelection {
    /// A saved device is strict: silently switching to another microphone is
    /// surprising and can leak speech to the wrong input.  The UI can offer
    /// System Default explicitly, while a missing saved device gets an
    /// actionable error from AudioCapture.start().
    static func resolve(
        storedUID: String?,
        available: [AudioInputDevice]
    ) -> AudioInputDeviceResolution {
        guard let storedUID else { return .systemDefault }
        let normalized = storedUID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else { return .systemDefault }
        guard let device = available.first(where: { $0.uid == normalized }) else {
            return .missingStoredDevice(normalized)
        }
        return .selected(device)
    }
}

/// CoreAudio routing has two independent failure points: setting the device
/// and asking the audio unit which device it actually owns.  Keeping this
/// validation pure prevents a failed read-back from becoming an implicit
/// route to the system default and makes the policy testable without hardware.
enum AudioInputRoutingError: LocalizedError, Equatable {
    case audioUnitUnavailable
    case setFailed(OSStatus)
    case readbackFailed(OSStatus)
    case readbackMismatch(requested: AudioDeviceID, observed: AudioDeviceID)

    var errorDescription: String? {
        switch self {
        case .audioUnitUnavailable:
            return "找不到 AVAudioEngine 的輸入單元。"
        case .setFailed(let status):
            return "CoreAudio 設定指定輸入裝置失敗（OSStatus: \(status)）。"
        case .readbackFailed(let status):
            return "CoreAudio 無法讀回目前輸入裝置（OSStatus: \(status)）。"
        case .readbackMismatch(let requested, let observed):
            return "CoreAudio 讀回的裝置 ID (\(observed)) 與指定 ID (\(requested)) 不一致。"
        }
    }
}

enum AudioInputRoutingPolicy {
    static func validate(
        setStatus: OSStatus,
        readbackStatus: OSStatus,
        requested: AudioDeviceID,
        observed: AudioDeviceID?
    ) throws {
        guard setStatus == noErr else {
            throw AudioInputRoutingError.setFailed(setStatus)
        }
        guard readbackStatus == noErr, let observed else {
            throw AudioInputRoutingError.readbackFailed(readbackStatus)
        }
        guard observed == requested else {
            throw AudioInputRoutingError.readbackMismatch(
                requested: requested,
                observed: observed
            )
        }
    }
}

/// Configure the input unit before the engine starts, then verify that CoreAudio
/// accepted the requested device. A successful setter alone is not proof of the
/// route: some virtual and aggregate devices keep the old device silently.
enum AudioInputDeviceRouting {
    static func configure(
        deviceID: AudioDeviceID,
        on input: AVAudioInputNode
    ) throws {
        guard let audioUnit = input.audioUnit else {
            throw AudioInputRoutingError.audioUnitUnavailable
        }

        var requestedDeviceID = deviceID
        let setStatus = AudioUnitSetProperty(
            audioUnit,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &requestedDeviceID,
            UInt32(MemoryLayout<AudioDeviceID>.size)
        )

        var observedDeviceID = AudioDeviceID(kAudioObjectUnknown)
        var readbackSize = UInt32(MemoryLayout<AudioDeviceID>.size)
        let readbackStatus = AudioUnitGetProperty(
            audioUnit,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &observedDeviceID,
            &readbackSize
        )

        try AudioInputRoutingPolicy.validate(
            setStatus: setStatus,
            readbackStatus: readbackStatus,
            requested: deviceID,
            observed: readbackStatus == noErr ? observedDeviceID : nil
        )
    }
}

/// The input node reports the device's native format after routing.  Validate
/// it before installing a tap so an unsupported device fails before the engine
/// starts instead of failing later from inside the audio callback.
enum AudioInputFormatError: LocalizedError, Equatable {
    case invalidSampleRate(Double)
    case noInputChannels
    case channelOutOfBounds(index: Int, count: Int)
    case unsupportedSampleFormat

    var errorDescription: String? {
        switch self {
        case .invalidSampleRate(let sampleRate):
            return "輸入裝置回報無效的原生取樣率（\(sampleRate) Hz）。"
        case .noInputChannels:
            return "輸入裝置回報零個輸入聲道。"
        case .channelOutOfBounds(let index, let count):
            return "指定聲道 \(index + 1) 超出裝置目前的 \(count) 個聲道。"
        case .unsupportedSampleFormat:
            return "輸入裝置的原生 PCM 格式不是目前支援的 Float32 或 Int16。"
        }
    }
}

enum AudioInputFormatPolicy {
    static func validate(
        sampleRate: Double,
        channelCount: Int,
        commonFormat: AVAudioCommonFormat,
        channelPolicy: AudioChannelPolicy
    ) throws {
        guard sampleRate.isFinite, sampleRate > 0 else {
            throw AudioInputFormatError.invalidSampleRate(sampleRate)
        }
        guard channelCount > 0 else {
            throw AudioInputFormatError.noInputChannels
        }
        if case .channel(let index) = channelPolicy,
           !(0..<channelCount).contains(index) {
            throw AudioInputFormatError.channelOutOfBounds(
                index: index,
                count: channelCount
            )
        }
        switch commonFormat {
        case .pcmFormatFloat32, .pcmFormatInt16:
            break
        default:
            throw AudioInputFormatError.unsupportedSampleFormat
        }
    }
}

enum AudioInputDeviceOption: Equatable {
    case systemDefault
    case available(AudioInputDevice)
    case unavailable(uid: String)
    case enumerationError(message: String)
    /// One or more devices exist but could not be read (a genuine per-device
    /// CoreAudio failure, not a legitimate zero-channel output device).
    /// Enumeration still succeeded for every other device, so this is a
    /// disabled diagnostic row alongside the usable list rather than a
    /// substitute for it — the failure stays visible without hiding the
    /// microphones that did resolve.
    case skippedDevices(count: Int, message: String)

    var uid: String? {
        switch self {
        case .systemDefault:
            return nil
        case .available(let device):
            return device.uid
        case .unavailable(let uid):
            return uid
        case .enumerationError:
            return nil
        case .skippedDevices:
            return nil
        }
    }

    var title: String {
        switch self {
        case .systemDefault:
            return AudioInputDevice.systemDefaultName
        case .available(let device):
            return device.name
        case .unavailable(let uid):
            return "不可用：\(uid)"
        case .enumerationError(let message):
            return "無法列出輸入裝置：\(message)"
        case .skippedDevices(let count, let message):
            return "有 \(count) 台裝置無法讀取：\(message)"
        }
    }

    var isEnabled: Bool {
        if case .unavailable = self { return false }
        if case .enumerationError = self { return false }
        if case .skippedDevices = self { return false }
        return true
    }
}

enum AudioInputChannelOption: Equatable {
    case mixdown
    case channel(Int)
    case unavailable(AudioChannelPolicy)

    var policy: AudioChannelPolicy {
        switch self {
        case .mixdown:
            return .mixdown
        case .channel(let index):
            return .channel(index)
        case .unavailable(let policy):
            return policy
        }
    }

    var title: String {
        switch self {
        case .mixdown:
            return AudioChannelPolicy.mixdown.title
        case .channel(let index):
            return AudioChannelPolicy.channel(index).title
        case .unavailable(let policy):
            return "不可用：\(policy.title)"
        }
    }

    var isEnabled: Bool {
        if case .unavailable = self { return false }
        return true
    }
}

enum AudioInputSettingsOptions {
    static func deviceOptions(
        storedUID: String?,
        enumeration: Result<[AudioInputDevice], AudioInputDeviceCatalog.Error>,
        skipped: [AudioInputDeviceCatalog.SkippedDevice] = []
    ) -> [AudioInputDeviceOption] {
        let available: [AudioInputDevice]
        let failureMessage: String?
        switch enumeration {
        case .success(let devices):
            available = devices
            failureMessage = devices.isEmpty
                ? "CoreAudio 未回報任何輸入裝置（裝置清單為 0 bytes）。"
                : nil
        case .failure(let error):
            available = []
            failureMessage = error.localizedDescription
        }

        let storedOption = deviceOption(storedUID: storedUID, available: available)
        let failureOption = failureMessage.map(AudioInputDeviceOption.enumerationError)
        // Per-device skips are reported even when overall enumeration
        // succeeded: losing one microphone to a bad read is a real, visible
        // failure and must not be conflated with (or hidden behind) the
        // "no devices at all" diagnostic above.
        let skippedOption: AudioInputDeviceOption? = skipped.isEmpty
            ? nil
            : .skippedDevices(
                count: skipped.count,
                message: skipped.map { device in
                    let name = device.name.map { "\($0) (id \(device.deviceID))" } ?? "id \(device.deviceID)"
                    return "\(name)：\(device.reason)"
                }.joined(separator: "；")
            )
        return [AudioInputDeviceOption.systemDefault]
            + (failureOption.map { [$0] } ?? [])
            + (skippedOption.map { [$0] } ?? [])
            + available.map(AudioInputDeviceOption.available)
            + (storedOption.isEnabled ? [] : [storedOption])
    }

    static func deviceOption(
        storedUID: String?,
        available: [AudioInputDevice]
    ) -> AudioInputDeviceOption {
        switch AudioInputDeviceSelection.resolve(storedUID: storedUID, available: available) {
        case .systemDefault:
            return .systemDefault
        case .selected(let device):
            return .available(device)
        case .missingStoredDevice(let uid):
            return .unavailable(uid: uid)
        }
    }

    static func channelOptions(
        storedPolicy: AudioChannelPolicy,
        availableChannels: Int?
    ) -> [AudioInputChannelOption] {
        var options: [AudioInputChannelOption] = [.mixdown]
        if let availableChannels, availableChannels > 0 {
            options += (0..<availableChannels).map(AudioInputChannelOption.channel)
        }
        if case .channel(let index) = storedPolicy,
           availableChannels == nil || !(0..<max(availableChannels ?? 0, 0)).contains(index) {
            options.append(.unavailable(storedPolicy))
        }
        return options
    }
}

struct AudioInputConfiguration: Equatable {
    var deviceUID: String?
    var channelPolicy: AudioChannelPolicy

    static let `default` = AudioInputConfiguration(
        deviceUID: nil,
        channelPolicy: .mixdown
    )
}

/// The result of querying a CoreAudio device's live state.  Keeping this
/// value independent from CoreAudio makes the hot-unplug policy deterministic
/// and testable without requiring a real microphone.
enum AudioDeviceLiveness: Equatable {
    case alive
    case dead
    case queryFailed(OSStatus)
}

enum AudioRuntimeMonitorDecision: Equatable {
    case unchanged
    case deviceUnavailable
    case deviceStateQueryFailed(OSStatus)
    case defaultDeviceChanged
}

enum AudioRuntimeMonitorPolicy {
    static func deviceLivenessDecision(
        _ liveness: AudioDeviceLiveness
    ) -> AudioRuntimeMonitorDecision {
        switch liveness {
        case .alive:
            return .unchanged
        case .dead:
            return .deviceUnavailable
        case .queryFailed(let status):
            return .deviceStateQueryFailed(status)
        }
    }

    static func defaultDeviceDecision(
        activeDeviceID: AudioDeviceID?,
        currentDefaultDeviceID: AudioDeviceID?
    ) -> AudioRuntimeMonitorDecision {
        guard let activeDeviceID, let currentDefaultDeviceID else {
            return .defaultDeviceChanged
        }
        return activeDeviceID == currentDefaultDeviceID
            ? .unchanged
            : .defaultDeviceChanged
    }
}

/// Pure channel policy logic, kept separate from AVAudioPCMBuffer plumbing so
/// channel bounds and downmix behavior can be tested without a microphone.
enum AudioChannelMixer {
    enum Error: LocalizedError, Equatable {
        case noChannels
        case inconsistentFrameCounts
        case channelOutOfBounds(index: Int, count: Int)

        var errorDescription: String? {
            switch self {
            case .noChannels:
                return "輸入裝置沒有可用聲道。請選擇其他麥克風。"
            case .inconsistentFrameCounts:
                return "輸入裝置的聲道資料長度不一致。請重新選擇裝置後再試。"
            case .channelOutOfBounds(let index, let count):
                return "選取的聲道 \(index + 1) 不存在；目前裝置只有 \(count) 個聲道。請改用自動混音或重新選擇聲道。"
            }
        }
    }

    static func downmix(
        _ channels: [[Float]],
        policy: AudioChannelPolicy
    ) throws -> [Float] {
        guard !channels.isEmpty else { throw Error.noChannels }
        let frameCount = channels[0].count
        guard channels.dropFirst().allSatisfy({ $0.count == frameCount }) else {
            throw Error.inconsistentFrameCounts
        }

        switch policy {
        case .mixdown:
            return (0..<frameCount).map { frame in
                let sum = channels.reduce(Float.zero) { partial, channel in
                    partial + channel[frame]
                }
                return sum / Float(channels.count)
            }
        case .channel(let index):
            guard channels.indices.contains(index) else {
                throw Error.channelOutOfBounds(index: index, count: channels.count)
            }
            return channels[index]
        }
    }

    /// Convert the engine's source buffer into a non-interleaved mono Float32
    /// buffer before AVAudioConverter performs the sample-rate conversion.
    static func makeMonoBuffer(
        from buffer: AVAudioPCMBuffer,
        policy: AudioChannelPolicy
    ) throws -> AVAudioPCMBuffer {
        let channelCount = Int(buffer.format.channelCount)
        guard channelCount > 0 else { throw Error.noChannels }

        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: buffer.format.sampleRate,
            channels: 1,
            interleaved: false
        ), let output = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: buffer.frameLength
        ), let destination = output.floatChannelData?[0]
        else {
            throw Error.inconsistentFrameCounts
        }

        let frameCount = Int(buffer.frameLength)
        if buffer.format.isInterleaved {
            guard let rawData = buffer.audioBufferList.pointee.mBuffers.mData else {
                throw Error.inconsistentFrameCounts
            }
            if case .channel(let index) = policy,
               !(0..<channelCount).contains(index) {
                throw Error.channelOutOfBounds(index: index, count: channelCount)
            }
            switch buffer.format.commonFormat {
            case .pcmFormatFloat32:
                let source = rawData.assumingMemoryBound(to: Float.self)
                for frame in 0..<frameCount {
                    if case .channel(let index) = policy {
                        destination[frame] = source[frame * channelCount + index]
                    } else {
                        var sum: Float = 0
                        for channel in 0..<channelCount {
                            sum += source[frame * channelCount + channel]
                        }
                        destination[frame] = sum / Float(channelCount)
                    }
                }
            case .pcmFormatInt16:
                let source = rawData.assumingMemoryBound(to: Int16.self)
                for frame in 0..<frameCount {
                    if case .channel(let index) = policy {
                        destination[frame] = Float(source[frame * channelCount + index]) / 32_768.0
                    } else {
                        var sum: Float = 0
                        for channel in 0..<channelCount {
                            sum += Float(source[frame * channelCount + channel]) / 32_768.0
                        }
                        destination[frame] = sum / Float(channelCount)
                    }
                }
            default:
                throw Error.inconsistentFrameCounts
            }
            output.frameLength = buffer.frameLength
            return output
        }

        switch policy {
        case .mixdown:
            guard let source = buffer.floatChannelData else {
                if let source = buffer.int16ChannelData {
                    for frame in 0..<frameCount {
                        var sum: Float = 0
                        for channel in 0..<channelCount {
                            sum += Float(source[channel][frame]) / 32_768.0
                        }
                        destination[frame] = sum / Float(channelCount)
                    }
                    output.frameLength = buffer.frameLength
                    return output
                }
                throw Error.inconsistentFrameCounts
            }
            for frame in 0..<frameCount {
                var sum: Float = 0
                for channel in 0..<channelCount {
                    sum += source[channel][frame]
                }
                destination[frame] = sum / Float(channelCount)
            }
        case .channel(let index):
            guard channelCount > index, index >= 0 else {
                throw Error.channelOutOfBounds(index: index, count: channelCount)
            }
            if let source = buffer.floatChannelData {
                for frame in 0..<frameCount {
                    destination[frame] = source[index][frame]
                }
            } else if let source = buffer.int16ChannelData {
                for frame in 0..<frameCount {
                    destination[frame] = Float(source[index][frame]) / 32_768.0
                }
            } else {
                throw Error.inconsistentFrameCounts
            }
        }
        output.frameLength = buffer.frameLength
        return output
    }
}

/// Runtime bridge from CoreAudio's volatile IDs to stable descriptors.
enum AudioInputDeviceCatalog {
    enum Error: LocalizedError, Equatable {
        case deviceListSize(OSStatus)
        case deviceListRead(OSStatus)
        case invalidDeviceListSize(UInt32)
        case deviceProperty(deviceID: AudioDeviceID, selector: AudioObjectPropertySelector, status: OSStatus)
        case missingDeviceProperty(deviceID: AudioDeviceID, selector: AudioObjectPropertySelector)
        case invalidStreamConfiguration(deviceID: AudioDeviceID, size: UInt32)
        case defaultDeviceQuery(OSStatus)
        case defaultDeviceNotInCatalog(AudioDeviceID)

        var errorDescription: String? {
            switch self {
            case .deviceListSize(let status):
                return "無法取得 CoreAudio 輸入裝置清單大小（OSStatus: \(status)）。"
            case .deviceListRead(let status):
                return "無法讀取 CoreAudio 輸入裝置清單（OSStatus: \(status)）。"
            case .invalidDeviceListSize(let size):
                return "CoreAudio 回報無效的裝置清單大小（\(size) bytes）。"
            case .deviceProperty(let deviceID, let selector, let status):
                return "無法讀取 CoreAudio 裝置 \(deviceID) 的屬性 \(selector)（OSStatus: \(status)）。"
            case .missingDeviceProperty(let deviceID, let selector):
                return "CoreAudio 裝置 \(deviceID) 沒有屬性 \(selector) 的值。"
            case .invalidStreamConfiguration(let deviceID, let size):
                return "CoreAudio 裝置 \(deviceID) 回報無效的輸入 stream configuration（\(size) bytes）。"
            case .defaultDeviceQuery(let status):
                return "無法查詢系統預設輸入裝置（OSStatus: \(status)）。"
            case .defaultDeviceNotInCatalog(let deviceID):
                return "系統預設輸入裝置 ID \(deviceID) 不在目前的 CoreAudio 裝置清單中。"
            }
        }
    }

    struct Record {
        let descriptor: AudioInputDevice
        let deviceID: AudioDeviceID
    }

    /// One device that could not be read while building the catalog. Kept
    /// separate from `Error` (which is for systemic failures that abort the
    /// whole enumeration) so a single bad device stays a visible, itemized
    /// diagnostic instead of either silently vanishing or taking every other
    /// microphone down with it.
    struct SkippedDevice: Equatable {
        let deviceID: AudioDeviceID
        let name: String?
        let reason: String
    }

    private enum InputChannelQuery {
        case count(Int)
        case noInputStream
    }

    /// Output-only AudioObjects are present in the global device list but do
    /// not expose an input stream configuration.  They must not poison the
    /// microphone catalog; a selected UID is still retained as a zero-channel
    /// record so capture reports a visible failure instead of defaulting.
    static func isOutputOnlyStatus(_ status: OSStatus) -> Bool {
        status == kAudioHardwareUnknownPropertyError
            || status == kAudioHardwareBadStreamError
            || status == kAudioHardwareUnsupportedOperationError
    }

    static func enumerate() throws -> [AudioInputDevice] {
        inputDescriptors(from: try records())
    }

    /// Same as `enumerate()`, but also reports devices that were skipped
    /// because reading their properties failed (not because they are
    /// legitimately output-only). `skipped` is replaced, not appended to.
    static func enumerate(skipped: inout [SkippedDevice]) throws -> [AudioInputDevice] {
        inputDescriptors(from: try recordsOrThrow(skipped: &skipped))
    }

    static func enumerationResult() -> Result<[AudioInputDevice], Error> {
        var skipped: [SkippedDevice] = []
        return enumerationResult(skipped: &skipped)
    }

    /// Same as `enumerationResult()`, but also reports devices skipped due to
    /// a genuine per-device read failure so the caller can surface them
    /// instead of the failure disappearing into a shorter device list.
    static func enumerationResult(skipped: inout [SkippedDevice]) -> Result<[AudioInputDevice], Error> {
        do {
            return .success(try enumerate(skipped: &skipped))
        } catch let error as Error {
            return .failure(error)
        } catch {
            // `enumerate()` only throws `AudioInputDeviceCatalog.Error`, but
            // keep this seam total if a future implementation adds another
            // CoreAudio failure type.
            return .failure(.deviceListRead(OSStatus(paramErr)))
        }
    }

    static func inputDescriptors(from records: [Record]) -> [AudioInputDevice] {
        records
            .filter { $0.descriptor.inputChannels > 0 }
            .map(\.descriptor)
    }

    static func records() throws -> [Record] {
        try recordsOrThrow()
    }

    static func recordsOrThrow() throws -> [Record] {
        var skipped: [SkippedDevice] = []
        return try recordsOrThrow(skipped: &skipped)
    }

    /// Builds the device catalog. A failure reading the global device list
    /// (size or contents) is systemic and still aborts with `throw` — there
    /// is nothing to enumerate without it. A failure reading one device's own
    /// properties (UID, name, or stream configuration) is local to that
    /// device: it is recorded into `skipped` and enumeration continues, so
    /// one uncooperative AudioObject can never blank out every microphone.
    static func recordsOrThrow(skipped: inout [SkippedDevice]) throws -> [Record] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var dataSize: UInt32 = 0
        let sizeStatus = AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &dataSize
        )
        guard sizeStatus == noErr else {
            throw Error.deviceListSize(sizeStatus)
        }

        guard dataSize % UInt32(MemoryLayout<AudioDeviceID>.stride) == 0 else {
            throw Error.invalidDeviceListSize(dataSize)
        }
        let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.stride
        guard count > 0 else { return [] }
        var ids = [AudioDeviceID](repeating: AudioDeviceID(kAudioObjectUnknown), count: count)
        let readStatus = ids.withUnsafeMutableBytes { bytes in
            guard let baseAddress = bytes.baseAddress else { return OSStatus(paramErr) }
            return AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                0,
                nil,
                &dataSize,
                baseAddress
            )
        }
        guard readStatus == noErr else {
            throw Error.deviceListRead(readStatus)
        }

        var records: [Record] = []
        records.reserveCapacity(ids.count)
        for deviceID in ids {
            do {
                let uid = try stringProperty(deviceID, selector: kAudioDevicePropertyDeviceUID)
                let name = try stringProperty(deviceID, selector: kAudioObjectPropertyName)
                let channelCount: Int
                switch try inputChannelCount(deviceID) {
                case .count(let count):
                    channelCount = count
                case .noInputStream:
                    channelCount = 0
                }
                records.append(
                    Record(
                        descriptor: AudioInputDevice(
                            uid: uid,
                            name: name,
                            inputChannels: channelCount
                        ),
                        deviceID: deviceID
                    )
                )
            } catch let error as Error {
                let name = try? stringProperty(deviceID, selector: kAudioObjectPropertyName)
                skipped.append(
                    SkippedDevice(
                        deviceID: deviceID,
                        name: name,
                        reason: error.errorDescription ?? "未知錯誤"
                    )
                )
            }
        }
        return records
    }

    static func defaultRecord() -> Record? {
        try? defaultRecordOrThrow()
    }

    static func defaultRecordOrThrow() throws -> Record {
        var deviceID = AudioDeviceID(kAudioObjectUnknown)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &size,
            &deviceID
        )
        guard status == noErr else {
            throw Error.defaultDeviceQuery(status)
        }
        guard let record = try recordsOrThrow().first(where: { $0.deviceID == deviceID }) else {
            throw Error.defaultDeviceNotInCatalog(deviceID)
        }
        return record
    }

    private static func stringProperty(
        _ deviceID: AudioDeviceID,
        selector: AudioObjectPropertySelector
    ) throws -> String {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &size,
            &value
        )
        guard status == noErr else {
            throw Error.deviceProperty(deviceID: deviceID, selector: selector, status: status)
        }
        guard let value else {
            throw Error.missingDeviceProperty(deviceID: deviceID, selector: selector)
        }
        return value.takeUnretainedValue() as String
    }

    private static func inputChannelCount(_ deviceID: AudioDeviceID) throws -> InputChannelQuery {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        let sizeStatus = AudioObjectGetPropertyDataSize(deviceID, &address, 0, nil, &size)
        guard sizeStatus == noErr else {
            if isOutputOnlyStatus(sizeStatus) {
                return .noInputStream
            }
            throw Error.deviceProperty(
                deviceID: deviceID,
                selector: kAudioDevicePropertyStreamConfiguration,
                status: sizeStatus
            )
        }
        guard size > 0 else {
            return .noInputStream
        }
        // Every valid AudioBufferList starts with a 4-byte `mNumberBuffers`
        // count, even when there is nothing to describe. A pure-output
        // device's input-scope stream configuration legitimately reports
        // `mNumberBuffers == 0` — CoreAudio has been observed padding that to
        // 8 bytes for struct alignment rather than reporting 0 bytes, which
        // an earlier version of this function mistook for a malformed
        // configuration and threw on, aborting the entire device catalog for
        // one output-only AudioObject. Anything smaller than a `UInt32`
        // cannot even hold that count and is a genuine error.
        guard size >= UInt32(MemoryLayout<UInt32>.size) else {
            throw Error.invalidStreamConfiguration(deviceID: deviceID, size: size)
        }

        let raw = UnsafeMutableRawPointer.allocate(
            byteCount: Int(size),
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { raw.deallocate() }
        let readStatus = AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &size,
            raw
        )
        guard readStatus == noErr else {
            if isOutputOnlyStatus(readStatus) {
                return .noInputStream
            }
            throw Error.deviceProperty(
                deviceID: deviceID,
                selector: kAudioDevicePropertyStreamConfiguration,
                status: readStatus
            )
        }

        let numberOfBuffers = raw.load(as: UInt32.self)
        guard numberOfBuffers > 0 else {
            return .noInputStream
        }
        // A non-zero buffer count needs enough bytes for the full
        // AudioBufferList layout (at minimum one AudioBuffer entry); if
        // CoreAudio claims buffers but didn't return room for them, that is
        // a real malformed configuration, not a zero-input device.
        guard size >= UInt32(MemoryLayout<AudioBufferList>.size) else {
            throw Error.invalidStreamConfiguration(deviceID: deviceID, size: size)
        }
        let list = raw.assumingMemoryBound(to: AudioBufferList.self)
        let buffers = UnsafeMutableAudioBufferListPointer(list)
        return .count(buffers.reduce(0) { $0 + Int($1.mNumberChannels) })
    }
}
