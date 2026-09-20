import AppKit
import XCTest
@testable import TeaASRClient

/// Covers the input-level bar's display scale and its update budget.
final class AudioLevelScaleTests: XCTestCase {
    // MARK: - dBFS mapping

    func testSilenceAndFullScaleMapToTheEndsOfTheBar() {
        XCTAssertEqual(AudioLevelScale.normalized(amplitude: 0), 0, accuracy: 0.0001)
        XCTAssertEqual(AudioLevelScale.normalized(amplitude: 1), 1, accuracy: 0.0001)
        XCTAssertEqual(AudioLevelScale.normalized(amplitude: -0.5), 0, accuracy: 0.0001)
        XCTAssertEqual(AudioLevelScale.normalized(amplitude: .nan), 0, accuracy: 0.0001)
        XCTAssertEqual(AudioLevelScale.normalized(amplitude: 4), 1, accuracy: 0.0001)
    }

    func testTheFloorIsMinus60dBFSAndAnythingQuieterIsEmpty() {
        // 0.001 == -60 dBFS, exactly the floor.
        XCTAssertEqual(AudioLevelScale.normalized(amplitude: 0.001), 0, accuracy: 0.0005)
        XCTAssertEqual(AudioLevelScale.normalized(amplitude: 0.0001), 0, accuracy: 0.0001)
        XCTAssertEqual(AudioLevelScale.floorDB, -60, accuracy: 0.0001)
    }

    func testDecibelMarksLandAtTheirProportionalPositions() {
        // -30 dBFS is half way up a -60…0 dBFS scale, -6 dBFS is 90%.
        XCTAssertEqual(
            AudioLevelScale.normalized(amplitude: 0.0316228),
            0.5,
            accuracy: 0.001
        )
        XCTAssertEqual(
            AudioLevelScale.normalized(amplitude: 0.501187),
            0.9,
            accuracy: 0.001
        )
    }

    func testOrdinarySpeechFillsAUsefulPartOfTheBarUnlikeTheLinearScale() {
        // The reported bug: a 0.05 RMS (about -26 dBFS) conversational level
        // filled 5% of the bar because the linear amplitude was used directly.
        let linearFill: Float = 0.05
        let scaledFill = AudioLevelScale.normalized(amplitude: 0.05)

        XCTAssertLessThan(linearFill, 0.1)
        XCTAssertGreaterThan(scaledFill, 0.5)
        XCTAssertLessThan(scaledFill, 0.7)
    }

    func testDisplayConversionScalesRMSAndPeakTogether() {
        let display = AudioLevelScale.display(
            for: AudioLevelSample(rms: 0.0316228, peak: 1)
        )
        XCTAssertEqual(display.rms, 0.5, accuracy: 0.001)
        XCTAssertEqual(display.peak, 1, accuracy: 0.001)
    }

    // MARK: - Smoothing

    func testSmootherDefaultsStayFastOnAttackAndGentleOnRelease() {
        let smoother = AudioLevelSmoother()
        XCTAssertEqual(smoother.attackSeconds, 0.012, accuracy: 0.0001)
        XCTAssertEqual(smoother.releaseSeconds, 0.220, accuracy: 0.0001)
        XCTAssertLessThan(smoother.attackSeconds, smoother.releaseSeconds)
    }

    /// The reported symptom was latency, and this is the number behind it: at
    /// the tap's ~10.7 ms buffer the bar must be essentially at the new level
    /// within about three buffers, not a tenth of a second.
    func testSpeechOnsetIsAlmostCompleteWithinFortyMilliseconds() {
        var smoother = AudioLevelSmoother()
        let speech = AudioLevelScale.display(for: AudioLevelSample(rms: 0.05, peak: 0.2))
        let buffer = 512.0 / 48_000.0

        var value: Float = 0
        var elapsed = 0.0
        while elapsed < 0.04 {
            value = smoother.update(speech, interval: buffer).rms
            elapsed += buffer
        }

        XCTAssertGreaterThan(value, speech.rms * 0.95)
        XCTAssertLessThanOrEqual(value, speech.rms)
    }

    /// The whole point of expressing attack as a time constant: the filter
    /// must not become sluggish because the HAL handed over a different
    /// buffer size than expected.
    func testAttackTakesTheSameWallTimeAtAnyBufferSize() {
        func riseAfter(_ seconds: TimeInterval, buffer: TimeInterval) -> Float {
            var smoother = AudioLevelSmoother()
            var elapsed = 0.0
            var value: Float = 0
            while elapsed + buffer <= seconds + 1e-9 {
                value = smoother.update(AudioLevelSample(rms: 1, peak: 1), interval: buffer).rms
                elapsed += buffer
            }
            return value
        }

        let small = riseAfter(0.048, buffer: 0.002)
        let large = riseAfter(0.048, buffer: 0.012)
        XCTAssertEqual(small, large, accuracy: 0.02)
        XCTAssertGreaterThan(small, 0.95)
    }

    func testSmootherDecaysTowardsSilenceWithoutOvershooting() {
        var smoother = AudioLevelSmoother()
        let buffer = 512.0 / 48_000.0
        _ = smoother.update(AudioLevelSample(rms: 1, peak: 1), interval: 0.1)

        var value = smoother.current.rms
        var elapsed = 0.0
        while elapsed < 0.7 {
            value = smoother.update(.zero, interval: buffer).rms
            XCTAssertGreaterThanOrEqual(value, 0)
            elapsed += buffer
        }
        // ~3 release time constants: the bar has visibly settled inside a
        // second, without dropping out between syllables on the way there.
        XCTAssertLessThan(value, 0.06)
    }

    func testReleaseIsSlowEnoughToSurviveOneQuietBuffer() {
        var smoother = AudioLevelSmoother()
        let buffer = 512.0 / 48_000.0
        _ = smoother.update(AudioLevelSample(rms: 1, peak: 1), interval: 0.1)

        let afterOneQuietBuffer = smoother.update(.zero, interval: buffer).rms
        XCTAssertGreaterThan(afterOneQuietBuffer, 0.9)
    }

    // MARK: - Update throttling

    func testFirstSampleIsAlwaysDelivered() {
        XCTAssertTrue(
            AudioLevelUpdatePolicy.shouldDeliver(
                pending: .zero,
                lastDelivered: nil,
                elapsed: 0
            )
        )
    }

    func testUpdatesFasterThanThirtyHertzAreDropped() {
        XCTAssertFalse(
            AudioLevelUpdatePolicy.shouldDeliver(
                pending: AudioLevelSample(rms: 0.9, peak: 0.9),
                lastDelivered: AudioLevelSample(rms: 0.1, peak: 0.1),
                elapsed: 0.01
            )
        )
        XCTAssertEqual(AudioLevelUpdatePolicy.minimumInterval, 1.0 / 60.0, accuracy: 0.0001)
    }

    /// The bar's real frame rate is set by the tap buffer, not by the cap.
    /// A 2,048-frame buffer at 48 kHz could never beat ~23 Hz however
    /// generous the throttle was, which is what "low frame rate" meant.
    func testEffectiveUpdateRateIsSetByTheTapBufferNotTheCap() {
        let oldRate = AudioLevelUpdatePolicy.effectiveRate(
            sourceInterval: 2_048.0 / 48_000.0,
            minimumInterval: 1.0 / 30.0
        )
        XCTAssertEqual(oldRate, 23.4, accuracy: 0.2)

        let newRate = AudioLevelUpdatePolicy.effectiveRate(
            sourceInterval: 512.0 / 48_000.0
        )
        XCTAssertEqual(newRate, 46.9, accuracy: 0.2)
        XCTAssertGreaterThan(newRate, oldRate * 1.5)
    }

    func testEffectiveRateNeverExceedsTheSourceRate() {
        XCTAssertEqual(
            AudioLevelUpdatePolicy.effectiveRate(
                sourceInterval: 0.1,
                minimumInterval: 1.0 / 60.0
            ),
            10,
            accuracy: 0.0001
        )
        XCTAssertEqual(AudioLevelUpdatePolicy.effectiveRate(sourceInterval: 0), 0)
    }

    func testAVisibleChangeIsDeliveredOnceTheIntervalHasPassed() {
        XCTAssertTrue(
            AudioLevelUpdatePolicy.shouldDeliver(
                pending: AudioLevelSample(rms: 0.5, peak: 0.5),
                lastDelivered: AudioLevelSample(rms: 0.1, peak: 0.1),
                elapsed: 0.04
            )
        )
    }

    func testSubPixelChangesDoNotCauseARedraw() {
        // A silent settings page must not repaint the bar at all.
        XCTAssertFalse(
            AudioLevelUpdatePolicy.shouldDeliver(
                pending: AudioLevelSample(rms: 0.20005, peak: 0.30005),
                lastDelivered: AudioLevelSample(rms: 0.2, peak: 0.3),
                elapsed: 0.1
            )
        )
    }

    func testAFrozenValueIsStillRefreshedSoTheBarSettles() {
        XCTAssertTrue(
            AudioLevelUpdatePolicy.shouldDeliver(
                pending: AudioLevelSample(rms: 0.20005, peak: 0.30005),
                lastDelivered: AudioLevelSample(rms: 0.2, peak: 0.3),
                elapsed: AudioLevelUpdatePolicy.idleRefreshInterval
            )
        )
    }

    // MARK: - Redraw scope

    func testOnlyTheMovingPartOfTheBarIsInvalidated() {
        let track = NSRect(x: 1, y: 3, width: 218, height: 12)
        let dirty = AudioLevelBarView.invalidationRect(
            from: AudioLevelSample(rms: 0.5, peak: 0.5),
            to: AudioLevelSample(rms: 0.52, peak: 0.52),
            in: track
        )

        XCTAssertLessThan(dirty.width, track.width / 4)
        XCTAssertTrue(track.contains(dirty))
        XCTAssertEqual(dirty.height, track.height, accuracy: 0.0001)
    }

    func testInvalidationCoversBothTheOldAndTheNewFill() {
        let track = NSRect(x: 0, y: 0, width: 200, height: 12)
        let dirty = AudioLevelBarView.invalidationRect(
            from: AudioLevelSample(rms: 0.1, peak: 0.1),
            to: AudioLevelSample(rms: 0.8, peak: 0.9),
            in: track
        )

        XCTAssertLessThanOrEqual(dirty.minX, 20)
        XCTAssertGreaterThanOrEqual(dirty.maxX, 180)
    }

    func testIdenticalLevelsDoNotMarkTheViewDirty() {
        let view = AudioLevelBarView()
        view.setLevel(AudioLevelSample(rms: 0.4, peak: 0.6))
        view.needsDisplay = false
        view.setLevel(AudioLevelSample(rms: 0.4, peak: 0.6))

        XCTAssertFalse(view.needsDisplay)
    }
}
