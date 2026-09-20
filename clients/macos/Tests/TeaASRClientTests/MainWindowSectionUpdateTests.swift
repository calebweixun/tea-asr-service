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
        // 五項：總覽／操作／設定／診斷與權限／日誌（見 LogsSectionTests）。
        XCTAssertEqual(MainWindowController.Section.allCases.count, 5)
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

    /// 驗證音訊電平監看在進入設定頁時啟動、切換到其他頁面停止、視窗關閉時停止。
    @MainActor
    func testAudioLevelMonitorLifecycleAcrossSectionSwitchAndWindowClose() {
        let controller = makeController()

        // 預設為 overview，monitor 應處於停止 (idle) 狀態
        XCTAssertEqual(controller.debugAudioLevelMonitor.state, .idle)

        // 切換至設定頁，monitor 應啟動（非 idle 或已回報權限/失敗狀態）
        controller.show(section: .settings)
        XCTAssertGreaterThanOrEqual(controller.debugMonitorStartCount, 1)

        // 切換至操作頁，離開設定頁後 monitor 應停止
        controller.show(section: .operations)
        XCTAssertEqual(controller.debugAudioLevelMonitor.state, .idle)

        // 再次切換回設定頁，monitor 應重新啟動
        let countBeforeReturn = controller.debugMonitorStartCount
        controller.show(section: .settings)
        XCTAssertGreaterThan(controller.debugMonitorStartCount, countBeforeReturn)

        // 視窗關閉時，monitor 應停止
        controller.windowWillClose(Notification(name: NSWindow.willCloseNotification))
        XCTAssertEqual(controller.debugAudioLevelMonitor.state, .idle)
    }

    /// 驗證在設定頁中切換輸入裝置或聲道時，電平監看會重新啟動。
    @MainActor
    func testAudioLevelMonitorRestartsOnInputDeviceAndChannelSelection() {
        let controller = makeController()
        controller.show(section: .settings)

        guard let mountedView = controller.debugMountedSectionView else {
            XCTFail("設定頁應已載入視圖")
            return
        }

        // 尋找 inputDevice 與 inputChannel 控制項
        guard let inputDevicePopup: NSPopUpButton = findView(identifier: "inputDevice", in: mountedView),
              let inputChannelPopup: NSPopUpButton = findView(identifier: "inputChannel", in: mountedView) else {
            XCTFail("找不到 inputDevice 或 inputChannel 控制項")
            return
        }

        // 切換輸入裝置時重啟 monitor
        let countBeforeDeviceChange = controller.debugMonitorStartCount
        if let target = inputDevicePopup.target, let action = inputDevicePopup.action {
            _ = target.perform(action, with: inputDevicePopup)
        }
        XCTAssertGreaterThan(controller.debugMonitorStartCount, countBeforeDeviceChange, "切換輸入裝置應重啟電平監看")

        // 切換聲道時重啟 monitor
        let countBeforeChannelChange = controller.debugMonitorStartCount
        if let target = inputChannelPopup.target, let action = inputChannelPopup.action {
            _ = target.perform(action, with: inputChannelPopup)
        }
        XCTAssertGreaterThan(controller.debugMonitorStartCount, countBeforeChannelChange, "切換聲道應重啟電平監看")
    }

    /// 驗證設定頁十二個 identifier 依然完整保留（第十一個是
    /// `serviceExecutable`，用來手動指定 tea-asr 執行檔路徑；第十二個是
    /// `stopServiceOnQuit`，決定結束 app 時要不要一併停掉本 app 啟動的
    /// 服務行程），且包含 AudioLevelBarView。
    @MainActor
    func testSettingsViewRetainsTwelveIdentifiersAndIncludesAudioLevelBar() {
        let controller = makeController()
        controller.show(section: .settings)

        guard let mountedView = controller.debugMountedSectionView else {
            XCTFail("設定頁應已載入視圖")
            return
        }

        let requiredIdentifiers = [
            "host", "port", "token", "autoInsert", "preview",
            "inputDevice", "inputChannel", "shortcut", "interactionMode", "feedback",
            "serviceExecutable", "stopServiceOnQuit"
        ]
        XCTAssertEqual(requiredIdentifiers.count, 12)

        for id in requiredIdentifiers {
            let found = findView(identifier: id, in: mountedView) as NSView?
            XCTAssertNotNil(found, "設定頁必須保留 identifier: \(id)")
        }

        // 檢查是否包含 AudioLevelBarView
        var foundBar: AudioLevelBarView?
        func searchBar(_ view: NSView) {
            if let bar = view as? AudioLevelBarView {
                foundBar = bar
            }
            view.subviews.forEach(searchBar)
        }
        searchBar(mountedView)
        XCTAssertNotNil(foundBar, "音訊輸入群組中必須包含 AudioLevelBarView")
        XCTAssertTrue(controller.debugLabelTexts().contains("輸入電平"), "設定頁應有「輸入電平」標籤")
    }

    /// 驗證 ASR 開始時電平監看停止，ASR 結束回到 idle 時電平監看重啟。
    @MainActor
    func testAudioLevelMonitorStopsWhenASRStartsAndRestartsWhenIdle() {
        let appState = AppState()
        let controller = makeController(appState: appState)
        controller.show(section: .settings)

        // 開始 ASR（模式非 idle）
        appState.setMode(.dictation)
        controller.refresh()
        XCTAssertEqual(controller.debugAudioLevelMonitor.state, .idle, "ASR 開始時 monitor 必須停止")

        // ASR 結束回到 idle
        let countBeforeIdle = controller.debugMonitorStartCount
        appState.setMode(.idle)
        controller.refresh()
        XCTAssertGreaterThan(controller.debugMonitorStartCount, countBeforeIdle, "ASR 結束且在設定頁時 monitor 應重啟")
    }

    @MainActor
    private func makeController(appState: AppState = AppState()) -> MainWindowController {
        let suiteName = "MainWindowSectionUpdateTests-\(UUID().uuidString)"
        let settings = Settings(defaults: UserDefaults(suiteName: suiteName)!)
        let permissions = PermissionCoordinator(
            platform: FakeSectionUpdateTestPlatform(),
            autoInsert: true,
            requiresInputMonitoring: false
        )
        return MainWindowController(settings: settings, appState: appState, permissions: permissions)
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
