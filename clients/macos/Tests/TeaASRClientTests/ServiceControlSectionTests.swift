import XCTest
@testable import TeaASRClient

/// Covers the Settings page's "本機服務" service start/stop control and
/// model-info row, plus the Logs page's "服務輸出" tab — the pieces added
/// on top of the existing executable-path field. `debugManagedService`/
/// `debugModelPrepareProcess` let a test install a real `ManagedProcess`
/// (wrapping a short-lived helper script) without going through the actual
/// `tea-asr` executable search, the same way `LogsSectionTests` substitutes
/// `LogsFetching` instead of hitting the network.
final class ServiceControlSectionTests: XCTestCase {
    private var tempDirectory: URL!

    override func setUpWithError() throws {
        tempDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ServiceControlSectionTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDirectory, withIntermediateDirectories: true)
        // Defensive: `ServiceControl.lastManagedServeProcess` is shared,
        // static state (see its doc comment), so a leftover handle from an
        // earlier test — in this suite or another — must never leak into a
        // test that expects nothing to be managed yet.
        ServiceControl.lastManagedServeProcess = nil
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tempDirectory)
        ServiceControl.lastManagedServeProcess = nil
    }

    // MARK: - Settings page: button state reflects the real process state

    @MainActor
    func testButtonShowsRestartAndIsEnabledWhenNothingIsRunningAndTheExecutableIsFound() throws {
        let executable = try makeExecutableScript(body: "#!/bin/sh\nexit 0\n")
        let controller = makeController(serviceExecutable: executable.path)
        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let button: NSButton = findView(identifier: "serviceControlButton", in: mounted)
        else {
            XCTFail("找不到服務控制按鈕")
            return
        }
        XCTAssertEqual(button.title, "重新啟動服務")
        XCTAssertTrue(button.isEnabled)
    }

    @MainActor
    func testButtonIsDisabledWhenTheExecutableCannotBeFound() {
        let controller = makeController(serviceExecutable: "/definitely/not/a/real/path/tea-asr")
        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let button: NSButton = findView(identifier: "serviceControlButton", in: mounted)
        else {
            XCTFail("找不到服務控制按鈕")
            return
        }
        XCTAssertFalse(button.isEnabled)
        XCTAssertTrue(
            controller.debugLabelTexts().contains(where: { $0.contains("找不到執行檔") }),
            "the disabled reason must be visible, not just the disabled state"
        )
    }

    @MainActor
    func testButtonShowsRestartWhenThisControllerHasALiveManagedProcess() throws {
        let script = try makeExecutableScript(body: "#!/bin/sh\nsleep 5\n")
        let controller = makeController(serviceExecutable: script.path)
        let process = Process()
        process.executableURL = script
        try process.run()
        defer { process.terminate() }
        controller.debugManagedService = ManagedProcess(process: process, outputCapacity: 100)

        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let button: NSButton = findView(identifier: "serviceControlButton", in: mounted)
        else {
            XCTFail("找不到服務控制按鈕")
            return
        }
        XCTAssertEqual(button.title, "重新啟動服務")
        XCTAssertTrue(button.isEnabled)
    }

    /// A reachable service this controller did not itself launch must not
    /// offer to start (a duplicate) or stop (something it never launched).
    @MainActor
    func testButtonIsDisabledWhenServiceIsReachableButNotManagedByThisController() throws {
        let executable = try makeExecutableScript(body: "#!/bin/sh\nexit 0\n")
        let appState = AppState()
        appState.updateService(.success(Self.fakeSnapshot()))
        let controller = makeController(serviceExecutable: executable.path, appState: appState)
        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let button: NSButton = findView(identifier: "serviceControlButton", in: mounted)
        else {
            XCTFail("找不到服務控制按鈕")
            return
        }
        XCTAssertFalse(button.isEnabled)
        XCTAssertTrue(
            controller.debugLabelTexts().contains(where: { $0.contains("不是由這個頁面啟動") })
        )
    }

    /// `canRestartManagedService` is what the menu bar's "重新啟動服務" item
    /// reads to decide its own enabled state, so it must agree with the
    /// Settings page button above about the one case that would otherwise
    /// try to restart a process this app never launched. `restartManagedService()`
    /// itself is not invoked here: on this branch it shows a blocking
    /// `NSAlert`, so — like every other disabled-button case in this file —
    /// only the state that keeps the button (and now the menu item) disabled
    /// is asserted.
    @MainActor
    func testCanRestartMirrorsTheDisabledButtonWhenServiceIsReachableButNotManaged() throws {
        let executable = try makeExecutableScript(body: "#!/bin/sh\nexit 0\n")
        let appState = AppState()
        appState.updateService(.success(Self.fakeSnapshot()))
        let controller = makeController(serviceExecutable: executable.path, appState: appState)
        controller.show(section: .settings)

        XCTAssertFalse(controller.canRestartManagedService)
    }

    @MainActor
    func testCanRestartIsTrueAsSoonAsThisControllerHasALiveManagedProcess() throws {
        let script = try makeExecutableScript(body: "#!/bin/sh\nsleep 5\n")
        let controller = makeController(serviceExecutable: script.path)
        let process = Process()
        process.executableURL = script
        try process.run()
        defer { process.terminate() }
        controller.debugManagedService = ManagedProcess(process: process, outputCapacity: 100)

        XCTAssertTrue(controller.canRestartManagedService)
    }

    // MARK: - Clicking the button actually starts/stops the exact process

    @MainActor
    func testClickingRestartLaunchesTheConfiguredExecutableWhenNothingWasRunning() throws {
        let script = try makeExecutableScript(body: "#!/bin/sh\nsleep 5\n")
        let controller = makeController(serviceExecutable: script.path)
        controller.show(section: .settings)
        defer { controller.debugManagedService?.terminate() }

        guard
            let mounted = controller.debugMountedSectionView,
            let button: NSButton = findView(identifier: "serviceControlButton", in: mounted),
            let target = button.target, let action = button.action
        else {
            XCTFail("找不到服務控制按鈕")
            return
        }
        _ = target.perform(action, with: button)

        XCTAssertNotNil(controller.debugManagedService)
        XCTAssertTrue(controller.debugManagedService?.isRunning ?? false)
        XCTAssertEqual(button.title, "重新啟動服務")
    }

    @MainActor
    func testClickingRestartTerminatesTheOldProcessAndLaunchesAFreshOne() throws {
        let script = try makeExecutableScript(body: "#!/bin/sh\nsleep 5\n")
        let controller = makeController(serviceExecutable: script.path)
        let process = Process()
        process.executableURL = script
        try process.run()
        let managed = ManagedProcess(process: process, outputCapacity: 100)
        controller.debugManagedService = managed
        controller.show(section: .settings)
        defer { controller.debugManagedService?.terminate() }

        guard
            let mounted = controller.debugMountedSectionView,
            let button: NSButton = findView(identifier: "serviceControlButton", in: mounted),
            let target = button.target, let action = button.action
        else {
            XCTFail("找不到服務控制按鈕")
            return
        }
        XCTAssertEqual(button.title, "重新啟動服務")
        _ = target.perform(action, with: button)

        process.waitUntilExit()
        XCTAssertFalse(managed.isRunning, "restart must terminate the exact old tracked process")
        XCTAssertTrue(
            controller.debugManagedService?.isRunning ?? false,
            "restart must end with a freshly launched process running"
        )
        XCTAssertFalse(
            controller.debugManagedService === managed,
            "the fresh process must be a new handle, not the terminated one"
        )
    }

    // MARK: - Service login item (LaunchAgent), moved here from the menu bar

    /// The real probe (`refreshServiceLoginItemState()`) runs off the main
    /// thread precisely so `update()` never spawns `tea-asr service status`
    /// synchronously (see that method's own doc comment) — so this test
    /// seeds the cached result directly via `debugServiceLoginItemInstalled`
    /// rather than racing the background probe.
    @MainActor
    func testServiceLoginItemCheckboxIsOffWhenNoAgentIsInstalled() throws {
        let executable = try makeExecutableScript(body: "#!/bin/sh\nexit 0\n")
        let controller = makeController(serviceExecutable: executable.path)
        controller.debugServiceLoginItemInstalled = false
        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let checkbox: NSButton = findView(identifier: "serviceLoginItem", in: mounted)
        else {
            XCTFail("找不到登入時自動啟動服務的勾選框")
            return
        }
        XCTAssertEqual(checkbox.state, .off)
        XCTAssertTrue(checkbox.isEnabled)
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("尚未設定") }))
    }

    @MainActor
    func testServiceLoginItemCheckboxIsOnWhenAgentIsAlreadyInstalled() throws {
        let executable = try makeExecutableScript(body: "#!/bin/sh\nexit 0\n")
        let controller = makeController(serviceExecutable: executable.path)
        controller.debugServiceLoginItemInstalled = true
        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let checkbox: NSButton = findView(identifier: "serviceLoginItem", in: mounted)
        else {
            XCTFail("找不到登入時自動啟動服務的勾選框")
            return
        }
        XCTAssertEqual(checkbox.state, .on)
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("已設定") }))
    }

    @MainActor
    func testServiceLoginItemCheckboxIsDisabledWhenTheExecutableCannotBeFound() {
        let controller = makeController(serviceExecutable: "/definitely/not/a/real/path/tea-asr")
        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let checkbox: NSButton = findView(identifier: "serviceLoginItem", in: mounted)
        else {
            XCTFail("找不到登入時自動啟動服務的勾選框")
            return
        }
        XCTAssertFalse(checkbox.isEnabled)
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("找不到執行檔") }))
    }

    @MainActor
    func testClickingServiceLoginItemInstallsThenUninstallsTheLaunchAgent() throws {
        let executable = try makeServiceAgentScript()
        let controller = makeController(serviceExecutable: executable.path)
        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let checkbox: NSButton = findView(identifier: "serviceLoginItem", in: mounted),
            let target = checkbox.target, let action = checkbox.action
        else {
            XCTFail("找不到登入時自動啟動服務的勾選框")
            return
        }

        _ = target.perform(action, with: checkbox)
        XCTAssertEqual(checkbox.state, .on, "clicking while uninstalled must install the LaunchAgent")
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("已設定") }))

        _ = target.perform(action, with: checkbox)
        XCTAssertEqual(checkbox.state, .off, "clicking again while installed must uninstall it")
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("尚未設定") }))
    }

    /// A fake `tea-asr` whose `service status/install/uninstall` behave like
    /// the real CLI (`agent_installed` in the JSON status, a flag file
    /// standing in for the real LaunchAgent plist) without touching
    /// `launchctl` or any real login-item state.
    private func makeServiceAgentScript() throws -> URL {
        let flag = tempDirectory.appendingPathComponent("agent-installed").path
        let script = """
        #!/bin/sh
        case "$1 $2" in
          "service status")
            if [ -f "\(flag)" ]; then
              echo '{"agent_installed": true}'
            else
              echo '{"agent_installed": false}'
            fi
            ;;
          "service install")
            touch "\(flag)"
            echo installed
            ;;
          "service uninstall")
            rm -f "\(flag)"
            echo uninstalled
            ;;
          *)
            exit 1
            ;;
        esac
        """
        return try makeExecutableScript(body: script)
    }

    // MARK: - Model info row

    @MainActor
    func testModelInfoShowsNotYetAvailableWhenTheServiceHasNeverBeenProbed() {
        let controller = makeController(serviceExecutable: "")
        controller.show(section: .settings)
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("尚未取得") }))
    }

    @MainActor
    func testModelInfoShowsTheModelNameAndRevisionFromTheLastStatusProbe() {
        let appState = AppState()
        appState.updateService(.success(Self.fakeSnapshot(model: "TEA-ASR-1.1-mini-mlx", revision: "abc123")))
        let controller = makeController(serviceExecutable: "", appState: appState)
        controller.show(section: .settings)

        XCTAssertTrue(controller.debugLabelTexts().contains(where: {
            $0.contains("TEA-ASR-1.1-mini-mlx") && $0.contains("abc123")
        }))
    }

    // MARK: - Logs page: 服務輸出 tab

    @MainActor
    func testServiceOutputTabExplainsWhenTheServiceIsRunningButNotManagedByThisApp() {
        let appState = AppState()
        appState.updateService(.success(Self.fakeSnapshot()))
        let controller = makeController(serviceExecutable: "", appState: appState)
        controller.show(section: .logs)

        selectLogsTab(.serviceOutput, in: controller)
        XCTAssertEqual(controller.debugServiceOutputText, "尚無輸出。", "no process handle means no body to render")
        XCTAssertTrue(
            controller.debugLabelTexts().contains("服務不是由這個 app 啟動，看不到它的輸出。")
        )
    }

    @MainActor
    func testServiceOutputTabRendersTheManagedProcessOutput() throws {
        let script = try makeExecutableScript(body: "#!/bin/sh\nsleep 5\n")
        let controller = makeController(serviceExecutable: script.path)
        let process = Process()
        process.executableURL = script
        try process.run()
        defer { process.terminate() }
        let managed = ManagedProcess(process: process, outputCapacity: 3)
        managed.output.append("line1\nline2\nline3\nline4\n")
        controller.debugManagedService = managed

        controller.show(section: .logs)
        selectLogsTab(.serviceOutput, in: controller)

        // Only the most recent 3 lines survive the buffer's cap, and the
        // drop must be visible in the rendered text.
        XCTAssertEqual(controller.debugServiceOutputText?.contains("已捨棄 1 行"), true)
        XCTAssertEqual(controller.debugServiceOutputText?.contains("line2"), true)
        XCTAssertEqual(controller.debugServiceOutputText?.contains("line4"), true)
        XCTAssertEqual(controller.debugServiceOutputText?.contains("line1"), false, "line1 must have been evicted")
    }

    @MainActor
    func testServiceOutputTabSuggestsStartingWhenNothingIsRunningOrManaged() {
        let controller = makeController(serviceExecutable: "")
        controller.show(section: .logs)
        selectLogsTab(.serviceOutput, in: controller)

        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("尚未啟動服務") }))
    }

    /// The actual bug report end-to-end: a service started from the menu
    /// bar goes through `ServiceControl.start(executable:)`, never through
    /// this controller's own `toggleManagedService`, so `debugManagedService`
    /// (this window's local `managedService`) is `nil` here — exactly as it
    /// would be for a real menu-bar-started service. The Logs page must
    /// still render live output by falling back to
    /// `ServiceControl.lastManagedServeProcess`, the shared handle `start`
    /// now populates.
    @MainActor
    func testServiceOutputTabRendersOutputForAServiceStartedViaTheMenuBarsFireAndForgetPath() throws {
        let script = try makeExecutableScript(body: "#!/bin/sh\necho 'from the menu bar'\nsleep 5\n")
        try ServiceControl.start(executable: script)
        defer { ServiceControl.lastManagedServeProcess?.terminate() }

        let controller = makeController(serviceExecutable: script.path)
        XCTAssertNil(controller.debugManagedService, "this window never touched ServiceControl.start itself")

        let deadline = Date().addingTimeInterval(3)
        while ServiceControl.lastManagedServeProcess?.output.snapshot().lines.isEmpty != false
            && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }

        controller.show(section: .logs)
        selectLogsTab(.serviceOutput, in: controller)

        XCTAssertEqual(controller.debugServiceOutputText?.contains("from the menu bar"), true)
        XCTAssertFalse(controller.debugLabelTexts().contains(where: { $0.contains("尚未啟動服務") }))
    }

    // MARK: - Helpers

    @discardableResult
    private func makeExecutableScript(body: String) throws -> URL {
        let url = tempDirectory.appendingPathComponent("tea-asr")
        try body.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        return url
    }

    private static func fakeSnapshot(model: String = "TEA-ASR-1.1-mini-mlx", revision: String = "rev") -> ServiceSnapshot {
        ServiceSnapshot(
            healthzOK: true,
            readyzOK: true,
            readyState: "ready",
            status: ServerStatus(
                modelState: "ready",
                model: model,
                modelRevision: revision,
                workerGeneration: 1,
                workerLoadMs: 100,
                lastError: nil,
                idleS: 0,
                activeSessions: 0,
                queue: QueueStatus(waitingTasks: 0, waitingSamples: 0, maxWaitingTasks: 10, maxWaitingSamples: 10)
            ),
            capabilities: Capabilities(
                protocolVersion: "1",
                audio: CapabilityAudio(sampleRate: 16000, channels: 1, format: "pcm_s16le"),
                profiles: [],
                features: CapabilityFeatures(
                    nativeAudioStreaming: true,
                    partialTranscripts: true,
                    wordTimestamps: false,
                    translation: false,
                    diarization: false,
                    hotwords: false,
                    contextBiasing: false,
                    durableSessions: false,
                    durableRevisable: false,
                    batchJobs: false
                ),
                limits: CapabilityLimits(
                    maxFramePCMBytes: 1,
                    maxUtteranceMs: 1,
                    maxContinuousSessions: 1,
                    maxTotalConnections: 1
                )
            )
        )
    }

    @MainActor
    private func selectLogsTab(_ tab: Int, in controller: MainWindowController) {
        guard
            let mounted = controller.debugMountedSectionView,
            let segmented: NSSegmentedControl = findView(identifier: "logsTab", in: mounted),
            let target = segmented.target, let action = segmented.action
        else {
            XCTFail("找不到日誌分頁控制項")
            return
        }
        segmented.selectedSegment = tab
        _ = target.perform(action, with: segmented)
    }

    @MainActor
    private func makeController(
        serviceExecutable: String,
        appState: AppState = AppState()
    ) -> MainWindowController {
        let suiteName = "ServiceControlSectionTests-\(UUID().uuidString)"
        let settings = Settings(defaults: UserDefaults(suiteName: suiteName)!)
        settings.serviceExecutable = serviceExecutable
        let permissions = PermissionCoordinator(
            platform: FakeServiceControlSectionTestPlatform(),
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

private extension Int {
    static var structured: Int { 0 }
    static var serviceOutput: Int { 1 }
}

private struct FakeServiceControlSectionTestPlatform: PermissionPlatform {
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
