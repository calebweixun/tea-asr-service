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

enum AudioInputDeviceOption: Equatable {
    case systemDefault
    case available(AudioInputDevice)
    case unavailable(uid: String)

    var uid: String? {
        switch self {
        case .systemDefault:
            return nil
        case .available(let device):
            return device.uid
        case .unavailable(let uid):
            return uid
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
        }
    }

    var isEnabled: Bool {
        if case .unavailable = self { return false }
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
    struct Record {
        let descriptor: AudioInputDevice
        let deviceID: AudioDeviceID
    }

    static func enumerate() -> [AudioInputDevice] {
        records().map(\.descriptor)
    }

    static func records() -> [Record] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var dataSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &dataSize
        ) == noErr else {
            return []
        }

        let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.stride
        guard count > 0 else { return [] }
        var ids = [AudioDeviceID](repeating: AudioDeviceID(kAudioObjectUnknown), count: count)
        let readStatus = ids.withUnsafeMutableBytes { bytes in
            guard let baseAddress = bytes.baseAddress else { return OSStatus(-50) }
            return AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                0,
                nil,
                &dataSize,
                baseAddress
            )
        }
        guard readStatus == noErr else { return [] }

        return ids.compactMap { deviceID in
            guard
                let uid = stringProperty(deviceID, selector: kAudioDevicePropertyDeviceUID),
                let name = stringProperty(deviceID, selector: kAudioObjectPropertyName),
                let channelCount = inputChannelCount(deviceID),
                channelCount > 0
            else { return nil }
            return Record(
                descriptor: AudioInputDevice(
                    uid: uid,
                    name: name,
                    inputChannels: channelCount
                ),
                deviceID: deviceID
            )
        }
    }

    static func defaultRecord() -> Record? {
        var deviceID = AudioDeviceID(kAudioObjectUnknown)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &size,
            &deviceID
        ) == noErr else { return nil }
        return records().first(where: { $0.deviceID == deviceID })
    }

    private static func stringProperty(
        _ deviceID: AudioDeviceID,
        selector: AudioObjectPropertySelector
    ) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &size,
            &value
        ) == noErr, let value else { return nil }
        return value.takeUnretainedValue() as String
    }

    private static func inputChannelCount(_ deviceID: AudioDeviceID) -> Int? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(deviceID, &address, 0, nil, &size) == noErr,
              size >= UInt32(MemoryLayout<AudioBufferList>.size)
        else { return nil }

        let raw = UnsafeMutableRawPointer.allocate(
            byteCount: Int(size),
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { raw.deallocate() }
        let list = raw.assumingMemoryBound(to: AudioBufferList.self)
        guard AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &size,
            list
        ) == noErr else { return nil }
        let buffers = UnsafeMutableAudioBufferListPointer(list)
        return buffers.reduce(0) { $0 + Int($1.mNumberChannels) }
    }
}
