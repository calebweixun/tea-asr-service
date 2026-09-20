import AppKit
import XCTest
@testable import TeaASRClient

/// The reported failure was a session that kept saying "listening" long after
/// it had stopped working: the user talked and nothing happened, with no error
/// anywhere, until they stopped and started again. Two separate guarantees are
/// needed, and these tests pin both of them:
///
/// 1. a *silent* session must stay connected, and
/// 2. a session that dies anyway must leave `.listening` immediately.
final class SessionLivenessTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_000)

    private func advanced(_ seconds: TimeInterval) -> Date {
        start.addingTimeInterval(seconds)
    }

    // MARK: - (1) surviving silence

    func testAnIdleButHealthySessionIsNeverReportedAsStalled() {
        var watchdog = SessionWatchdog()
        watchdog.arm(at: start)

        // Nobody speaks for five minutes. Frames still arrive every 100 ms and
        // the service still acknowledges them, which is exactly what a quiet
        // room looks like.
        var second = 1.0
        while second <= 300 {
            let now = advanced(second)
            watchdog.noteFrame(at: now)
            watchdog.noteServerEvent(at: now)
            if case .fail = watchdog.tick(now: now) {
                XCTFail("a healthy silent session was reported as stalled at \(second)s")
                return
            }
            second += 1
        }
    }

    func testKeepalivesAreEmittedOftenEnoughToOutrunTheServiceIdleTimeout() {
        var watchdog = SessionWatchdog()
        watchdog.arm(at: start)

        var keepaliveTimes: [TimeInterval] = []
        var second = 1.0
        while second <= 120 {
            let now = advanced(second)
            watchdog.noteFrame(at: now)
            watchdog.noteServerEvent(at: now)
            if case .keepalive = watchdog.tick(now: now) {
                keepaliveTimes.append(second)
            }
            second += 1
        }

        XCTAssertFalse(keepaliveTimes.isEmpty)
        // The service drops a connection that carries nothing for 120 s.
        // Every gap between keepalives has to stay well inside that.
        var previous = 0.0
        for time in keepaliveTimes {
            XCTAssertLessThanOrEqual(time - previous, 12)
            previous = time
        }
        XCTAssertLessThanOrEqual(120 - previous, 12)
    }

    // MARK: - (2) failing visibly when it does break

    func testASocketThatWentQuietIsReportedRatherThanLeftListening() {
        var watchdog = SessionWatchdog()
        watchdog.arm(at: start)

        // The microphone is fine — frames keep coming — but nothing has
        // arrived from the service. A live session gets an ack per frame, so
        // this can only mean the connection is dead.
        var action = SessionWatchdog.Action.none
        var second = 1.0
        while second <= 30 {
            let now = advanced(second)
            watchdog.noteFrame(at: now)
            action = watchdog.tick(now: now)
            if case .fail = action { break }
            second += 1
        }

        guard case .fail(let stall) = action else {
            return XCTFail("a dead socket was never reported")
        }
        guard case .serverSilent = stall else {
            return XCTFail("expected a server-silence stall, got \(stall)")
        }
        XCTAssertLessThanOrEqual(second, SessionWatchdog.serverSilenceTimeout + 1)
        XCTAssertTrue(stall.issue.retryable)
        XCTAssertEqual(stall.issue.code, "connection_stalled")
    }

    func testAMicrophoneThatStoppedDeliveringIsReportedWithinSeconds() {
        var watchdog = SessionWatchdog()
        watchdog.arm(at: start)

        var action = SessionWatchdog.Action.none
        var second = 1.0
        while second <= 30 {
            let now = advanced(second)
            watchdog.noteServerEvent(at: now)
            action = watchdog.tick(now: now)
            if case .fail = action { break }
            second += 1
        }

        guard case .fail(let stall) = action, case .audioStopped = stall else {
            return XCTFail("a stalled capture was never reported, got \(action)")
        }
        // Fast enough that the user sees the overlay change instead of
        // talking into a session that is not listening.
        XCTAssertLessThanOrEqual(second, 5)
        XCTAssertEqual(stall.issue.code, "audio_stalled")
    }

    func testADisarmedWatchdogNeverFires() {
        var watchdog = SessionWatchdog()
        watchdog.arm(at: start)
        watchdog.disarm()

        XCTAssertEqual(watchdog.tick(now: advanced(600)), SessionWatchdog.Action.none)
        // A late frame from a session that already ended must not re-arm it.
        watchdog.noteFrame(at: advanced(601))
        XCTAssertNil(watchdog.lastFrameAt)
    }

    /// A deliberate stop is not a stall: the caller shuts the microphone down
    /// first and the service may spend a while draining the last segment.
    /// `ASRClient.stop()` disarms for exactly that window, and the disarmed
    /// watchdog has to stay quiet however long the drain takes.
    func testADrainingStopIsNeverMistakenForAStall() {
        var watchdog = SessionWatchdog()
        watchdog.arm(at: start)
        watchdog.disarm()

        var second = 1.0
        while second <= 120 {
            XCTAssertEqual(watchdog.tick(now: advanced(second)), SessionWatchdog.Action.none)
            second += 1
        }
    }

    func testTheServerIsBlamedFirstWhenBothPathsStall() {
        var watchdog = SessionWatchdog()
        watchdog.arm(at: start)

        guard case .fail(let stall) = watchdog.tick(now: advanced(60)) else {
            return XCTFail("a fully stalled session was not reported")
        }
        guard case .serverSilent = stall else {
            return XCTFail("expected the connection to be blamed, got \(stall)")
        }
    }

    // MARK: - what the user actually sees

    func testAStalledSessionStopsShowingListeningImmediately() {
        let appState = AppState()
        appState.setMode(.dictation)
        appState.updateClientState(.listening(protocolVersion: "1.1", preview: true))

        guard case .listening = appState.displayStatus else {
            return XCTFail("a live session should read as listening")
        }

        appState.updateClientState(.failed(SessionStall.serverSilent(seconds: 21).issue))

        switch appState.displayStatus {
        case .listening:
            XCTFail("a stalled session must not keep claiming it is listening")
        case .retryable(let issue):
            XCTAssertEqual(issue.code, "connection_stalled")
            XCTAssertTrue(issue.message.contains("連線已中斷"))
        default:
            XCTFail("expected the stall to surface as a retryable failure")
        }
    }

    func testAStalledCaptureAlsoStopsShowingListening() {
        let appState = AppState()
        appState.setMode(.dictation)
        appState.updateClientState(.listening(protocolVersion: "1.0", preview: false))
        appState.updateClientState(.failed(SessionStall.audioStopped(seconds: 5).issue))

        if case .listening = appState.displayStatus {
            XCTFail("a stalled capture must not keep claiming it is listening")
        }
    }
}
