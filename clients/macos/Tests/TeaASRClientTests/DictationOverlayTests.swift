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
}
