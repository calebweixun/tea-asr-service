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

    // MARK: - Lease handoff timing

    func testCaptureWaitsForTheMonitorToReleaseTheDeviceAndThenStarts() throws {
        let uid = "handoff-\(UUID().uuidString)"
        let monitorLease = try AudioInputLeaseCoordinator.acquire(deviceUID: uid)
        XCTAssertTrue(AudioInputLeaseCoordinator.isHeld)

        // The monitor teardown finishes shortly after dictation is requested.
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.15) {
            monitorLease.release()
        }

        let captureLease = try AudioInputLeaseCoordinator.acquire(
            deviceUID: uid,
            waitingUpTo: 2.0
        )
        defer { captureLease.release() }

        XCTAssertEqual(captureLease.deviceUID, uid)
    }

    func testCaptureFailsWithAnExplicitTimeoutWhenTheMonitorNeverReleases() throws {
        let uid = "stuck-\(UUID().uuidString)"
        let stuckLease = try AudioInputLeaseCoordinator.acquire(deviceUID: uid)
        defer { stuckLease.release() }

        XCTAssertThrowsError(
            try AudioInputLeaseCoordinator.acquire(deviceUID: uid, waitingUpTo: 0.2)
        ) { error in
            guard case .deviceHandoffTimedOut(let reportedUID, let seconds)? =
                error as? AudioLevelMonitorError else {
                return XCTFail("expected a bounded handoff timeout, got \(error)")
            }
            XCTAssertEqual(reportedUID, uid)
            XCTAssertEqual(seconds, 0.2, accuracy: 0.0001)
        }
    }

    func testHandoffTimeoutMessageNamesTheDeviceAndRulesOutASilentFallback() {
        let message = AudioCapture.CaptureError.inputHandoffTimedOut(
            name: "MacBook Pro的麥克風",
            uid: "BuiltInMicrophoneDevice",
            seconds: 1.0
        ).localizedDescription

        XCTAssertTrue(message.contains("MacBook Pro的麥克風"))
        XCTAssertTrue(message.contains("BuiltInMicrophoneDevice"))
        XCTAssertTrue(message.contains("沒有改用其他裝置"))
    }

    func testWaitUntilIdleReportsWhetherTheDeviceWasActuallyReleased() throws {
        let uid = "idle-\(UUID().uuidString)"
        let lease = try AudioInputLeaseCoordinator.acquire(deviceUID: uid)
        XCTAssertFalse(AudioInputLeaseCoordinator.waitUntilIdle(timeout: 0.05))
        lease.release()
        XCTAssertTrue(AudioLevelMonitor.waitForInputHandoff(timeout: 0.05))
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
