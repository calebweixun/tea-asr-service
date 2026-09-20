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
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tempDirectory)
    }

    // MARK: - Settings page: button state reflects the real process state

    @MainActor
    func testButtonShowsStartAndIsEnabledWhenNothingIsRunningAndTheExecutableIsFound() throws {
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
        XCTAssertEqual(button.title, "啟動服務")
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
    func testButtonShowsStopWhenThisControllerHasALiveManagedProcess() throws {
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
        XCTAssertEqual(button.title, "停止服務")
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

    // MARK: - Clicking the button actually starts/stops the exact process

    @MainActor
    func testClickingStartLaunchesTheConfiguredExecutableAndTheButtonBecomesStop() throws {
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
        XCTAssertEqual(button.title, "停止服務")
    }

    @MainActor
    func testClickingStopTerminatesOnlyTheProcessThisControllerLaunched() throws {
        let script = try makeExecutableScript(body: "#!/bin/sh\nsleep 5\n")
        let controller = makeController(serviceExecutable: script.path)
        let process = Process()
        process.executableURL = script
        try process.run()
        let managed = ManagedProcess(process: process, outputCapacity: 100)
        controller.debugManagedService = managed
        controller.show(section: .settings)

        guard
            let mounted = controller.debugMountedSectionView,
            let button: NSButton = findView(identifier: "serviceControlButton", in: mounted),
            let target = button.target, let action = button.action
        else {
            XCTFail("找不到服務控制按鈕")
            return
        }
        XCTAssertEqual(button.title, "停止服務")
        _ = target.perform(action, with: button)

        process.waitUntilExit()
        XCTAssertFalse(managed.isRunning, "stop must terminate the exact tracked process")
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
