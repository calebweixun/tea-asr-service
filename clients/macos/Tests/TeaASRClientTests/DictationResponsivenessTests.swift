import XCTest
@testable import TeaASRClient

/// The three user-reported symptoms this file exists for:
///
/// 1. "按下去與放開都要等一下才有反應" — the overlay used to be shown only
///    after the whole start path (microphone consent, the process-wide input
///    lease, the audio unit) had completed.
/// 2. "切換模式下只有第一句話會出現浮動泡泡" — a continuous toggle session
///    kept running after the overlay auto-hid, so it stopped reflecting the
///    session at all.
/// 3. "按了沒反應" — a press latch that a missing key-release could leave
///    stuck forever.
@MainActor
final class DictationResponsivenessTests: XCTestCase {

    // MARK: - 1. Feedback does not wait for the audio path

    /// The state entered on key-down must already be on screen, and must not
    /// be `.listening`: claiming the microphone is live before the lease and
    /// the audio unit have been acquired would be a lie the user would catch.
    func testShortcutPressProducesVisibleFeedbackBeforeAudioIsRunning() {
        let starting = DictationOverlayPolicy.transition(.hidden, event: .startRequested)
        XCTAssertEqual(starting, .starting)
        XCTAssertTrue(starting.isVisible)
        XCTAssertNotEqual(starting, .listening)
    }

    /// The controller-level version of the same property: nothing but
    /// `startRequested()` is called — no permission callback, no capture, no
    /// server session — and the overlay is already showing.
    func testOverlayControllerShowsStartingWithoutAnyAudioPathProgress() {
        let overlay = DictationOverlayController()
        XCTAssertEqual(overlay.debugState, .hidden)

        overlay.startRequested()

        XCTAssertEqual(overlay.debugState, .starting)
        XCTAssertTrue(overlay.debugState.isVisible)
        XCTAssertTrue(overlay.debugSessionIsActive)
    }

    /// `.starting` is a stage, not a destination: once capture really runs,
    /// `begin()` must move it on to the honest "listening" text.
    func testStartingAdvancesToListeningOnlyWhenCaptureActuallyStarted() {
        let overlay = DictationOverlayController()
        overlay.startRequested()
        overlay.begin()
        XCTAssertEqual(overlay.debugState, .listening)
    }

    /// A start that never became a session (microphone denied, device busy,
    /// push-to-talk released during the consent prompt) must take the
    /// `.starting` overlay back down rather than leave it claiming a session.
    func testFailedStartReplacesStartingInsteadOfLeavingItOnScreen() {
        let overlay = DictationOverlayController()
        overlay.startRequested()
        overlay.showError("沒有麥克風權限")
        XCTAssertEqual(overlay.debugState, .error("沒有麥克風權限"))
        XCTAssertFalse(overlay.debugSessionIsActive)

        let cancelled = DictationOverlayController()
        cancelled.startRequested()
        cancelled.hide()
        XCTAssertEqual(cancelled.debugState, .hidden)
        XCTAssertFalse(cancelled.debugState.isVisible)
    }

    /// Releasing the key must change the overlay immediately, not after the
    /// server's last final has come back.
    func testStopRequestIsVisibleBeforeTheFinalTranscriptArrives() {
        let overlay = DictationOverlayController()
        overlay.startRequested()
        overlay.begin()
        overlay.stopRequested()
        XCTAssertEqual(overlay.debugState, .stopping)
        XCTAssertTrue(overlay.debugState.isVisible)
        XCTAssertFalse(overlay.debugSessionIsActive)
    }

    // MARK: - 2. Every utterance of a continuous session is shown

    func testAutoDismissKeepsTheOverlayWhileTheSessionIsStillRunning() {
        XCTAssertEqual(
            DictationOverlayAutoDismissPolicy.outcome(sessionIsActive: true),
            .returnToListening
        )
        XCTAssertEqual(
            DictationOverlayAutoDismissPolicy.outcome(sessionIsActive: false),
            .hide
        )
    }

    /// Toggle mode: one session, several utterances. Every one of them must
    /// produce a visible overlay, and the gaps between them must still show
    /// that the session is live.
    func testEverySentenceOfAToggleSessionIsShown() {
        let overlay = DictationOverlayController()
        overlay.startRequested()
        overlay.begin()

        for sentence in ["第一句", "第二句", "第三句"] {
            overlay.showPartial(sentence + "…")
            XCTAssertEqual(overlay.debugState, .partial(sentence + "…"))

            overlay.showFinal(sentence)
            XCTAssertEqual(overlay.debugState, .final(sentence))

            // The utterance's dismissal timer fires while the toggle session
            // is still open: it must fall back to "listening", never hide.
            overlay.debugApplyAutoDismiss()
            XCTAssertEqual(
                overlay.debugState,
                .listening,
                "a still-running session must keep visible feedback after \(sentence)"
            )
            XCTAssertTrue(overlay.debugState.isVisible)
        }
    }

    /// The same is true of a clipboard fallback in the middle of a session.
    func testClipboardFallbackMidSessionAlsoReturnsToListening() {
        let overlay = DictationOverlayController()
        overlay.startRequested()
        overlay.begin()
        overlay.showCopiedToClipboard("你好", reason: "缺少輔助使用權限")
        XCTAssertEqual(overlay.debugState, .copiedToClipboard(text: "你好", reason: "缺少輔助使用權限"))
        overlay.debugApplyAutoDismiss()
        XCTAssertEqual(overlay.debugState, .listening)
    }

    /// Once the session really ended, the timer must hide the overlay — the
    /// continuous-session behaviour above must not make it sticky.
    func testTheLastFinalOfAnEndedSessionStillHides() {
        let overlay = DictationOverlayController()
        overlay.startRequested()
        overlay.begin()
        overlay.stopRequested()
        overlay.showFinal("最後一句")
        overlay.debugApplyAutoDismiss()
        XCTAssertEqual(overlay.debugState, .hidden)

        let ended = DictationOverlayController()
        ended.startRequested()
        ended.begin()
        ended.showFinal("一句")
        ended.stopped()
        XCTAssertEqual(ended.debugState, .hidden)
        ended.debugApplyAutoDismiss()
        XCTAssertEqual(ended.debugState, .hidden)
    }

    // MARK: - 3. The press latch is released on every ending path

    /// `sessionEnded`, the cancellation family, an explicit latch release,
    /// and both reset entry points must all leave `shortcutIsDown` false, and
    /// a later key-down must be honoured rather than swallowed as a repeat.
    func testShortcutLatchIsClearedOnEveryEndingPath() {
        let endings: [(String, (inout DictationInteractionStateMachine) -> Void)] = [
            ("sessionEnded", { _ = $0.handle(.sessionEnded) }),
            ("cancelled", { _ = $0.handle(.cancelled) }),
            ("focusLost", { _ = $0.handle(.focusLost) }),
            ("systemSleep", { _ = $0.handle(.systemSleep) }),
            ("permissionLost", { _ = $0.handle(.permissionLost) }),
            ("shortcutUp", { _ = $0.handle(.shortcutUp) }),
            ("releaseShortcutLatch", { $0.releaseShortcutLatch() }),
            ("resetAfterStartFailure", { $0.resetAfterStartFailure() }),
            ("resetAfterSessionEnd", { $0.resetAfterSessionEnd() }),
            ("beginManualSession", { $0.beginManualSession() }),
        ]

        for mode in [DictationInteractionMode.toggle, .pushToTalk] {
            for (name, ending) in endings {
                var machine = DictationInteractionStateMachine(mode: mode)
                XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [.start])
                XCTAssertTrue(machine.shortcutIsDown)

                ending(&machine)

                XCTAssertFalse(
                    machine.shortcutIsDown,
                    "\(mode) / \(name) left the press latch stuck"
                )
            }
        }
    }

    /// The symptom the latch leak produced: the hot key is re-registered
    /// while the user's finger is down, the matching `kEventHotKeyReleased`
    /// dies with the old registration, and every later press is read as a
    /// duplicate. After `releaseShortcutLatch()` the next press must work.
    func testPressAfterAHotKeyReRegistrationIsStillHonoured() {
        var toggle = DictationInteractionStateMachine(mode: .toggle)
        XCTAssertEqual(toggle.handle(.shortcutDown(isRepeat: false)), [.start])
        // The release for this press never arrives.
        toggle.releaseShortcutLatch()
        XCTAssertEqual(
            toggle.handle(.shortcutDown(isRepeat: false)),
            [.stop],
            "a toggle press after a lost key-release must not be swallowed"
        )

        var ptt = DictationInteractionStateMachine(mode: .pushToTalk)
        XCTAssertEqual(ptt.handle(.shortcutDown(isRepeat: false)), [.start])
        ptt.releaseShortcutLatch()
        // The physical key is still down; its real release must still stop
        // the session even though the latch was dropped underneath it.
        XCTAssertEqual(ptt.handle(.shortcutUp), [.stop])
        XCTAssertFalse(ptt.active)
        XCTAssertEqual(ptt.handle(.shortcutDown(isRepeat: false)), [.start])
    }

    /// Releasing the latch must not be mistaken for ending the session: a
    /// push-to-talk session that is still recording stays active.
    func testReleasingTheLatchDoesNotEndAnActiveSession() {
        var machine = DictationInteractionStateMachine(mode: .pushToTalk)
        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [.start])
        machine.releaseShortcutLatch()
        XCTAssertTrue(machine.active)
    }
}
