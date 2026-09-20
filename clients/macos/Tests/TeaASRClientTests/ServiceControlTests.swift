import XCTest
@testable import TeaASRClient

/// `ServiceControl` is the thin, intentionally-not-pure layer around
/// `ExecutableDiscovery` (see `ExecutableDiscoveryTests` for the pure
/// candidate-list logic) and around `Process` itself. These tests use real
/// temporary files because that is the cheapest way to exercise
/// `FileManager.isExecutableFile`/`Process.run()` faithfully — but every
/// *search-order* decision is already covered without touching disk in
/// `ExecutableDiscoveryTests`.
final class ServiceControlTests: XCTestCase {
    private var tempDirectory: URL!

    override func setUpWithError() throws {
        tempDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ServiceControlTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDirectory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tempDirectory)
        // `start(executable:)` stashes its handle in the shared, static
        // `ServiceControl.lastManagedServeProcess` (see that property's doc
        // comment) so the Logs page can render output for a service started
        // from the menu bar. That state outlives any one test's
        // `XCTestCase` instance, so it must be reset here — otherwise a
        // later test in a different suite (e.g. `ServiceControlSectionTests`)
        // could observe a stale handle left over from a test here that never
        // touches it.
        ServiceControl.lastManagedServeProcess = nil
    }

    // MARK: - search(configured:)

    func testSearchWithConfiguredExecutablePathReturnsItAndOnlyItInTheSearchList() throws {
        let executable = try makeExecutableScript(named: "tea-asr", body: "#!/bin/sh\nexit 0\n")
        let result = ServiceControl.search(configured: executable.path)
        XCTAssertEqual(result.executable, executable)
        XCTAssertEqual(result.searchedPaths, [executable.path])
    }

    func testSearchWithConfiguredPathThatDoesNotExistReportsItAsSearchedButNotFound() {
        let missing = tempDirectory.appendingPathComponent("nope/tea-asr").path
        let result = ServiceControl.search(configured: missing)
        XCTAssertNil(result.executable)
        XCTAssertEqual(result.searchedPaths, [missing])
    }

    func testSearchWithEmptyConfiguredFallsBackToDiscoveryAndReportsWhatItTried() {
        // No configured path in this environment resolves to a real
        // `tea-asr` almost certainly, so this only asserts the *shape* of
        // the fallback: it tried more than one location, and it does not
        // silently trust a blank configuration by returning an empty search.
        let result = ServiceControl.search(configured: "")
        XCTAssertFalse(result.searchedPaths.isEmpty)
    }

    func testSearchTrimsWhitespaceBeforeTreatingAConfiguredPathAsSet() throws {
        let executable = try makeExecutableScript(named: "tea-asr", body: "#!/bin/sh\nexit 0\n")
        let result = ServiceControl.search(configured: "  \(executable.path)  ")
        XCTAssertEqual(result.executable, executable)
    }

    // MARK: - ControlError.executableNotFound

    func testExecutableNotFoundMessageNamesEveryLocationItTried() {
        let error = ServiceControl.ControlError.executableNotFound(searched: [
            "/opt/homebrew/bin/tea-asr",
            "/Users/me/project/.venv/bin/tea-asr",
        ])
        let message = error.errorDescription ?? ""
        XCTAssertTrue(message.contains("/opt/homebrew/bin/tea-asr"))
        XCTAssertTrue(message.contains("/Users/me/project/.venv/bin/tea-asr"))
        // Must explain the *cause*, not just restate "找不到"/"無法連線" —
        // this is the whole point of surfacing a search list at all.
        XCTAssertTrue(message.contains("找不到"))
        XCTAssertFalse(message.contains("無法連線"), "this is a discovery failure, not a connection failure")
    }

    // MARK: - start(executable:)

    func testStartThrowsExecutableMissingWhenThePathDoesNotExist() {
        let missing = tempDirectory.appendingPathComponent("tea-asr")
        XCTAssertThrowsError(try ServiceControl.start(executable: missing)) { error in
            guard case ServiceControl.ControlError.executableMissing(let path) = error else {
                XCTFail("expected .executableMissing, got \(error)")
                return
            }
            XCTAssertEqual(path, missing.path)
        }
    }

    func testStartThrowsNotExecutableWhenThePathExistsWithoutExecutePermission() throws {
        let path = tempDirectory.appendingPathComponent("tea-asr")
        try "not a real binary".write(to: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path.path)

        XCTAssertThrowsError(try ServiceControl.start(executable: path)) { error in
            guard case ServiceControl.ControlError.notExecutable(let reportedPath) = error else {
                XCTFail("expected .notExecutable, got \(error)")
                return
            }
            XCTAssertEqual(reportedPath, path.path)
        }
    }

    func testStartThrowsExitedImmediatelyWhenTheProcessCrashesOnLaunch() throws {
        let script = try makeExecutableScript(
            named: "tea-asr",
            body: "#!/bin/sh\necho 'model file missing' 1>&2\nexit 7\n"
        )
        XCTAssertThrowsError(try ServiceControl.start(executable: script)) { error in
            guard case ServiceControl.ControlError.exitedImmediately(let status, let message) = error else {
                XCTFail("expected .exitedImmediately, got \(error)")
                return
            }
            XCTAssertEqual(status, 7)
            XCTAssertEqual(message, "model file missing")
        }
    }

    func testStartDoesNotThrowWhenTheProcessStaysAliveThroughTheCheckWindow() throws {
        let script = try makeExecutableScript(named: "tea-asr", body: "#!/bin/sh\nsleep 2\nexit 0\n")
        XCTAssertNoThrow(try ServiceControl.start(executable: script))
    }

    /// This is the actual bug report: the menu bar's "啟動服務" used to call
    /// `start(executable:)` and throw the `Process`/`Pipe` away the moment
    /// the crash-on-start check passed (stdout went to `/dev/null`, stderr
    /// was only ever read on a crash), so the Logs page's "服務輸出" tab had
    /// nothing to show for a service started this way — not because the
    /// process produced no output, but because nothing captured it.
    /// `start` now delegates to `launchService` and stashes the resulting
    /// handle in `lastManagedServeProcess`, so this asserts that a real,
    /// still-running process's stdout actually lands there.
    func testStartCapturesLiveOutputIntoTheSharedLastManagedServeProcessHandle() throws {
        let script = try makeExecutableScript(
            named: "tea-asr",
            body: "#!/bin/sh\necho 'starting up'\nsleep 5\n"
        )
        try ServiceControl.start(executable: script)
        defer { ServiceControl.lastManagedServeProcess?.terminate() }

        guard let managed = ServiceControl.lastManagedServeProcess else {
            XCTFail("start(executable:) must populate lastManagedServeProcess")
            return
        }
        XCTAssertTrue(managed.isRunning)

        // The readability handler runs asynchronously; poll for it rather
        // than trusting a single fixed delay (same pattern as
        // `ServiceControlManagedLaunchTests`).
        let deadline = Date().addingTimeInterval(3)
        while managed.output.snapshot().lines.isEmpty && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }

        XCTAssertTrue(managed.output.snapshot().lines.contains("starting up"))
    }

    /// `tea-asr` is a Python entry point, and CPython buffers stdout in
    /// large blocks when it is not attached to a terminal (as it never is
    /// behind a `Pipe`) — a second, independent reason "服務輸出" could look
    /// empty even when output capture itself is wired up correctly: the
    /// process might just not have flushed anything yet. `launchManaged`
    /// (used by `start`, `launchService`, and `launchModelPrepare` alike)
    /// sets `PYTHONUNBUFFERED=1` in the child's environment to rule this
    /// out; this asserts the child actually receives it.
    func testStartSetsPythonUnbufferedInTheChildEnvironment() throws {
        // Sleeps after printing rather than exiting immediately: `start`
        // goes through `launchService`'s 1.2s crash-on-start window (see
        // `ManagedProcessTests`' identical reasoning), and a script that
        // exits cleanly inside that window would be misreported as a
        // launch failure — irrelevant to what this test is actually about.
        let script = try makeExecutableScript(
            named: "tea-asr",
            body: "#!/bin/sh\necho \"PYTHONUNBUFFERED=$PYTHONUNBUFFERED\"\nsleep 5\n"
        )
        try ServiceControl.start(executable: script)
        defer { ServiceControl.lastManagedServeProcess?.terminate() }
        guard let managed = ServiceControl.lastManagedServeProcess else {
            XCTFail("start(executable:) must populate lastManagedServeProcess")
            return
        }

        let deadline = Date().addingTimeInterval(3)
        while managed.output.snapshot().lines.isEmpty && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }

        XCTAssertTrue(managed.output.snapshot().lines.contains("PYTHONUNBUFFERED=1"))
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
