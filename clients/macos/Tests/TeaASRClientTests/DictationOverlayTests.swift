import XCTest
@testable import TeaASRClient

final class DictationOverlayTests: XCTestCase {
    func testPartialReplacesThePreviousPartial() {
        var state: DictationOverlayState = .hidden
        state = DictationOverlayPolicy.transition(state, event: .begin)
        state = DictationOverlayPolicy.transition(state, event: .partial("第一版"))
        state = DictationOverlayPolicy.transition(state, event: .partial("修訂版"))
        XCTAssertEqual(state, .partial("修訂版"))
    }

    func testFinalStopAndErrorHaveDeterministicTerminalStates() {
        var state: DictationOverlayState = .hidden
        state = DictationOverlayPolicy.transition(state, event: .begin)
        state = DictationOverlayPolicy.transition(state, event: .final("完成"))
        XCTAssertEqual(state, .final("完成"))
        state = DictationOverlayPolicy.transition(state, event: .stopRequested)
        XCTAssertEqual(state, .stopping)
        state = DictationOverlayPolicy.transition(state, event: .stopped)
        XCTAssertEqual(state, .hidden)
        state = DictationOverlayPolicy.transition(state, event: .failed("服務離線"))
        XCTAssertEqual(state, .error("服務離線"))
        state = DictationOverlayPolicy.transition(state, event: .clear)
        XCTAssertEqual(state, .hidden)
    }

    func testEmptyPartialDoesNotLeaveStaleText() {
        var state: DictationOverlayState = .partial("舊文字")
        state = DictationOverlayPolicy.transition(state, event: .partial(""))
        XCTAssertEqual(state, .listening)
    }

    /// The clipboard-fallback state must be distinct from a successful
    /// `.final`: showing "已辨識" for text that never actually reached the
    /// target field is the "looks fine but nothing happened" symptom this
    /// state exists to prevent. It must also carry a human-readable reason
    /// through unchanged so the overlay can display it.
    func testCopiedToClipboardIsDistinctFromFinalAndCarriesItsReason() {
        var state: DictationOverlayState = .hidden
        state = DictationOverlayPolicy.transition(state, event: .begin)
        state = DictationOverlayPolicy.transition(
            state,
            event: .copiedToClipboard(text: "你好", reason: "原始輸入焦點已變更")
        )
        XCTAssertEqual(state, .copiedToClipboard(text: "你好", reason: "原始輸入焦點已變更"))
        XCTAssertNotEqual(state, .final("你好"))
        state = DictationOverlayPolicy.transition(state, event: .stopped)
        XCTAssertEqual(state, .hidden)
    }

    /// Different fallback reasons (autoInsert off, missing Accessibility
    /// permission, no captured target, focus changed) must not collapse
    /// into the same state, since each needs its own message to the user.
    func testCopiedToClipboardReasonsAreDistinguishable() {
        let missingPermission = DictationOverlayPolicy.transition(
            .listening,
            event: .copiedToClipboard(text: "文字", reason: "缺少輔助使用權限")
        )
        let autoInsertOff = DictationOverlayPolicy.transition(
            .listening,
            event: .copiedToClipboard(text: "文字", reason: "自動貼上已關閉")
        )
        XCTAssertNotEqual(missingPermission, autoInsertOff)
    }
}
