import XCTest
@testable import TeaASRClient

/// Covers the pieces that make the Settings page's start/stop control and
/// the Logs page's "服務輸出" tab possible: `BoundedLineBuffer` (a hard cap
/// on retained output with visible truncation) and `ManagedProcess`/
/// `ServiceControl.launchService`/`launchModelPrepare` (a live process
/// handle that can later be `terminate()`d safely, unlike the older
/// fire-and-forget `start(executable:)`).
final class BoundedLineBufferTests: XCTestCase {
    func testAppendSplitsCompleteLinesAndHoldsAnIncompleteTrailingLineAsPending() {
        let buffer = BoundedLineBuffer(maxLines: 10)
        buffer.append("line one\nline two\npartial")
        XCTAssertEqual(buffer.snapshot().lines, ["line one", "line two"])

        buffer.append(" continues\nline three\n")
        XCTAssertEqual(buffer.snapshot().lines, ["line one", "line two", "partial continues", "line three"])
    }

    func testFlushSurfacesAFinalLineThatNeverGotATrailingNewline() {
        let buffer = BoundedLineBuffer(maxLines: 10)
        buffer.append("no trailing newline")
        XCTAssertEqual(buffer.snapshot().lines, [], "an incomplete line must not appear before flush")

        buffer.flush()
        XCTAssertEqual(buffer.snapshot().lines, ["no trailing newline"])
    }

    func testFlushIsANoOpWhenNothingIsPending() {
        let buffer = BoundedLineBuffer(maxLines: 10)
        buffer.append("complete\n")
        buffer.flush()
        XCTAssertEqual(buffer.snapshot().lines, ["complete"])
    }

    /// The docs/06 bounded-buffer constraint: a cap that is actually
    /// enforced, and a truncation that is visible (`droppedLines`) rather
    /// than a buffer that silently grows or silently loses data.
    func testExceedingMaxLinesDropsTheOldestLinesAndReportsHowManyWereDropped() {
        let buffer = BoundedLineBuffer(maxLines: 3)
        for index in 1...5 {
            buffer.append("line \(index)\n")
        }
        let snapshot = buffer.snapshot()
        XCTAssertEqual(snapshot.lines, ["line 3", "line 4", "line 5"], "only the most recent maxLines lines survive")
        XCTAssertEqual(snapshot.droppedLines, 2, "every line evicted to stay under the cap must be counted")
    }

    func testMaxLinesIsFlooredAtOneEvenIfConstructedWithZeroOrNegative() {
        let buffer = BoundedLineBuffer(maxLines: 0)
        buffer.append("a\nb\n")
        XCTAssertEqual(buffer.snapshot().lines.count, 1)
    }
}

final class ServiceControlManagedLaunchTests: XCTestCase {
    private var tempDirectory: URL!

    override func setUpWithError() throws {
        tempDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ServiceControlManagedLaunchTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDirectory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tempDirectory)
    }

    // MARK: - launchService error paths (mirrors the three ControlError cases start(executable:) reports)

    func testLaunchServiceThrowsExecutableMissingWhenThePathDoesNotExist() {
        let missing = tempDirectory.appendingPathComponent("tea-asr")
        XCTAssertThrowsError(try ServiceControl.launchService(executable: missing)) { error in
            guard case ServiceControl.ControlError.executableMissing(let path) = error else {
                XCTFail("expected .executableMissing, got \(error)")
                return
            }
            XCTAssertEqual(path, missing.path)
        }
    }

    func testLaunchServiceThrowsNotExecutableWhenThePathLacksExecutePermission() throws {
        let path = tempDirectory.appendingPathComponent("tea-asr")
        try "not a real binary".write(to: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path.path)

        XCTAssertThrowsError(try ServiceControl.launchService(executable: path)) { error in
            guard case ServiceControl.ControlError.notExecutable(let reportedPath) = error else {
                XCTFail("expected .notExecutable, got \(error)")
                return
            }
            XCTAssertEqual(reportedPath, path.path)
        }
    }

    func testLaunchServiceThrowsExitedImmediatelyAndCapturesItsOutputAsTheMessage() throws {
        let script = try makeExecutableScript(
            named: "tea-asr",
            body: "#!/bin/sh\necho 'model file missing' 1>&2\nexit 7\n"
        )
        XCTAssertThrowsError(try ServiceControl.launchService(executable: script)) { error in
            guard case ServiceControl.ControlError.exitedImmediately(let status, let message) = error else {
                XCTFail("expected .exitedImmediately, got \(error)")
                return
            }
            XCTAssertEqual(status, 7)
            XCTAssertEqual(message, "model file missing")
        }
    }

    // MARK: - The live handle itself, which is the whole point of launchService/launchModelPrepare

    func testLaunchServiceReturnsAHandleThatStaysRunningAndCanBeTerminatedSafely() throws {
        let script = try makeExecutableScript(named: "tea-asr", body: "#!/bin/sh\nsleep 5\nexit 0\n")
        let managed = try ServiceControl.launchService(executable: script)
        XCTAssertTrue(managed.isRunning)

        managed.terminate()
        managed.process.waitUntilExit()
        XCTAssertFalse(managed.isRunning, "terminate() must actually stop the exact process this call launched")
    }

    /// `terminate()` on an already-exited handle must not raise or crash —
    /// there is no force-kill fallback, and calling it twice (e.g. from both
    /// a user click and a later cleanup path) must be safe. Uses
    /// `launchModelPrepare` rather than `launchService` because the latter's
    /// 0.3s crash-on-start check would treat an immediate, clean exit as a
    /// launch failure — irrelevant to what this test is actually about.
    func testTerminateOnAnAlreadyExitedProcessIsANoOp() throws {
        let script = try makeExecutableScript(named: "tea-asr", body: "#!/bin/sh\nexit 0\n")
        let managed = try ServiceControl.launchModelPrepare(executable: script)
        managed.process.waitUntilExit()
        XCTAssertFalse(managed.isRunning)

        managed.terminate()
        managed.terminate()
    }

    func testLaunchServiceCapturesCombinedStdoutAndStderrIntoTheBoundedBuffer() throws {
        let script = try makeExecutableScript(
            named: "tea-asr",
            body: "#!/bin/sh\necho 'starting up'\nsleep 5\n"
        )
        let managed = try ServiceControl.launchService(executable: script)
        defer { managed.terminate() }

        // The readability handler runs asynchronously; poll for it rather
        // than trusting a single fixed delay.
        let deadline = Date().addingTimeInterval(3)
        while managed.output.snapshot().lines.isEmpty && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }

        XCTAssertTrue(managed.output.snapshot().lines.contains("starting up"))
    }

    func testLaunchModelPrepareRunsTheModelPrepareSubcommandAndTracksExit() throws {
        let script = try makeExecutableScript(
            named: "tea-asr",
            body: "#!/bin/sh\necho \"args: $@\"\nexit 0\n"
        )
        let managed = try ServiceControl.launchModelPrepare(executable: script)
        managed.process.waitUntilExit()

        XCTAssertEqual(managed.exitStatus, 0)
        managed.output.flush()
        XCTAssertTrue(managed.output.snapshot().lines.contains("args: model-prepare"))
    }

    func testOnExitFiresWithTheProcessExitStatus() throws {
        let script = try makeExecutableScript(named: "tea-asr", body: "#!/bin/sh\nexit 3\n")
        let managed = try ServiceControl.launchModelPrepare(executable: script)
        let expectation = expectation(description: "onExit fired")
        managed.onExit = { status in
            XCTAssertEqual(status, 3)
            expectation.fulfill()
        }
        wait(for: [expectation], timeout: 2)
    }

    // MARK: - Helpers

    @discardableResult
    private func makeExecutableScript(named name: String, body: String) throws -> URL {
        let url = tempDirectory.appendingPathComponent(name)
        try body.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        return url
    }
}
