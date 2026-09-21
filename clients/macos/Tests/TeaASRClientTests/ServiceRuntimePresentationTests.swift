import XCTest
@testable import TeaASRClient

/// Pure presentation logic for the Settings page's start/stop control
/// (`ServiceRuntimeControl`) and the Logs page's "服務輸出" tab
/// (`ServiceOutputPresentation`). Neither type touches `Process` or AppKit,
/// so every state combination is asserted directly here.
final class ServiceRuntimeControlTests: XCTestCase {
    func testManagedRunningShowsRestartAndIsAlwaysEnabled() {
        let presentation = ServiceRuntimeControl.presentation(for: .managedRunning)
        XCTAssertEqual(presentation.buttonTitle, "重新啟動服務")
        XCTAssertTrue(presentation.buttonEnabled)
    }

    func testReachableElsewhereDisablesTheButtonAndExplainsWhy() {
        let presentation = ServiceRuntimeControl.presentation(for: .reachableElsewhere)
        XCTAssertEqual(presentation.buttonTitle, "重新啟動服務")
        XCTAssertFalse(presentation.buttonEnabled, "must never offer to start a duplicate or stop an untracked process")
        XCTAssertTrue(presentation.statusText.contains("不是由這個頁面啟動"))
    }

    func testStoppedWithExecutableFoundShowsRestartEnabled() {
        let presentation = ServiceRuntimeControl.presentation(
            for: .stopped(executableFound: true, exitStatus: nil)
        )
        XCTAssertEqual(presentation.buttonTitle, "重新啟動服務")
        XCTAssertTrue(presentation.buttonEnabled)
        XCTAssertEqual(presentation.statusText, "已停止。")
    }

    func testStoppedWithANonZeroExitStatusSurfacesItInTheStatusText() {
        let presentation = ServiceRuntimeControl.presentation(
            for: .stopped(executableFound: true, exitStatus: 7)
        )
        XCTAssertTrue(presentation.statusText.contains("7"))
    }

    func testStoppedWithoutAnExecutableDisablesTheButtonAndNamesTheReason() {
        let presentation = ServiceRuntimeControl.presentation(
            for: .stopped(executableFound: false, exitStatus: nil)
        )
        XCTAssertFalse(presentation.buttonEnabled)
        XCTAssertTrue(presentation.statusText.contains("找不到執行檔"))
    }

    // MARK: - state(...) precedence

    func testManagedRunningTakesPrecedenceOverReachableElsewhere() {
        let state = ServiceRuntimeControl.state(
            managedRunning: true,
            reachableElsewhere: true,
            executableFound: true,
            exitStatus: nil
        )
        XCTAssertEqual(state, .managedRunning)
    }

    func testReachableElsewhereTakesPrecedenceOverStopped() {
        let state = ServiceRuntimeControl.state(
            managedRunning: false,
            reachableElsewhere: true,
            executableFound: true,
            exitStatus: nil
        )
        XCTAssertEqual(state, .reachableElsewhere)
    }
}

final class ServiceAutoStartPolicyTests: XCTestCase {
    func testSkipsWhenAlreadyReachableRegardlessOfExecutable() {
        XCTAssertEqual(
            ServiceAutoStartPolicy.decide(reachable: true, executableFound: true),
            .skipAlreadyRunning
        )
        XCTAssertEqual(
            ServiceAutoStartPolicy.decide(reachable: true, executableFound: false),
            .skipAlreadyRunning
        )
    }

    func testSkipsWithAClearReasonWhenNothingIsReachableAndNoExecutableWasFound() {
        XCTAssertEqual(
            ServiceAutoStartPolicy.decide(reachable: false, executableFound: false),
            .skipExecutableNotFound
        )
    }

    func testStartsWhenNothingIsReachableAndAnExecutableWasFound() {
        XCTAssertEqual(
            ServiceAutoStartPolicy.decide(reachable: false, executableFound: true),
            .start
        )
    }
}

final class ServiceOutputPresentationTests: XCTestCase {
    func testNotManagedAndReachableElsewhereSaysSoExplicitlyRatherThanShowingNothing() {
        let text = ServiceOutputPresentation.statusText(isManaged: false, isRunning: false, reachableElsewhere: true)
        XCTAssertEqual(text, "服務不是由這個 app 啟動，看不到它的輸出。")
    }

    func testNotManagedAndNotReachableSuggestsStartingIt() {
        let text = ServiceOutputPresentation.statusText(isManaged: false, isRunning: false, reachableElsewhere: false)
        XCTAssertTrue(text.contains("尚未啟動服務"))
    }

    func testManagedAndRunningReportsRunning() {
        let text = ServiceOutputPresentation.statusText(isManaged: true, isRunning: true, reachableElsewhere: false)
        XCTAssertEqual(text, "執行中。")
    }

    func testManagedAndExitedReportsExited() {
        let text = ServiceOutputPresentation.statusText(isManaged: true, isRunning: false, reachableElsewhere: false)
        XCTAssertTrue(text.contains("已結束"))
    }

    func testEmptyOutputSaysSoRatherThanRenderingBlank() {
        XCTAssertEqual(ServiceOutputPresentation.body(lines: [], droppedLines: 0), "尚無輸出。")
    }

    func testNonEmptyOutputJoinsLinesWithoutATruncationNoticeWhenNothingWasDropped() {
        let body = ServiceOutputPresentation.body(lines: ["a", "b"], droppedLines: 0)
        XCTAssertEqual(body, "a\nb")
    }

    /// The truncation from `BoundedLineBuffer` must stay visible in the
    /// rendered text, not just in a debug-only count no one sees.
    func testTruncationIsSurfacedInTheRenderedBody() {
        let body = ServiceOutputPresentation.body(lines: ["a", "b"], droppedLines: 5)
        XCTAssertTrue(body.contains("已捨棄 5 行"))
        XCTAssertTrue(body.hasSuffix("a\nb"))
    }
}
