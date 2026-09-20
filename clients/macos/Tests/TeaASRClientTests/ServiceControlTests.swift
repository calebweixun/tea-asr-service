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

    // MARK: - Helpers

    @discardableResult
    private func makeExecutableScript(named name: String, body: String) throws -> URL {
        let url = tempDirectory.appendingPathComponent(name)
        try body.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        return url
    }
}
