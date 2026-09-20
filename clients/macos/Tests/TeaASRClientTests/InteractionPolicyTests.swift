import XCTest
import Carbon.HIToolbox
@testable import TeaASRClient

final class InteractionPolicyTests: XCTestCase {
    func testToggleStartsAndStopsWithoutTreatingRepeatAsSecondToggle() {
        var machine = DictationInteractionStateMachine(mode: .toggle)

        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [.start])
        XCTAssertTrue(machine.active)
        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: true)), [])
        XCTAssertTrue(machine.active)
        // Carbon may deliver another callback without marking it as a
        // repeat. The physical latch still suppresses it until release.
        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [])
        XCTAssertTrue(machine.shortcutIsDown)
        XCTAssertEqual(machine.handle(.shortcutUp), [])
        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [.stop])
        XCTAssertFalse(machine.active)
    }

    func testPushToTalkStartsOnFirstDownAndStopsOnMatchingUp() {
        var machine = DictationInteractionStateMachine(mode: .pushToTalk)

        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [.start])
        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: true)), [])
        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [])
        XCTAssertTrue(machine.active)
        XCTAssertEqual(machine.handle(.shortcutUp), [.stop])
        XCTAssertFalse(machine.active)
        XCTAssertEqual(machine.handle(.shortcutUp), [])
    }

    func testCancellationAndPermissionLossAlwaysReleasePTTState() {
        for event in [
            DictationInteractionEvent.cancelled,
            .focusLost,
            .systemSleep,
            .permissionLost,
        ] {
            var machine = DictationInteractionStateMachine(mode: .pushToTalk)
            XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [.start])
            XCTAssertEqual(machine.handle(event), [.stop])
            XCTAssertFalse(machine.active)
            XCTAssertFalse(machine.shortcutIsDown)
            XCTAssertEqual(machine.handle(.shortcutUp), [])
        }
    }

    func testSessionEndClearsPressedStateWithoutEmittingSecondStop() {
        var machine = DictationInteractionStateMachine(mode: .pushToTalk)
        _ = machine.handle(.shortcutDown(isRepeat: false))
        XCTAssertEqual(machine.handle(.sessionEnded), [])
        XCTAssertEqual(machine.handle(.sessionEnded), [])
        XCTAssertFalse(machine.active)
        XCTAssertFalse(machine.shortcutIsDown)
    }

    func testFeedbackIsOptInAndErrorsStaySilent() {
        XCTAssertFalse(InteractionFeedbackPolicy.shouldPlay(enabled: false, event: .started))
        XCTAssertTrue(InteractionFeedbackPolicy.shouldPlay(enabled: true, event: .started))
        XCTAssertTrue(InteractionFeedbackPolicy.shouldPlay(enabled: true, event: .stopped))
        XCTAssertTrue(InteractionFeedbackPolicy.shouldPlay(enabled: true, event: .ignoredDuringMeeting))
        XCTAssertFalse(InteractionFeedbackPolicy.shouldPlay(enabled: true, event: .failed))
    }

    func testShortcutWhileMeetingProducesOnlyTheSafeIgnoreCommand() {
        var machine = DictationInteractionStateMachine(mode: .toggle)

        XCTAssertEqual(machine.handle(.shortcutWhileMeeting), [.ignoredDuringMeeting])
        XCTAssertFalse(machine.active)
        XCTAssertFalse(machine.shortcutIsDown)
    }

    func testExclusiveRegistrationDistinguishesConflictFromOtherFailures() {
        XCTAssertEqual(
            ShortcutRegistrationPolicy.outcome(for: noErr),
            .registered
        )
        XCTAssertEqual(
            ShortcutRegistrationPolicy.outcome(for: OSStatus(eventHotKeyExistsErr)),
            .conflict
        )
        XCTAssertEqual(
            ShortcutRegistrationPolicy.outcome(for: -1),
            .unavailable
        )
    }

    func testMissingInputMonitorsAreRetriedIndependently() {
        XCTAssertEqual(
            InputMonitorInstallPolicy.missing(globalInstalled: false, localInstalled: false),
            [.globalKeyUp, .localKeyUp]
        )
        XCTAssertEqual(
            InputMonitorInstallPolicy.missing(globalInstalled: false, localInstalled: true),
            [.globalKeyUp]
        )
        XCTAssertEqual(
            InputMonitorInstallPolicy.missing(globalInstalled: true, localInstalled: false),
            [.localKeyUp]
        )
        XCTAssertTrue(InputMonitorInstallPolicy.missing(globalInstalled: true, localInstalled: true).isEmpty)
    }

    func testShortcutEditorDoesNotCaptureKeysWhileModalIsClosed() {
        var editor = ShortcutEditorSession(original: .default)

        XCTAssertEqual(
            editor.capture(keyCode: 14, modifiers: [.command]),
            .ignoredWhileClosed
        )
        XCTAssertFalse(editor.isModalOpen)
        XCTAssertEqual(editor.candidate, .default)
    }

    func testShortcutEditorCancelLeavesPersistedSettingUnchanged() throws {
        let suiteName = "TeaASRClientTests.ShortcutEditor.Cancel.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let settings = Settings(defaults: defaults)
        let original = settings.shortcut
        var editor = ShortcutEditorSession(original: original)
        editor.open()
        XCTAssertEqual(
            editor.capture(keyCode: 14, modifiers: [.command]),
            .captured(try GlobalShortcut(keyCode: 14, modifiers: [.command]))
        )
        editor.cancel()

        XCTAssertFalse(editor.isModalOpen)
        XCTAssertEqual(editor.candidate, original)
        XCTAssertEqual(settings.shortcut, original)
    }

    func testShortcutEditorRejectsExclusiveConflictBeforeSaving() throws {
        var editor = ShortcutEditorSession(original: .default)
        editor.open()
        let candidate = try GlobalShortcut(keyCode: 14, modifiers: [.command])
        XCTAssertEqual(
            editor.capture(keyCode: candidate.keyCode, modifiers: candidate.modifiers),
            .captured(candidate)
        )

        let result: ShortcutEditorSaveResult = editor.save { _ in
            ShortcutRegistrationResult(status: OSStatus(eventHotKeyExistsErr))
        }

        XCTAssertEqual(result, .rejected(.registrationConflict))
        XCTAssertTrue(editor.isModalOpen)
        XCTAssertEqual(editor.candidate, candidate)
        XCTAssertTrue(
            ShortcutEditorSaveError.registrationConflict.localizedDescription.contains("其他 app")
        )
    }
}
