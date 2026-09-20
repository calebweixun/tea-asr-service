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
        XCTAssertEqual(smoother.attack, 0.65, accuracy: 0.0001)
        XCTAssertEqual(smoother.release, 0.18, accuracy: 0.0001)
        XCTAssertGreaterThan(smoother.attack, smoother.release)
    }

    func testSmoothingInDisplayUnitsReachesSpeechLevelWithinAFewBuffers() {
        // Smoothing now runs on the dBFS display value, so a speech onset is
        // visible after two ~43 ms buffers instead of crawling up from a tiny
        // linear amplitude.
        var smoother = AudioLevelSmoother()
        let speech = AudioLevelScale.display(for: AudioLevelSample(rms: 0.05, peak: 0.2))

        _ = smoother.update(speech)
        let afterTwo = smoother.update(speech)

        XCTAssertGreaterThan(afterTwo.rms, speech.rms * 0.85)
        XCTAssertLessThanOrEqual(afterTwo.rms, speech.rms)
    }

    func testSmootherDecaysTowardsSilenceWithoutOvershooting() {
        var smoother = AudioLevelSmoother(attack: 1, release: 0.18)
        _ = smoother.update(AudioLevelSample(rms: 1, peak: 1))

        var value = smoother.current.rms
        for _ in 0..<20 {
            value = smoother.update(.zero).rms
            XCTAssertGreaterThanOrEqual(value, 0)
        }
        // ~20 buffers is under a second at the ~23 Hz tap rate.
        XCTAssertLessThan(value, 0.05)
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
        XCTAssertEqual(AudioLevelUpdatePolicy.minimumInterval, 1.0 / 30.0, accuracy: 0.0001)
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
                pending: AudioLevelSample(rms: 0.2001, peak: 0.3001),
                lastDelivered: AudioLevelSample(rms: 0.2, peak: 0.3),
                elapsed: 0.1
            )
        )
    }

    func testAFrozenValueIsStillRefreshedSoTheBarSettles() {
        XCTAssertTrue(
            AudioLevelUpdatePolicy.shouldDeliver(
                pending: AudioLevelSample(rms: 0.2001, peak: 0.3001),
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
