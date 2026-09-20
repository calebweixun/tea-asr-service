import XCTest
@testable import TeaASRClient

/// Covers `InfoButton`, the circular "i" disclosure that replaced several
/// standing explanation labels across the main window (see
/// `MainWindowSectionUpdateTests` for where it is actually wired in).
final class InfoButtonTests: XCTestCase {
    @MainActor
    func testPopoverContentMatchesTheExplanationItWasBuiltWith() {
        let button = InfoButton(explanation: "這段說明只有按下按鈕才會出現。")
        XCTAssertEqual(button.debugPopoverText, "這段說明只有按下按鈕才會出現。")
        XCTAssertEqual(button.explanation, "這段說明只有按下按鈕才會出現。")
    }

    /// A permission row's explanation changes with live TCC state (e.g.
    /// Input Monitoring's text differs once it stops being required), so the
    /// button must expose a mutable `explanation` that refreshes the popover
    /// content in place rather than requiring a new button per state.
    @MainActor
    func testUpdatingExplanationRefreshesThePopoverContentInPlace() {
        let button = InfoButton(explanation: "第一版說明")
        XCTAssertEqual(button.debugPopoverText, "第一版說明")

        button.explanation = "第二版說明"
        XCTAssertEqual(button.debugPopoverText, "第二版說明")
        XCTAssertEqual(button.explanation, "第二版說明")
    }

    /// The whole point of moving explanatory text behind a button is that it
    /// stops permanently occupying layout space. The popover itself must
    /// never be shown until something actually clicks the button — not on
    /// construction, not as a side effect of assigning `explanation`.
    @MainActor
    func testPopoverStaysHiddenUntilClicked() {
        let button = InfoButton(explanation: "說明文字")
        XCTAssertFalse(button.debugPopoverIsShown)
        button.explanation = "另一段說明"
        XCTAssertFalse(button.debugPopoverIsShown, "assigning a new explanation must not open the popover")
    }

    /// The button itself is a small fixed-size disclosure control, not a
    /// label that grows with its text — this is what keeps a row's height
    /// from changing depending on how long the explanation happens to be.
    @MainActor
    func testButtonHasAFixedSmallFootprintRegardlessOfExplanationLength() {
        let short = InfoButton(explanation: "短")
        let long = InfoButton(explanation: String(repeating: "很長的說明文字，", count: 40))

        for button in [short, long] {
            let widthConstraints = button.constraints.filter { $0.firstAttribute == .width }
            let heightConstraints = button.constraints.filter { $0.firstAttribute == .height }
            XCTAssertEqual(widthConstraints.map(\.constant), [16])
            XCTAssertEqual(heightConstraints.map(\.constant), [16])
        }
    }

    /// `info.circle`, never `exclamationmark.circle`: this button discloses
    /// neutral background copy, and its icon must not read as a warning.
    @MainActor
    func testUsesTheNeutralInfoSymbolNotAWarningSymbol() {
        let button = InfoButton(explanation: "說明")
        XCTAssertEqual(button.image?.accessibilityDescription, "說明")
        XCTAssertEqual(button.contentTintColor, .secondaryLabelColor)
    }
}
