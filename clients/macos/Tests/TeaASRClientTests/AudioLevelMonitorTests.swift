import AVFoundation
import AppKit
import XCTest
@testable import TeaASRClient

final class AudioLevelMonitorTests: XCTestCase {
    func testLevelMathComputesRMSAndPeak() throws {
        let sample = try XCTUnwrap(AudioLevelMath.measure(samples: [0, 0.5, -0.5, 1]))

        XCTAssertEqual(sample.rms, Float(sqrt(0.375)), accuracy: 0.0001)
        XCTAssertEqual(sample.peak, 1, accuracy: 0.0001)
    }

    func testLevelMathIgnoresNonFiniteSamples() throws {
        let sample = try XCTUnwrap(
            AudioLevelMath.measure(samples: [.nan, .infinity, -0.5, 0.5])
        )

        XCTAssertEqual(sample.rms, 0.5, accuracy: 0.0001)
        XCTAssertEqual(sample.peak, 0.5, accuracy: 0.0001)
        XCTAssertNil(AudioLevelMath.measure(samples: [.nan, -.infinity]))
    }

    func testLevelMathReadsNonInterleavedPCMBuffer() throws {
        let format = try XCTUnwrap(
            AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: 48_000,
                channels: 1,
                interleaved: false
            )
        )
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 2))
        buffer.frameLength = 2
        let channel = try XCTUnwrap(buffer.floatChannelData?[0])
        channel[0] = 0.25
        channel[1] = -0.75

        let sample = try XCTUnwrap(AudioLevelMath.measure(buffer: buffer))

        XCTAssertEqual(sample.rms, Float(sqrt(0.3125)), accuracy: 0.0001)
        XCTAssertEqual(sample.peak, 0.75, accuracy: 0.0001)
    }

    func testSmootherUsesFastAttackAndGentleRelease() {
        var smoother = AudioLevelSmoother(attack: 0.5, release: 0.25)

        XCTAssertEqual(smoother.update(AudioLevelSample(rms: 1, peak: 1)), .init(rms: 0.5, peak: 0.5))
        XCTAssertEqual(smoother.update(.zero), .init(rms: 0.375, peak: 0.375))
    }

    func testSmootherClampsInvalidTargets() {
        var smoother = AudioLevelSmoother(attack: 1, release: 1)

        XCTAssertEqual(smoother.update(AudioLevelSample(rms: 2, peak: -.infinity)), .init(rms: 1, peak: 0))
        smoother.reset()
        XCTAssertEqual(smoother.current, .zero)
    }

    func testNoDataPolicyDoesNotConfuseSilenceWithMissingBuffers() {
        XCTAssertTrue(AudioLevelMonitorNoDataPolicy.shouldPublishNoData(hasReceivedSample: false))
        XCTAssertFalse(AudioLevelMonitorNoDataPolicy.shouldPublishNoData(hasReceivedSample: true))
    }

    func testPermissionPolicyExposesUndeterminedAndDeniedStates() {
        XCTAssertEqual(
            AudioLevelMonitorPermissionPolicy.error(for: .notDetermined),
            .permissionRequired
        )
        XCTAssertEqual(
            AudioLevelMonitorPermissionPolicy.state(for: .notDetermined),
            .permissionRequired
        )
        XCTAssertEqual(
            AudioLevelMonitorPermissionPolicy.error(for: .denied),
            .permissionDenied
        )
        XCTAssertNil(AudioLevelMonitorPermissionPolicy.error(for: .authorized))
    }

    func testInputLeaseRejectsASecondAudioPathEvenForAnotherDevice() throws {
        let first = try AudioInputLeaseCoordinator.acquire(
            deviceUID: "test-lease-\(UUID().uuidString)"
        )
        defer { first.release() }

        XCTAssertThrowsError(
            try AudioInputLeaseCoordinator.acquire(
                deviceUID: "test-other-\(UUID().uuidString)"
            )
        ) { error in
            guard case .deviceBusy = error as? AudioLevelMonitorError else {
                return XCTFail("expected process-wide input lease failure, got \(error)")
            }
        }
    }

    func testLevelBarExposesAStableControlSizeAndPermissionState() {
        let view = AudioLevelBarView()

        XCTAssertEqual(view.intrinsicContentSize, NSSize(width: 220, height: 18))
        view.setState(.permissionDenied)
        XCTAssertEqual(view.state, .permissionDenied)
        view.setLevel(AudioLevelSample(rms: 0.4, peak: 0.8))
        XCTAssertEqual(view.sample.peak, 0.8, accuracy: 0.0001)
    }
}
