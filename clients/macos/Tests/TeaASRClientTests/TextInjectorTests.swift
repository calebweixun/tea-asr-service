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

    /// Regression coverage for the "floating preview shows the text but it
    /// never lands in the field" bug: `focusedElementAndProcess()` used to
    /// key the fallback identifier off the AXUIElement wrapper's own pointer
    /// address (`Unmanaged.passUnretained(...).toOpaque()`). AX hands back a
    /// fresh wrapper object on every lookup even for the exact same
    /// underlying field, so that pointer almost never matched between
    /// dictation start and the final, and every normal dictation fell back
    /// to the clipboard. `focusedElementIdentifier` must therefore be built
    /// from something that stays equal across repeated lookups of the same
    /// field (AXUIElement's CFHash, which — unlike the wrapper pointer — is
    /// defined over the remote accessibility object) for a normal,
    /// nothing-changed dictation to be allowed through.
    func testNormalDictationFlowAllowsInsertionWhenTheSameFieldReportsTheSameStableIdentifier() {
        // Simulates two lookups of the same field via a hash-of-content
        // style identifier (e.g. CFHash) that is stable across lookups, as
        // opposed to a wrapper pointer that would differ every time.
        let capturedAtDictationStart = TextInsertionTarget(
            processIdentifier: 555,
            focusedElementIdentifier: 0xABCD
        )
        let currentAtFinal = TextInsertionTarget(
            processIdentifier: 555,
            focusedElementIdentifier: 0xABCD
        )
        XCTAssertTrue(
            TextInsertionTargetPolicy.allowsInsertion(
                captured: capturedAtDictationStart,
                current: currentAtFinal
            )
        )
    }

    /// A real target change (user clicked into a different app or field
    /// while dictating) must still be blocked, whether or not either side
    /// reports an accessibility identifier.
    func testGenuineTargetChangeIsStillBlockedWithoutAccessibilityIdentifiers() {
        let capturedAtDictationStart = TextInsertionTarget(
            processIdentifier: 555,
            focusedElementIdentifier: 0xABCD
        )
        let currentAtFinal = TextInsertionTarget(
            processIdentifier: 555,
            focusedElementIdentifier: 0xBEEF
        )
        XCTAssertFalse(
            TextInsertionTargetPolicy.allowsInsertion(
                captured: capturedAtDictationStart,
                current: currentAtFinal
            )
        )
    }

    /// `insert(_:ifCurrent:)` must not silently drop text when the current
    /// focus cannot be proven to be the captured one: it copies to the
    /// clipboard and reports why, rather than doing nothing. This exercises
    /// the real focus lookup (not the pure policy), and passes regardless
    /// of whether this environment has Accessibility permission, because
    /// the captured target's pid (999) cannot legitimately be the focused
    /// app either way.
    func testInsertCopiesToClipboardAndReportsFallbackWhenCurrentFocusDoesNotMatch() {
        let capturedTarget = TextInsertionTarget(processIdentifier: 999, focusedElementIdentifier: 1)
        let text = "測試文字-\(UUID().uuidString)"

        let result = TextInjector.insert(text, ifCurrent: capturedTarget)

        XCTAssertEqual(result, .copiedBecauseFocusChanged)
        XCTAssertEqual(NSPasteboard.general.string(forType: .string), text)
    }
}
