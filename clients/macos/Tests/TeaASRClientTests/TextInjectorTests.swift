import XCTest
@testable import TeaASRClient

final class TextInjectorTests: XCTestCase {
    func testInsertionRequiresTheSameApplicationAndFocusedElement() {
        let target = TextInsertionTarget(processIdentifier: 101, focusedElementIdentifier: 202)

        XCTAssertTrue(TextInsertionTargetPolicy.allowsInsertion(captured: target, current: target))
        XCTAssertFalse(
            TextInsertionTargetPolicy.allowsInsertion(
                captured: target,
                current: TextInsertionTarget(processIdentifier: 101, focusedElementIdentifier: 303)
            )
        )
        XCTAssertFalse(
            TextInsertionTargetPolicy.allowsInsertion(
                captured: target,
                current: TextInsertionTarget(processIdentifier: 404, focusedElementIdentifier: 202)
            )
        )
        XCTAssertFalse(TextInsertionTargetPolicy.allowsInsertion(captured: target, current: nil))

        // AXUIElement wrappers can be recreated by the system; a stable AX
        // identifier is preferred over wrapper pointer identity when present.
        XCTAssertTrue(
            TextInsertionTargetPolicy.allowsInsertion(
                captured: TextInsertionTarget(
                    processIdentifier: 101,
                    focusedElementIdentifier: 202,
                    accessibilityIdentifier: "editor.main"
                ),
                current: TextInsertionTarget(
                    processIdentifier: 101,
                    focusedElementIdentifier: 303,
                    accessibilityIdentifier: "editor.main"
                )
            )
        )
    }
}
