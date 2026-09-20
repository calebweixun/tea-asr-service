import AVFoundation
import Foundation
import XCTest
@testable import TeaASRClient

final class AudioDeviceSelectionTests: XCTestCase {
    func testRoutingPolicyAcceptsMatchingCurrentDeviceReadback() throws {
        XCTAssertNoThrow(
            try AudioInputRoutingPolicy.validate(
                setStatus: noErr,
                readbackStatus: noErr,
                requested: 17,
                observed: 17
            )
        )
    }

    func testRoutingPolicyRejectsSetFailureWithoutDefaultFallback() {
        XCTAssertThrowsError(
            try AudioInputRoutingPolicy.validate(
                setStatus: -10879,
                readbackStatus: noErr,
                requested: 17,
                observed: 17
            )
        ) { error in
            XCTAssertEqual(error as? AudioInputRoutingError, .setFailed(-10879))
        }
    }

    func testRoutingPolicyRejectsReadbackFailureAndMismatch() {
        XCTAssertThrowsError(
            try AudioInputRoutingPolicy.validate(
                setStatus: noErr,
                readbackStatus: -50,
                requested: 17,
                observed: nil
            )
        ) { error in
            XCTAssertEqual(error as? AudioInputRoutingError, .readbackFailed(-50))
        }

        XCTAssertThrowsError(
            try AudioInputRoutingPolicy.validate(
                setStatus: noErr,
                readbackStatus: noErr,
                requested: 17,
                observed: 18
            )
        ) { error in
            XCTAssertEqual(
                error as? AudioInputRoutingError,
                .readbackMismatch(requested: 17, observed: 18)
            )
        }
    }

    func testNativeFormatPreflightRejectsInvalidDeviceAndChannel() {
        XCTAssertThrowsError(
            try AudioInputFormatPolicy.validate(
                sampleRate: 48_000,
                channelCount: 0,
                commonFormat: .pcmFormatFloat32,
                channelPolicy: .mixdown
            )
        ) { error in
            XCTAssertEqual(error as? AudioInputFormatError, .noInputChannels)
        }

        XCTAssertThrowsError(
            try AudioInputFormatPolicy.validate(
                sampleRate: 48_000,
                channelCount: 1,
                commonFormat: .pcmFormatFloat32,
                channelPolicy: .channel(1)
            )
        ) { error in
            XCTAssertEqual(
                error as? AudioInputFormatError,
                .channelOutOfBounds(index: 1, count: 1)
            )
        }
    }

    func testCatalogSkipsOnlyExplicitOutputOnlyStreamStatuses() {
        XCTAssertTrue(
            AudioInputDeviceCatalog.isOutputOnlyStatus(kAudioHardwareUnknownPropertyError)
        )
        XCTAssertTrue(
            AudioInputDeviceCatalog.isOutputOnlyStatus(kAudioHardwareBadStreamError)
        )
        XCTAssertFalse(
            AudioInputDeviceCatalog.isOutputOnlyStatus(kAudioHardwareBadDeviceError)
        )
    }

    func testRuntimeFailureGateDeduplicatesObserverCallbacksUntilReset() {
        var gate = AudioCaptureFailureGate()

        XCTAssertTrue(gate.beginFailure())
        XCTAssertFalse(gate.beginFailure())
        gate.reset()
        XCTAssertTrue(gate.beginFailure())
    }

    func testTeardownClearsPendingPCMBeforeTheNextSession() {
        var pending = Data(repeating: 0, count: 10)

        let dropped = AudioCaptureTeardownPolicy.clearPending(&pending, frameBytes: 4)

        XCTAssertEqual(dropped, 2)
        XCTAssertTrue(pending.isEmpty)
    }

    func testDeviceAliveObserverFailsForDeadOrUnqueryableDevice() {
        XCTAssertEqual(
            AudioRuntimeMonitorPolicy.deviceLivenessDecision(.alive),
            .unchanged
        )
        XCTAssertEqual(
            AudioRuntimeMonitorPolicy.deviceLivenessDecision(.dead),
            .deviceUnavailable
        )
        XCTAssertEqual(
            AudioRuntimeMonitorPolicy.deviceLivenessDecision(.queryFailed(-1)),
            .deviceStateQueryFailed(-1)
        )
    }

    func testSystemDefaultObserverOnlyFailsWhenDefaultDeviceIDChanges() {
        XCTAssertEqual(
            AudioRuntimeMonitorPolicy.defaultDeviceDecision(
                activeDeviceID: 7,
                currentDefaultDeviceID: 7
            ),
            .unchanged
        )
        XCTAssertEqual(
            AudioRuntimeMonitorPolicy.defaultDeviceDecision(
                activeDeviceID: 7,
                currentDefaultDeviceID: 8
            ),
            .defaultDeviceChanged
        )
        XCTAssertEqual(
            AudioRuntimeMonitorPolicy.defaultDeviceDecision(
                activeDeviceID: 7,
                currentDefaultDeviceID: nil
            ),
            .defaultDeviceChanged
        )
    }

    func testMissingStoredUIDUsesSystemDefaultOnlyWhenNoUIDIsSaved() {
        let devices = [
            AudioInputDevice(uid: "uid-built-in", name: "Built-in Microphone", inputChannels: 1)
        ]

        XCTAssertEqual(
            AudioInputDeviceSelection.resolve(storedUID: nil, available: devices),
            .systemDefault
        )
        XCTAssertEqual(
            AudioInputDeviceSelection.resolve(storedUID: "", available: devices),
            .systemDefault
        )
    }

    func testStoredUIDResolvesByStableUIDAndNotDisplayName() {
        let devices = [
            AudioInputDevice(uid: "uid-usb", name: "同名麥克風", inputChannels: 2),
            AudioInputDevice(uid: "uid-other", name: "同名麥克風", inputChannels: 1),
        ]

        XCTAssertEqual(
            AudioInputDeviceSelection.resolve(storedUID: "uid-usb", available: devices),
            .selected(devices[0])
        )
    }

    func testMissingStoredUIDIsAnActionableErrorPolicy() {
        let result = AudioInputDeviceSelection.resolve(
            storedUID: "uid-disconnected",
            available: []
        )

        XCTAssertEqual(result, .missingStoredDevice("uid-disconnected"))
    }

    func testSettingsPresentationKeepsMissingUIDSelectedInsteadOfDefaulting() {
        let option = AudioInputSettingsOptions.deviceOption(
            storedUID: "uid-disconnected",
            available: []
        )

        XCTAssertEqual(option, .unavailable(uid: "uid-disconnected"))
        XCTAssertEqual(option.uid, "uid-disconnected")
        XCTAssertEqual(option.title, "不可用：uid-disconnected")
        XCTAssertFalse(option.isEnabled)
    }

    func testSettingsPresentationKeepsUnavailableChannelPolicySelected() {
        let options = AudioInputSettingsOptions.channelOptions(
            storedPolicy: .channel(2),
            availableChannels: 1
        )

        XCTAssertEqual(options.last, .unavailable(.channel(2)))
        XCTAssertEqual(options.last?.policy, .channel(2))
        XCTAssertFalse(options.last?.isEnabled ?? true)
    }

    func testSettingsPresentationRoundTripsUnavailableValuesWithoutOverwrite() {
        let suiteName = "TeaASRClientTests.AudioDeviceSelection.roundTrip.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let settings = Settings(defaults: defaults)
        settings.inputDeviceUID = "uid-disconnected"
        settings.inputChannelPolicy = .channel(2)

        let deviceOption = AudioInputSettingsOptions.deviceOption(
            storedUID: settings.inputDeviceUID,
            available: []
        )
        let channelOption = AudioInputSettingsOptions.channelOptions(
            storedPolicy: settings.inputChannelPolicy,
            availableChannels: nil
        ).last!

        // This is the same value the AppKit controls persist when the user
        // presses Save/Test or toggles another setting without choosing a new
        // device/channel.
        settings.inputDeviceUID = deviceOption.uid
        settings.inputChannelPolicy = channelOption.policy

        XCTAssertEqual(Settings(defaults: defaults).inputDeviceUID, "uid-disconnected")
        XCTAssertEqual(Settings(defaults: defaults).inputChannelPolicy, .channel(2))
    }

    func testMixdownAveragesAllChannels() throws {
        let result = try AudioChannelMixer.downmix(
            [[0, 1, -1], [1, 0, 1]],
            policy: .mixdown
        )

        XCTAssertEqual(result, [0.5, 0.5, 0])
    }

    func testChannelPolicySelectsOneChannel() throws {
        let result = try AudioChannelMixer.downmix(
            [[0, 1], [10, 11]],
            policy: .channel(1)
        )

        XCTAssertEqual(result, [10, 11])
    }

    func testChannelPolicyRejectsOutOfBoundsChannel() {
        XCTAssertThrowsError(
            try AudioChannelMixer.downmix([[0, 1]], policy: .channel(1))
        ) { error in
            XCTAssertEqual(
                error as? AudioChannelMixer.Error,
                .channelOutOfBounds(index: 1, count: 1)
            )
        }
    }

    func testPCMBufferMixerProducesMonoForMixdownAndSelectedChannel() throws {
        let format = try XCTUnwrap(
            AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: 48_000,
                channels: 2,
                interleaved: false
            )
        )
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 3))
        buffer.frameLength = 3
        let channels = try XCTUnwrap(buffer.floatChannelData)
        channels[0][0] = 0
        channels[0][1] = 1
        channels[0][2] = -1
        channels[1][0] = 1
        channels[1][1] = 0
        channels[1][2] = 1

        let mixed = try AudioChannelMixer.makeMonoBuffer(from: buffer, policy: .mixdown)
        let selected = try AudioChannelMixer.makeMonoBuffer(from: buffer, policy: .channel(1))

        XCTAssertEqual(Array(UnsafeBufferPointer(start: mixed.floatChannelData?[0], count: 3)), [0.5, 0.5, 0])
        XCTAssertEqual(Array(UnsafeBufferPointer(start: selected.floatChannelData?[0], count: 3)), [1, 0, 1])
    }

    func testSettingsPersistStableUIDAndChannelPolicy() {
        let suiteName = "TeaASRClientTests.AudioDeviceSelection.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let settings = Settings(defaults: defaults)
        XCTAssertNil(settings.inputDeviceUID)
        XCTAssertEqual(settings.inputChannelPolicy, .mixdown)

        settings.inputDeviceUID = "uid-stable-usb"
        settings.inputChannelPolicy = .channel(1)

        let reloaded = Settings(defaults: defaults)
        XCTAssertEqual(reloaded.inputDeviceUID, "uid-stable-usb")
        XCTAssertEqual(reloaded.inputChannelPolicy, .channel(1))
        XCTAssertEqual(defaults.string(forKey: "audioInputDeviceUID"), "uid-stable-usb")

        settings.inputDeviceUID = nil
        XCTAssertNil(Settings(defaults: defaults).inputDeviceUID)
    }

    func testCatalogExposesInputDevicesByStableUIDOnly() {
        switch AudioInputDeviceCatalog.enumerationResult() {
        case .success(let devices):
            XCTAssertTrue(devices.allSatisfy { !$0.uid.isEmpty })
            XCTAssertTrue(devices.allSatisfy { $0.inputChannels > 0 })
        case .failure(let error):
            XCTAssertFalse(error.localizedDescription.isEmpty)
        }
    }

    func testDeviceOptionsExposeEnumerationFailureAsVisibleDisabledError() {
        let options = AudioInputSettingsOptions.deviceOptions(
            storedUID: nil,
            enumeration: .failure(.deviceListSize(-50))
        )

        XCTAssertEqual(options.first, .systemDefault)
        guard case .enumerationError(let message) = options[1] else {
            return XCTFail("expected a visible enumeration error option")
        }
        XCTAssertTrue(message.contains("OSStatus: -50"))
        XCTAssertTrue(options[1].title.contains("無法列出輸入裝置"))
        XCTAssertFalse(options[1].isEnabled)
    }

    func testEmptyEnumerationAlsoShowsWhyOnlySystemDefaultIsAvailable() {
        let options = AudioInputSettingsOptions.deviceOptions(
            storedUID: nil,
            enumeration: .success([])
        )

        XCTAssertEqual(options.first, .systemDefault)
        XCTAssertTrue(options.contains {
            if case .enumerationError = $0 { return true }
            return false
        })
    }

    func testInputDescriptorsKeepMultichannelAndVirtualInputs() {
        let records = [
            AudioInputDeviceCatalog.Record(
                descriptor: AudioInputDevice(uid: "stereo", name: "Stereo mic", inputChannels: 2),
                deviceID: 1
            ),
            AudioInputDeviceCatalog.Record(
                descriptor: AudioInputDevice(uid: "virtual", name: "Virtual mic", inputChannels: 8),
                deviceID: 2
            ),
            AudioInputDeviceCatalog.Record(
                descriptor: AudioInputDevice(uid: "output-only", name: "Output only", inputChannels: 0),
                deviceID: 3
            ),
        ]

        XCTAssertEqual(
            AudioInputDeviceCatalog.inputDescriptors(from: records).map(\.uid),
            ["stereo", "virtual"]
        )
    }
}
