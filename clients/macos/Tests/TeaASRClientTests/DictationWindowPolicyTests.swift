import AppKit
import XCTest
@testable import TeaASRClient

/// `hideForDictation()` no longer hides the management window, so the safety
/// property it used to provide has to hold on its own: a final must never be
/// pasted into TEA ASR itself, even when a TEA ASR window is visible and
/// focused. These tests pin the two mechanisms that actually provide it.
final class DictationWindowPolicyTests: XCTestCase {
    private let ourProcess = NSRunningApplication.current.processIdentifier

    /// `AppController.start` captures the target with
    /// `captureFocusedTarget(excluding: <our pid>)`, which returns nil when
    /// TEA ASR owns focus. With no captured target there is nothing to insert
    /// into, so the final goes to the clipboard.
    func testNoTargetIsRecordedWhenTeaASROwnsFocusAtDictationStart() {
        let captured = TextInjector.captureFocusedTarget(excluding: ourProcess)
        XCTAssertNotEqual(captured?.processIdentifier, ourProcess)
        XCTAssertFalse(
            TextInsertionTargetPolicy.allowsInsertion(
                captured: nil,
                current: TextInsertionTarget(
                    processIdentifier: ourProcess,
                    focusedElementIdentifier: 1
                )
            )
        )
    }

    /// And if the user clicks into a visible TEA ASR window *during* dictation,
    /// the pid check at paste time refuses as well: the captured target is
    /// always some other app, so insertion into ourselves cannot be reached.
    func testAFinalIsNotPastedIntoTeaASRWhenItsWindowStaysVisible() {
        let editorInAnotherApp = TextInsertionTarget(
            processIdentifier: ourProcess == 4_242 ? 4_243 : 4_242,
            focusedElementIdentifier: 99,
            accessibilityIdentifier: "editor.main"
        )
        let teaASRField = TextInsertionTarget(
            processIdentifier: ourProcess,
            focusedElementIdentifier: 99,
            accessibilityIdentifier: "editor.main"
        )

        XCTAssertNotEqual(editorInAnotherApp.processIdentifier, ourProcess)
        XCTAssertFalse(
            TextInsertionTargetPolicy.allowsInsertion(
                captured: editorInAnotherApp,
                current: teaASRField
            ),
            "matching element identifiers must not defeat the process check"
        )
        XCTAssertTrue(
            TextInsertionTargetPolicy.allowsInsertion(
                captured: editorInAnotherApp,
                current: editorInAnotherApp
            ),
            "the real target must still receive the final"
        )
    }

    /// The second half of the report: repeating the shortcut made the app stop
    /// responding. `mode` is only assigned after the asynchronous consent
    /// callback, so without this gate every press inside that window queued
    /// another `capture.start` against the same audio engine.
    func testRepeatedStartRequestsAreAdmittedOnce() {
        var gate = SessionStartGate()

        XCTAssertTrue(gate.begin())
        XCTAssertFalse(gate.begin())
        XCTAssertFalse(gate.begin())
        XCTAssertTrue(gate.isStarting)

        gate.finish()
        XCTAssertFalse(gate.isStarting)
        XCTAssertTrue(gate.begin(), "a later press must still be able to start a session")
    }

    func testAFailedStartReopensTheGate() {
        var gate = SessionStartGate()
        XCTAssertTrue(gate.begin())
        gate.finish()
        XCTAssertTrue(gate.begin())
    }
}
