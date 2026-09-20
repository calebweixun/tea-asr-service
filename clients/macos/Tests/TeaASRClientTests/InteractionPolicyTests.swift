import XCTest
@testable import TeaASRClient

final class InteractionPolicyTests: XCTestCase {
    func testToggleStartsAndStopsWithoutTreatingRepeatAsSecondToggle() {
        var machine = DictationInteractionStateMachine(mode: .toggle)

        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: false)), [.start])
        XCTAssertTrue(machine.active)
        XCTAssertEqual(machine.handle(.shortcutDown(isRepeat: true)), [])
        XCTAssertTrue(machine.active)
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
        XCTAssertFalse(InteractionFeedbackPolicy.shouldPlay(enabled: true, event: .failed))
    }
}
