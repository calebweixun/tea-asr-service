import AVFoundation
import AppKit
import CoreAudio
import XCTest
@testable import TeaASRClient

/// Covers the settings-page → dictation handoff and the engine-reconfiguration
/// policy that used to abort recording started from the settings page.
final class AudioHandoffTests: XCTestCase {
    private let builtInFormat = AudioInputFormatSignature(
        sampleRate: 48_000,
        channelCount: 2,
        commonFormat: .pcmFormatFloat32
    )

    // MARK: - Shared input-source lifecycle

    private func configuration(
        uid: String,
        channelPolicy: AudioChannelPolicy = .mixdown
    ) -> AudioInputSourceLifecycle.Configuration {
        AudioInputSourceLifecycle.Configuration(
            deviceUID: uid,
            channelPolicy: channelPolicy
        )
    }

    func testLevelAndCaptureConsumersShareOneOpenAndCloseAfterLastDetach() throws {
        var openCount = 0
        var closeCount = 0
        var levelBuffers: [Data] = []
        var captureBuffers: [Data] = []
        let source = AudioInputSourceLifecycle(
            open: { _ in openCount += 1 },
            close: { closeCount += 1 },
            reconfigure: { _ in }
        )
        let config = configuration(uid: "shared-\(UUID().uuidString)")

        let level = try source.attach(configuration: config) { levelBuffers.append($0) }
        let capture = try source.attach(configuration: config) { captureBuffers.append($0) }
        source.publish(Data([1, 2, 3]))

        XCTAssertEqual(openCount, 1, "two consumers must open one input resource")
        XCTAssertEqual(levelBuffers, [Data([1, 2, 3])])
        XCTAssertEqual(captureBuffers, [Data([1, 2, 3])])
        XCTAssertEqual(source.consumerCount, 2)

        try source.detach(level)
        XCTAssertTrue(source.isOpen, "the recording consumer still owns the source")
        XCTAssertEqual(closeCount, 0)
        try source.detach(capture)
        XCTAssertFalse(source.isOpen)
        XCTAssertEqual(closeCount, 1, "the last consumer must release the input")
    }

    func testMeterConsumerCannotDropRecordingConsumerBuffers() throws {
        var meterCount = 0
        var recordingBuffers: [Data] = []
        let source = AudioInputSourceLifecycle(
            open: { _ in },
            close: {},
            reconfigure: { _ in }
        )
        let config = configuration(uid: "fanout-\(UUID().uuidString)")
        let meter = try source.attach(configuration: config) { _ in meterCount += 1 }
        let recording = try source.attach(configuration: config) { recordingBuffers.append($0) }

        for value in 0..<4 { source.publish(Data([UInt8(value)])) }

        XCTAssertEqual(meterCount, 4)
        XCTAssertEqual(recordingBuffers, (0..<4).map { Data([UInt8($0)]) })
        try source.detach(meter)
        try source.detach(recording)
    }

    func testChangingDeviceOrChannelReconfiguresTheSharedSource() throws {
        var opened: [AudioInputSourceLifecycle.Configuration] = []
        var reconfigured: [AudioInputSourceLifecycle.Configuration] = []
        var closeCount = 0
        let source = AudioInputSourceLifecycle(
            open: { opened.append($0) },
            close: { closeCount += 1 },
            reconfigure: { reconfigured.append($0) }
        )
        let consumer = try source.attach(
            configuration: configuration(uid: "device-a", channelPolicy: .mixdown),
            handler: { _ in }
        )
        try source.update(
            consumer,
            configuration: configuration(uid: "device-a", channelPolicy: .channel(1))
        )
        try source.update(
            consumer,
            configuration: configuration(uid: "device-b", channelPolicy: .channel(1))
        )

        XCTAssertEqual(opened.count, 1)
        XCTAssertEqual(reconfigured, [
            configuration(uid: "device-a", channelPolicy: .channel(1)),
            configuration(uid: "device-b", channelPolicy: .channel(1))
        ])
        XCTAssertEqual(source.activeConfiguration, configuration(uid: "device-b", channelPolicy: .channel(1)))
        try source.detach(consumer)
        XCTAssertEqual(closeCount, 3, "each configuration switch closes the old stream and the final detach closes the new one")
    }

    func testMismatchedSecondConsumerFailsWithoutOpeningAnotherDevice() throws {
        var openCount = 0
        let source = AudioInputSourceLifecycle(
            open: { _ in openCount += 1 },
            close: {},
            reconfigure: { _ in }
        )
        let first = try source.attach(configuration: configuration(uid: "first")) { _ in }

        XCTAssertThrowsError(
            try source.attach(configuration: configuration(uid: "second")) { _ in }
        ) { error in
            guard case .configurationConflict = error as? AudioInputSourceLifecycle.Error else {
                return XCTFail("expected an explicit shared-source configuration conflict")
            }
        }
        XCTAssertEqual(openCount, 1)
        try source.detach(first)
    }

    func testSharedSourceRejectsUnknownConsumerWithoutChangingOpenState() throws {
        let source = AudioInputSourceLifecycle(
            open: { _ in },
            close: {},
            reconfigure: { _ in }
        )
        let missing = UUID()
        XCTAssertThrowsError(try source.detach(missing)) { error in
            XCTAssertEqual(error as? AudioInputSourceLifecycle.Error, .unknownConsumer)
        }
        XCTAssertFalse(source.isOpen)
    }

    // MARK: - Engine reconfiguration policy

    func testHandoffReconfigurationKeepsRecordingWhenDeviceAndFormatAreUnchanged() {
        // This is the observed handoff case: AVAudioEngine posts a
        // configuration change after start, but the input unit is still bound
        // to the selected device with the same native format.
        XCTAssertEqual(
            AudioEngineConfigurationChangePolicy.decide(
                isRunning: true,
                expectedDeviceID: 122,
                boundDeviceID: 122,
                expectedFormat: builtInFormat,
                currentFormat: builtInFormat
            ),
            .keepRunning
        )
    }

    func testReconfigurationStillFailsWhenTheInputDeviceActuallyChanged() {
        guard case .failRecording(let reason) = AudioEngineConfigurationChangePolicy.decide(
            isRunning: true,
            expectedDeviceID: 122,
            boundDeviceID: 99,
            expectedFormat: builtInFormat,
            currentFormat: builtInFormat
        ) else {
            return XCTFail("a real device switch must stop recording")
        }
        XCTAssertEqual(reason, AudioEngineConfigurationChangePolicy.deviceChangedReason)
        XCTAssertTrue(reason.contains("避免靜默改用其他裝置"))
    }

    func testReconfigurationFailsWhenTheBoundDeviceCannotBeReadBack() {
        guard case .failRecording(let reason) = AudioEngineConfigurationChangePolicy.decide(
            isRunning: true,
            expectedDeviceID: 122,
            boundDeviceID: nil,
            expectedFormat: builtInFormat,
            currentFormat: nil
        ) else {
            return XCTFail("an unprovable device identity must stop recording")
        }
        XCTAssertTrue(reason.contains("無法確認目前的輸入裝置"))
    }

    func testReconfigurationFailsWhenTheNativeFormatChangedUnderTheConverter() {
        let changed = AudioInputFormatSignature(
            sampleRate: 44_100,
            channelCount: 2,
            commonFormat: .pcmFormatFloat32
        )
        guard case .failRecording(let reason) = AudioEngineConfigurationChangePolicy.decide(
            isRunning: true,
            expectedDeviceID: 122,
            boundDeviceID: 122,
            expectedFormat: builtInFormat,
            currentFormat: changed
        ) else {
            return XCTFail("a format change invalidates the running converter")
        }
        XCTAssertTrue(reason.contains("44100.0 Hz"))
    }

    func testReconfigurationIsIgnoredWhileNotRecording() {
        XCTAssertEqual(
            AudioEngineConfigurationChangePolicy.decide(
                isRunning: false,
                expectedDeviceID: nil,
                boundDeviceID: nil,
                expectedFormat: nil,
                currentFormat: nil
            ),
            .keepRunning
        )
    }

    func testFormatSignatureComparesTheFieldsTheConverterDependsOn() {
        let format = try? XCTUnwrap(
            AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: 48_000,
                channels: 2,
                interleaved: false
            )
        )
        XCTAssertEqual(format.map(AudioInputFormatSignature.init), builtInFormat)
    }
}
