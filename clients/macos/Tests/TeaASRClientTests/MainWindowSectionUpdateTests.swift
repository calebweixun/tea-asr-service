import XCTest
@testable import TeaASRClient

/// Covers the MainWindowController refactor that stopped tearing down and
/// rebuilding the whole detail pane on every status tick (see
/// `renderDetail`'s old `subviews.forEach { $0.removeFromSuperview() }`).
///
/// XCTest can construct real NSWindow/NSView instances in this headless
/// environment (verified experimentally: NSWindow, NSTextField and friends
/// build and lay out fine without a window server as long as nothing calls
/// `makeKeyAndOrderFront`/`orderFront`), so this exercises the production
/// AppKit view hierarchy directly rather than a fake model. If that ever
/// stops being true in CI, prefer skipping over asserting something false.
final class MainWindowSectionUpdateTests: XCTestCase {
    @MainActor
    func testRepeatedStatusUpdatesReuseTheSameSectionViewAndRefreshItsLabel() {
        let controller = makeController()

        // Default section is .overview; mounting it the first time builds it.
        guard let firstMount = controller.debugMountedSectionView else {
            XCTFail("expected a mounted section view after construction")
            return
        }

        controller.setStatus("狀態一")
        guard let secondMount = controller.debugMountedSectionView else {
            XCTFail("expected a mounted section view after setStatus")
            return
        }
        // The whole point of the refactor: a status update must not replace
        // the section's root view (which is what removeFromSuperview()-based
        // rebuilding used to do on every call).
        XCTAssertTrue(firstMount === secondMount, "setStatus must not rebuild the overview section's view tree")
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("狀態一") }))

        controller.setStatus("狀態二")
        let thirdMount = controller.debugMountedSectionView
        XCTAssertTrue(firstMount === thirdMount, "a second update must still reuse the same view")
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("狀態二") }))
        XCTAssertFalse(controller.debugLabelTexts().contains(where: { $0.contains("狀態一") }), "stale text must not linger after the label is reassigned")
    }

    @MainActor
    func testSwitchingSectionsAndBackReusesTheCachedViewInstance() {
        let controller = makeController()
        controller.show(section: .overview)
        let overviewView = controller.debugMountedSectionView

        controller.show(section: .operations)
        XCTAssertFalse(controller.debugMountedSectionView === overviewView, "a different section must mount a different view")

        controller.show(section: .overview)
        XCTAssertTrue(controller.debugMountedSectionView === overviewView, "returning to a section must reuse its cached view, not rebuild it")
    }

    /// The permission checklist is a group inside Diagnostics now, not a
    /// sidebar destination: the sidebar has four entries and the permission
    /// rows render on the Diagnostics page.
    @MainActor
    func testDiagnosticsSectionCarriesThePermissionRows() {
        XCTAssertEqual(MainWindowController.Section.allCases.count, 4)
        XCTAssertEqual(MainWindowController.Section.diagnostics.title, "診斷與權限")
        XCTAssertFalse(MainWindowController.Section.allCases.map(\.title).contains("權限"))

        let controller = makeController()
        controller.show(section: .diagnostics)
        let texts = controller.debugLabelTexts()
        XCTAssertTrue(texts.contains("權限"), "the diagnostics page must show the permission group heading")
        XCTAssertTrue(
            texts.contains(where: { $0.hasPrefix(PermissionKind.accessibility.title) }),
            "the diagnostics page must show a row per permission"
        )
    }

    /// A TCC change must refresh the permission rows through the section's
    /// update closure, never by rebuilding the Diagnostics view tree.
    @MainActor
    func testPermissionChangeUpdatesDiagnosticsInPlace() {
        let controller = makeController()
        controller.show(section: .diagnostics)
        let mounted = controller.debugMountedSectionView
        controller.refresh()
        XCTAssertTrue(controller.debugMountedSectionView === mounted, "a permission refresh must reuse the diagnostics view")
    }

    /// The sidebar is fixed furniture: one width, no drag, no collapse.
    @MainActor
    func testSidebarIsFixedWidthAndCannotCollapseOrBeDragged() {
        let controller = makeController()
        let item = controller.debugSidebarSplitItem
        XCTAssertFalse(item.canCollapse)
        XCTAssertFalse(item.isCollapsed)
        XCTAssertEqual(item.minimumThickness, item.maximumThickness)
        XCTAssertEqual(controller.debugDividerDragRect(), .zero, "the divider must expose no drag hit area")
    }

    @MainActor
    private func makeController() -> MainWindowController {
        let suiteName = "MainWindowSectionUpdateTests-\(UUID().uuidString)"
        let settings = Settings(defaults: UserDefaults(suiteName: suiteName)!)
        let appState = AppState()
        let permissions = PermissionCoordinator(
            platform: FakeSectionUpdateTestPlatform(),
            autoInsert: true,
            requiresInputMonitoring: false
        )
        return MainWindowController(settings: settings, appState: appState, permissions: permissions)
    }
}

private struct FakeSectionUpdateTestPlatform: PermissionPlatform {
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
