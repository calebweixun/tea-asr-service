import XCTest
@testable import TeaASRClient

/// Covers the Logs page inside `MainWindowController`: fetch timing (on
/// section switch / explicit refresh / level change, never on a timer),
/// honest failure/empty/has-more presentation, and the build-once +
/// update-in-place contract every other section already follows.
///
/// `MainWindowController` takes a `LogsFetching` dependency so these tests
/// never touch the real network — `FakeLogsClient` completes synchronously
/// with a canned result, the same seam `PermissionCoordinator`'s
/// `PermissionPlatform` already uses elsewhere in this test target.
final class LogsSectionTests: XCTestCase {
    @MainActor
    func testSwitchingToLogsSectionFetchesOnceWithTheDefaultWarningLevel() {
        let fake = FakeLogsClient()
        fake.result = .success(LogsResponse(
            items: [
                LogEntry(
                    ts: "2026-09-20T10:00:00Z",
                    level: "ERROR",
                    logger: "tea_asr.api",
                    message: "worker.restart_failed",
                    fields: ["code": .string("E1")]
                ),
            ],
            count: 1,
            limit: 200,
            hasMore: false
        ))
        let controller = makeController(logsClient: fake)

        controller.show(section: .logs)

        XCTAssertEqual(fake.fetchCallCount, 1)
        XCTAssertEqual(fake.lastLevel, .warning, "opening the page must default to warning+error, not debug/info noise")
        XCTAssertEqual(fake.lastLimit, 200)
        XCTAssertTrue(controller.debugLogsRenderedText?.contains("worker.restart_failed") ?? false)
    }

    /// Mounting the same page again must not re-issue a network call: only
    /// switching sections, refreshing, or changing the level filter should.
    @MainActor
    func testMerelyRefreshingTheWindowDoesNotRefetchLogs() {
        let fake = FakeLogsClient()
        let controller = makeController(logsClient: fake)
        controller.show(section: .logs)
        XCTAssertEqual(fake.fetchCallCount, 1)

        controller.refresh()

        XCTAssertEqual(fake.fetchCallCount, 1, "a background status refresh must not poll /v1/logs")
    }

    @MainActor
    func testHasMoreIsSurfacedHonestly() {
        let fake = FakeLogsClient()
        fake.result = .success(LogsResponse(items: [], count: 0, limit: 50, hasMore: true))
        let controller = makeController(logsClient: fake)

        controller.show(section: .logs)

        XCTAssertTrue(
            controller.debugLabelTexts().contains(where: { $0.contains("還有") }),
            "has_more must be surfaced, never silently dropped"
        )
    }

    @MainActor
    func testTrulyEmptyResultIsDistinctFromAnyFailure() {
        let fake = FakeLogsClient()
        fake.result = .success(LogsResponse(items: [], count: 0, limit: 100, hasMore: false))
        let controller = makeController(logsClient: fake)

        controller.show(section: .logs)

        XCTAssertTrue(controller.debugLabelTexts().contains(LogsPresentation.emptyNotice()))
    }

    @MainActor
    func testServiceUnreachableShowsAnExplicitReasonNotNoLogs() {
        let fake = FakeLogsClient()
        fake.result = .failure(.unreachable("Connection refused"))
        let controller = makeController(logsClient: fake)

        controller.show(section: .logs)

        let texts = controller.debugLabelTexts()
        XCTAssertTrue(texts.contains(where: { $0.contains("無法連到") }))
        XCTAssertFalse(texts.contains(where: { $0 == LogsPresentation.emptyNotice() }))
    }

    @MainActor
    func testUnauthorizedShowsAnExplicitReasonNotNoLogs() {
        let fake = FakeLogsClient()
        fake.result = .failure(.unauthorized)
        let controller = makeController(logsClient: fake)

        controller.show(section: .logs)

        let texts = controller.debugLabelTexts()
        XCTAssertTrue(texts.contains(where: { $0.contains("token") }))
        XCTAssertFalse(texts.contains(where: { $0 == LogsPresentation.emptyNotice() }))
    }

    @MainActor
    func testUnparsableResponseShowsAnExplicitReasonNotNoLogs() {
        let fake = FakeLogsClient()
        fake.result = .failure(.invalidResponse)
        let controller = makeController(logsClient: fake)

        controller.show(section: .logs)

        let texts = controller.debugLabelTexts()
        XCTAssertTrue(texts.contains(where: { $0.contains("回應格式") }))
        XCTAssertFalse(texts.contains(where: { $0 == LogsPresentation.emptyNotice() }))
    }

    @MainActor
    func testRefreshButtonRefetchesWithTheCurrentLevel() {
        let fake = FakeLogsClient()
        let controller = makeController(logsClient: fake)
        controller.show(section: .logs)
        XCTAssertEqual(fake.fetchCallCount, 1)

        guard
            let mounted = controller.debugMountedSectionView,
            let button: NSButton = findButton(titled: "重新整理", in: mounted)
        else {
            XCTFail("找不到重新整理按鈕")
            return
        }
        guard let target = button.target, let action = button.action else {
            XCTFail("重新整理按鈕未接上 target/action")
            return
        }
        _ = target.perform(action, with: button)

        XCTAssertEqual(fake.fetchCallCount, 2)
        XCTAssertEqual(fake.lastLevel, .warning)
    }

    @MainActor
    func testChangingTheLevelPopupRefetchesWithTheNewLevel() {
        let fake = FakeLogsClient()
        let controller = makeController(logsClient: fake)
        controller.show(section: .logs)

        guard
            let mounted = controller.debugMountedSectionView,
            let popup: NSPopUpButton = findView(identifier: "logsLevel", in: mounted)
        else {
            XCTFail("找不到日誌等級篩選控制項")
            return
        }
        guard let errorItem = popup.itemArray.first(where: { ($0.representedObject as? String) == LogLevel.error.rawValue }) else {
            XCTFail("找不到 Error 選項")
            return
        }
        popup.select(errorItem)
        guard let target = popup.target, let action = popup.action else {
            XCTFail("等級篩選控制項未接上 target/action")
            return
        }
        _ = target.perform(action, with: popup)

        XCTAssertEqual(fake.fetchCallCount, 2)
        XCTAssertEqual(fake.lastLevel, .error)
    }

    /// The same build-once + update-in-place contract every other section
    /// already has to hold: leaving and returning to the page must reuse the
    /// cached view instance rather than tearing it down.
    @MainActor
    func testLogsSectionIsBuiltOnceAndUpdatedInPlace() {
        let fake = FakeLogsClient()
        let controller = makeController(logsClient: fake)

        controller.show(section: .logs)
        guard let firstMount = controller.debugMountedSectionView else {
            XCTFail("expected a mounted logs section view")
            return
        }

        controller.show(section: .overview)
        controller.show(section: .logs)

        XCTAssertTrue(
            controller.debugMountedSectionView === firstMount,
            "returning to the logs page must reuse the already-built view, not rebuild it"
        )
        // A second visit does re-fetch (see the "switching to the page"
        // contract above), but that must update the existing labels/text
        // view in place rather than replacing the section's root view.
        XCTAssertEqual(fake.fetchCallCount, 2)
    }

    @MainActor
    func testSidebarStillHasFiveDestinationsWithLogsLast() {
        XCTAssertEqual(MainWindowController.Section.allCases.count, 5)
        XCTAssertEqual(MainWindowController.Section.allCases.last, .logs)
        XCTAssertEqual(MainWindowController.Section.logs.title, "日誌")
    }

    // MARK: - Helpers

    @MainActor
    private func makeController(logsClient: LogsFetching) -> MainWindowController {
        let suiteName = "LogsSectionTests-\(UUID().uuidString)"
        let settings = Settings(defaults: UserDefaults(suiteName: suiteName)!)
        let permissions = PermissionCoordinator(
            platform: FakeLogsSectionTestPlatform(),
            autoInsert: true,
            requiresInputMonitoring: false
        )
        return MainWindowController(settings: settings, appState: AppState(), permissions: permissions, logsClient: logsClient)
    }

    @MainActor
    private func findView<T: NSView>(identifier: String, in root: NSView) -> T? {
        if root.identifier?.rawValue == identifier, let match = root as? T {
            return match
        }
        for subview in root.subviews {
            if let found: T = findView(identifier: identifier, in: subview) {
                return found
            }
        }
        return nil
    }

    @MainActor
    private func findButton(titled title: String, in root: NSView) -> NSButton? {
        if let button = root as? NSButton, button.title == title {
            return button
        }
        for subview in root.subviews {
            if let found = findButton(titled: title, in: subview) {
                return found
            }
        }
        return nil
    }
}

private struct FakeLogsSectionTestPlatform: PermissionPlatform {
    var microphoneAuthorization: PermissionAuthorization { .authorized }
    var accessibilityTrusted: Bool { true }
    var inputMonitoringAuthorized: Bool { true }
    func requestMicrophoneAccess(completion: @escaping (Bool) -> Void) { completion(true) }
    @discardableResult
    func promptAccessibility() -> Bool { true }
    @discardableResult
    func promptInputMonitoring() -> Bool { true }
    @discardableResult
    func openSettings(for kind: PermissionKind) -> Bool { true }
}

/// Completes synchronously on the calling thread so tests stay deterministic
/// and never touch the network — the level/limit/host/port/token arguments
/// are recorded for assertions on request assembly at the section level.
final class FakeLogsClient: LogsFetching {
    var result: Result<LogsResponse, LogsFetchError> = .success(LogsResponse(items: [], count: 0, limit: 100, hasMore: false))
    private(set) var fetchCallCount = 0
    private(set) var lastLevel: LogLevel?
    private(set) var lastLimit: Int?
    private(set) var lastHost: String?
    private(set) var lastPort: Int?
    private(set) var lastToken: String?

    func fetch(
        host: String,
        port: Int,
        token: String?,
        level: LogLevel,
        limit: Int,
        completion: @escaping (Result<LogsResponse, LogsFetchError>) -> Void
    ) {
        fetchCallCount += 1
        lastHost = host
        lastPort = port
        lastToken = token
        lastLevel = level
        lastLimit = limit
        completion(result)
    }
}
