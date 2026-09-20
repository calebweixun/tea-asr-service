import XCTest
@testable import TeaASRClient

/// `ExecutableDiscovery` is the pure candidate-list logic behind
/// `ServiceControl.search(configured:)` — no `FileManager`, no `Bundle`, no
/// real filesystem access anywhere in this file. Every case below hands it
/// fabricated candidate lists and a fake `isExecutable` closure, which is
/// exactly what lets "found via PATH", "found via project venv", and "not
/// found anywhere" all be exercised deterministically.
final class ExecutableDiscoveryTests: XCTestCase {
    // MARK: - candidates(pathEnvironment:startingDirectory:)

    func testCandidatesIncludeEveryPathDirectory() {
        let candidates = ExecutableDiscovery.candidates(
            pathEnvironment: "/usr/bin:/opt/homebrew/bin:/Users/me/bin",
            startingDirectory: nil
        )
        let paths = candidates.map(\.path)
        XCTAssertTrue(paths.contains("/usr/bin/tea-asr"))
        XCTAssertTrue(paths.contains("/opt/homebrew/bin/tea-asr"))
        XCTAssertTrue(paths.contains("/Users/me/bin/tea-asr"))
    }

    func testCandidatesSkipEmptyPathSegments() {
        // A trailing/doubled colon in $PATH (":/usr/bin::/bin") must not turn
        // into a bogus "tea-asr" candidate for the empty string.
        let candidates = ExecutableDiscovery.candidates(
            pathEnvironment: ":/usr/bin::/bin:",
            startingDirectory: nil
        )
        XCTAssertFalse(candidates.contains { $0.path == "tea-asr" })
        XCTAssertTrue(candidates.contains { $0.path == "/usr/bin/tea-asr" })
        XCTAssertTrue(candidates.contains { $0.path == "/bin/tea-asr" })
    }

    func testCandidatesAlwaysIncludeTheFixedHomebrewAndLocalLocations() {
        let candidates = ExecutableDiscovery.candidates(pathEnvironment: nil, startingDirectory: nil)
        let paths = candidates.map(\.path)
        XCTAssertTrue(paths.contains("/opt/homebrew/bin/tea-asr"))
        XCTAssertTrue(paths.contains("/usr/local/bin/tea-asr"))
    }

    func testCandidatesIncludeTheProjectVenvAtEveryAncestorDirectory() {
        let candidates = ExecutableDiscovery.candidates(
            pathEnvironment: nil,
            startingDirectory: "/Users/me/Codes/tea-asr-service/clients/macos/.build/debug",
            maxVenvAncestors: 8
        )
        let paths = candidates.map(\.path)
        // The real checkout root sits several levels above the running
        // binary's own directory — this is the case the login-shell-only
        // fallback used to miss entirely for a source checkout that never
        // `pip install`ed `tea-asr` onto $PATH.
        XCTAssertTrue(paths.contains("/Users/me/Codes/tea-asr-service/.venv/bin/tea-asr"))
        XCTAssertTrue(paths.contains("/Users/me/Codes/tea-asr-service/clients/macos/.venv/bin/tea-asr"))
    }

    func testCandidatesWithNoStartingDirectoryOmitVenvCandidates() {
        let candidates = ExecutableDiscovery.candidates(pathEnvironment: nil, startingDirectory: nil)
        XCTAssertFalse(candidates.contains { $0.source == "專案 .venv" })
    }

    // MARK: - venvCandidatePaths

    func testVenvCandidatePathsClimbUpToTheFilesystemRoot() {
        let paths = ExecutableDiscovery.venvCandidatePaths(startingAt: "/a/b", maxAncestors: 10)
        XCTAssertEqual(paths, [
            "/a/b/.venv/bin/tea-asr",
            "/a/.venv/bin/tea-asr",
            "/.venv/bin/tea-asr",
        ])
    }

    func testVenvCandidatePathsRespectTheAncestorCeiling() {
        let paths = ExecutableDiscovery.venvCandidatePaths(startingAt: "/a/b/c/d", maxAncestors: 1)
        XCTAssertEqual(paths, [
            "/a/b/c/d/.venv/bin/tea-asr",
            "/a/b/c/.venv/bin/tea-asr",
        ])
    }

    // MARK: - firstExecutable

    func testFirstExecutableReturnsTheFirstMatchingCandidateInOrder() {
        let candidates = [
            ExecutableDiscovery.Candidate(path: "/opt/homebrew/bin/tea-asr", source: "Homebrew"),
            ExecutableDiscovery.Candidate(path: "/a/.venv/bin/tea-asr", source: "專案 .venv"),
        ]
        let found = ExecutableDiscovery.firstExecutable(in: candidates) { $0 == "/a/.venv/bin/tea-asr" }
        XCTAssertEqual(found, ExecutableDiscovery.Candidate(path: "/a/.venv/bin/tea-asr", source: "專案 .venv"))
    }

    func testFirstExecutableReturnsNilWhenNothingIsExecutable() {
        let candidates = [
            ExecutableDiscovery.Candidate(path: "/opt/homebrew/bin/tea-asr", source: "Homebrew"),
            ExecutableDiscovery.Candidate(path: "/usr/local/bin/tea-asr", source: "/usr/local/bin"),
        ]
        let found = ExecutableDiscovery.firstExecutable(in: candidates) { _ in false }
        XCTAssertNil(found)
    }
}
