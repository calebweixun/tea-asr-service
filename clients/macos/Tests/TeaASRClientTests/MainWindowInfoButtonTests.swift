import XCTest
@testable import TeaASRClient

/// Covers the part of the "explanations behind an info button" change that
/// lives in `MainWindowController` itself: the Settings page's connection
/// hints and each Diagnostics permission row's "why" text moved off standing
/// labels and onto `InfoButton`s, while every genuine status/warning stayed
/// on a plain, always-visible label. See `InfoButtonTests` for the button's
/// own behaviour, and `ServiceControlTests`/`ExecutableDiscoveryTests` for
/// part B (finding `tea-asr`).
final class MainWindowInfoButtonTests: XCTestCase {
    @MainActor
    func testSettingsPageNoLongerRendersTheAddressTokenOrLanExplanationsAsStandingLabels() {
        let controller = makeController()
        controller.show(section: .settings)
        let texts = controller.debugLabelTexts()

        // These three used to be full-time NSTextField content on the
        // Settings page. They must not appear as rendered label text any
        // more — only inside an InfoButton's popover (checked below).
        XCTAssertFalse(
            texts.contains(where: { $0.contains("只有在要連到另一台主機上執行的服務時才需要修改") }),
            "the address explanation must no longer be a standing label"
        )
        XCTAssertFalse(
            texts.contains(where: { $0.contains("貿然開放會讓區網內任何裝置未經驗證就能存取語音與逐字稿") }),
            "the LAN explanation must no longer be a standing label"
        )
        XCTAssertFalse(
            texts.contains(where: { $0.contains("Token 只寫入") }),
            "the token storage explanation must no longer be a standing label"
        )
        XCTAssertFalse(
            texts.contains(where: { $0.contains("按一下快捷鍵按鈕即可修改") }),
            "the shortcut usage tip must no longer be appended to the status label"
        )
    }

    @MainActor
    func testSettingsPageStillDiscloseTheSameExplanationsThroughInfoButtons() {
        let controller = makeController()
        controller.show(section: .settings)
        guard let mounted = controller.debugMountedSectionView else {
            XCTFail("settings section must be mounted")
            return
        }
        let explanations = allInfoButtonExplanations(in: mounted)

        XCTAssertTrue(explanations.contains { $0.contains("只有在要連到另一台主機上執行的服務時才需要修改") })
        XCTAssertTrue(explanations.contains { $0.contains("貿然開放會讓區網內任何裝置未經驗證就能存取語音與逐字稿") })
        XCTAssertTrue(explanations.contains { $0.contains("Token 只寫入") })
        XCTAssertTrue(explanations.contains { $0.contains("按一下快捷鍵按鈕即可修改") })
    }

    /// The shortcut status label is a real warning surface (it turns red on
    /// a registration failure/conflict) and must keep reporting the live
    /// status text on its own, even though the static usage tip moved out.
    @MainActor
    func testShortcutStatusLabelStillShowsLiveStatusAfterMovingTheUsageTipToAnInfoButton() {
        let controller = makeController()
        controller.show(section: .settings)
        controller.setShortcutStatus("已註冊：⌘⌥D")
        XCTAssertTrue(controller.debugLabelTexts().contains("已註冊：⌘⌥D"))
    }

    @MainActor
    func testDiagnosticsPermissionRowsNoLongerRenderTheirExplanationAsAStandingLabelButStillShowStatus() {
        let controller = makeController()
        controller.show(section: .diagnostics)
        let texts = controller.debugLabelTexts()

        XCTAssertFalse(
            texts.contains(where: { $0.contains(PermissionKind.microphone.explanation) }),
            "why microphone access is needed must no longer be a standing label"
        )
        // The satisfied/denied status itself is not explanatory copy — it is
        // exactly the kind of thing the user must see without a click, so it
        // must still be present verbatim.
        XCTAssertTrue(
            texts.contains(where: { $0.hasPrefix(PermissionKind.microphone.title) && $0.contains("·") }),
            "the permission status line itself must stay visible"
        )
    }

    @MainActor
    func testDiagnosticsPermissionRowsStillDiscloseTheirExplanationThroughAnInfoButton() {
        let controller = makeController()
        controller.show(section: .diagnostics)
        guard let mounted = controller.debugMountedSectionView else {
            XCTFail("diagnostics section must be mounted")
            return
        }
        let explanations = allInfoButtonExplanations(in: mounted)
        XCTAssertTrue(explanations.contains(PermissionKind.microphone.explanation))
    }

    // MARK: - Part B: locating tea-asr from the Settings page

    @MainActor
    func testSettingsPageReportsWhyTheServiceCouldNotBeFoundNotJustThatItCannotConnect() {
        let suiteName = "MainWindowInfoButtonTests-\(UUID().uuidString)"
        let settings = Settings(defaults: UserDefaults(suiteName: suiteName)!)
        settings.serviceExecutable = "/nonexistent/path/tea-asr"
        let controller = MainWindowController(
            settings: settings,
            appState: AppState(),
            permissions: PermissionCoordinator(
                platform: FakeInfoButtonTestPlatform(),
                autoInsert: true,
                requiresInputMonitoring: false
            )
        )
        controller.show(section: .settings)
        let texts = controller.debugLabelTexts()
        XCTAssertTrue(
            texts.contains(where: { $0.contains("找不到 tea-asr 執行檔") }),
            "must name the actual cause instead of a generic connection failure"
        )
        XCTAssertTrue(
            texts.contains(where: { $0.contains("/nonexistent/path/tea-asr") }),
            "must say which path it tried"
        )
    }

    @MainActor
    func testServiceExecutableFieldPersistsThroughSaveSettings() {
        let suiteName = "MainWindowInfoButtonTests-\(UUID().uuidString)"
        let settings = Settings(defaults: UserDefaults(suiteName: suiteName)!)
        let controller = MainWindowController(
            settings: settings,
            appState: AppState(),
            permissions: PermissionCoordinator(
                platform: FakeInfoButtonTestPlatform(),
                autoInsert: true,
                requiresInputMonitoring: false
            )
        )
        controller.show(section: .settings)
        guard
            let mounted = controller.debugMountedSectionView,
            let field: NSTextField = findView(identifier: "serviceExecutable", in: mounted)
        else {
            XCTFail("expected a serviceExecutable field on the settings page")
            return
        }
        field.stringValue = "/opt/homebrew/bin/tea-asr"
        _ = field.target?.perform(field.action, with: field)
        XCTAssertEqual(settings.serviceExecutable, "/opt/homebrew/bin/tea-asr")
    }

    // MARK: - Helpers

    @MainActor
    private func allInfoButtonExplanations(in root: NSView) -> [String] {
        var result: [String] = []
        func visit(_ view: NSView) {
            if let button = view as? InfoButton {
                result.append(button.explanation)
            }
            view.subviews.forEach(visit)
        }
        visit(root)
        return result
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
    private func makeController(appState: AppState = AppState()) -> MainWindowController {
        let suiteName = "MainWindowInfoButtonTests-\(UUID().uuidString)"
        let settings = Settings(defaults: UserDefaults(suiteName: suiteName)!)
        let permissions = PermissionCoordinator(
            platform: FakeInfoButtonTestPlatform(),
            autoInsert: true,
            requiresInputMonitoring: false
        )
        return MainWindowController(settings: settings, appState: appState, permissions: permissions)
    }
}

private struct FakeInfoButtonTestPlatform: PermissionPlatform {
    var microphoneAuthorization: PermissionAuthorization { .denied }
    var accessibilityTrusted: Bool { false }
    var inputMonitoringAuthorized: Bool { false }
    func requestMicrophoneAccess(completion: @escaping (Bool) -> Void) { completion(false) }
    @discardableResult
    func promptAccessibility() -> Bool { false }
    @discardableResult
    func promptInputMonitoring() -> Bool { false }
    @discardableResult
    func openSettings(for kind: PermissionKind) -> Bool { true }
}
