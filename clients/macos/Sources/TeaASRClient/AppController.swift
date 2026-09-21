import AppKit
import Carbon.HIToolbox
import ServiceManagement

/// Whether this *app* (as opposed to the local service) opens automatically
/// at login, via `SMAppService.mainApp` — the macOS 13+ login-item registry,
/// not a hand-installed LaunchAgent plist. Kept behind a protocol so the
/// menu item's toggle can be tested without touching the real login-item
/// registry.
protocol LoginItemService {
    var isRegistered: Bool { get }
    func register() throws
    func unregister() throws
}

/// Production implementation, backed by `SMAppService.mainApp`.
struct AppLoginItemService: LoginItemService {
    var isRegistered: Bool { SMAppService.mainApp.status == .enabled }
    func register() throws { try SMAppService.mainApp.register() }
    func unregister() throws { try SMAppService.mainApp.unregister() }
}

/// Toggles a `LoginItemService`: always the opposite of its current state,
/// regardless of *why* it is currently off (never registered, or disabled by
/// the user from System Settings' Login Items pane).
enum LoginItemToggle {
    @discardableResult
    static func toggle(_ service: LoginItemService) -> Result<Void, Error> {
        do {
            if service.isRegistered {
                try service.unregister()
            } else {
                try service.register()
            }
            return .success(())
        } catch {
            return .failure(error)
        }
    }
}

/// Chooses the section shown when macOS launches or re-opens the app.
///
/// The menu bar action can still explicitly select a section, but Finder/open
/// and a second open request should always reveal the same management window.
/// Keeping this policy separate also makes the permission-first launch path
/// easy to verify without constructing AppKit windows in tests.
///
/// The permission checklist now lives inside Diagnostics (it is no longer a
/// sidebar destination of its own), so "launch where the user can fix the
/// missing permission" means Diagnostics.
enum MainWindowLaunchPolicy {
    static func section(requiredPermissionsGranted: Bool) -> MainWindowController.Section {
        requiredPermissionsGranted ? .overview : .diagnostics
    }
}

/// Loads a menu-bar template by its base resource name so AppKit can discover
/// the matching @2x and @3x representations in the bundle. Loading one PNG by
/// URL would keep only the 1x representation and make the Retina icon blurry.
enum MenuBarImageLoader {
    static let logicalSize = NSSize(width: 18, height: 18)

    static func image(state: String, bundle: Bundle = .main) -> NSImage? {
        guard let image = bundle.image(forResource: NSImage.Name("MenuBar-\(state)")) else {
            return nil
        }
        image.isTemplate = true
        image.size = logicalSize
        return image
    }
}

/// Gives an image-only status item a stable, non-zero capture width.
///
/// Keep this independent of the image load result: Thaw needs a valid window
/// geometry even during the brief interval before the first image is assigned.
enum MenuBarStatusItemSizing {
    static let length = max(MenuBarImageLoader.logicalSize.width, 1)
}

/// Menu bar app: dictation into the focused app, or a meeting transcript window.
@MainActor
final class AppController: NSObject, NSApplicationDelegate {
    private enum Mode {
        case idle
        case dictation
        case meeting
    }

    private let settings = Settings()
    private let capture = AudioCapture()
    private lazy var client = ASRClient(settings: settings)
    private let transcriptEvents = TranscriptEventProcessor()
    private let appState = AppState()
    private let serviceProbe = ServiceProbe()

    private var statusItem: NSStatusItem!
    private let menu = NSMenu()
    private let statusEntry = NSMenuItem(title: "未啟動", action: nil, keyEquivalent: "")
    private let toggleEntry = NSMenuItem()
    private let meetingEntry = NSMenuItem()
    private let autoInsertEntry = NSMenuItem()
    private let previewEntry = NSMenuItem()
    private var warnedAboutAccessibility = false
    private let serviceEntry = NSMenuItem()
    private let loginItemEntry = NSMenuItem()
    private let loginItem: LoginItemService = AppLoginItemService()
    private var healthTimer: Timer?
    private var accessibilityHintTask: Task<Void, Never>?

    private var mode: Mode = .idle
    private var mainWindow: MainWindowController?
    private var permissionCoordinator: PermissionCoordinator?
    private var hotKeyRef: EventHotKeyRef?
    private var hotKeyEventHandler: EventHandlerRef?
    private var globalKeyUpMonitor: Any?
    private var localKeyUpMonitor: Any?
    private var hotKeyRegistered = false
    private var hotKeyRegistrationOutcome: ShortcutRegistrationOutcome = .unavailable
    /// Guards `autoStartServiceIfNeeded()` so it only ever runs the one time
    /// this app is launched, never again on `applicationDidBecomeActive` or
    /// any other later re-entry into launch-adjacent code.
    private var didAttemptServiceAutoStart = false
    private var interactionMachine = DictationInteractionStateMachine(mode: .toggle)
    /// Drops shortcut presses that arrive while a previous start is still
    /// waiting on microphone consent (see `SessionStartGate`).
    private var startGate = SessionStartGate()
    private let dictationOverlay = DictationOverlayController()
    private var workspaceObservers: [NSObjectProtocol] = []
    private var insertedDictationSegments = Set<String>()
    private var dictationInsertionTarget: TextInsertionTarget?
    /// Wall clock of the current session's sample 0, so a session restarted after
    /// a timeline gap still lands on one continuous meeting timeline.
    private var sessionOrigin = Date()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        buildMenu()
        let permissions = PermissionCoordinator()
        permissionCoordinator = permissions
        permissions.updateInteractionRequirement(settings.interactionMode == .pushToTalk)
        let window = MainWindowController(settings: settings, appState: appState, permissions: permissions)
        permissions.updateAutoInsertRequirement(settings.autoInsert)
        window.onStartDictation = { [weak self] in self?.beginDictationFromUI() }
        window.onStartMeeting = { [weak self] in self?.toggleMeeting() }
        window.onStopSession = { [weak self] in self?.stopSession() }
        window.onRefreshService = { [weak self] in self?.refreshServiceState() }
        window.onShortcutEditorWillBegin = { [weak self] in
            self?.pauseHotKeyForShortcutEditor()
        }
        window.onShortcutEditorDidEnd = { [weak self] in
            self?.applyInteractionSettings()
        }
        window.onSettingsChanged = { [weak self] in
            guard let self else { return }
            self.permissionCoordinator?.updateAutoInsertRequirement(self.settings.autoInsert)
            self.applyInteractionSettings()
        }
        mainWindow = window
        wireClient()
        appState.onChange = { [weak self] in
            self?.mainWindow?.refresh()
            self?.render()
        }
        interactionMachine = DictationInteractionStateMachine(mode: settings.interactionMode)
        registerHotKey()
        installWorkspaceObservers()
        refreshServiceState()
        autoStartServiceIfNeeded()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.refreshServiceState()
            }
        }
        render()
        window.show(
            section: MainWindowLaunchPolicy.section(
                requiredPermissionsGranted: permissions.state.requiredPermissionsGranted
            )
        )
    }

    /// Finder/open sends a reopen request to an already-running accessory app.
    /// Reuse the controller created at launch so there is one management window
    /// even if the user opens the app repeatedly.
    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows flag: Bool
    ) -> Bool {
        guard let window = mainWindow else { return false }
        let requiredPermissionsGranted = permissionCoordinator?.state.requiredPermissionsGranted ?? true
        window.show(
            section: MainWindowLaunchPolicy.section(
                requiredPermissionsGranted: requiredPermissionsGranted
            )
        )
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        accessibilityHintTask?.cancel()
        workspaceObservers.forEach(NSWorkspace.shared.notificationCenter.removeObserver)
        workspaceObservers.removeAll()
        removeKeyUpMonitors()
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let hotKeyEventHandler {
            RemoveEventHandler(hotKeyEventHandler)
            self.hotKeyEventHandler = nil
        }
        capture.stop()
        client.cancel()
        dictationOverlay.hide()
        stopManagedService()
    }

    /// This app is the service's main runtime, so stopping it on quit is
    /// unconditional — there is no setting to turn it off. That is only ever
    /// safe for the exact `Process` this app launched from the Settings page
    /// and still holds a handle to. A LaunchAgent, a terminal-launched
    /// service, and even this app's own menu-bar `重新啟動服務` (which keeps
    /// no handle) are all invisible here and are therefore left running —
    /// nothing is ever matched by process name.
    ///
    /// Only reachable from `applicationWillTerminate`: a `SIGKILL`
    /// (Force Quit, `kill -9`) or a crash skips it entirely and leaves the
    /// service up. That is stated in the Settings page's own help text
    /// rather than papered over.
    private func stopManagedService() {
        let managed = mainWindow?.managedServiceProcess
        let action = ServiceQuitPolicy.action(
            hasManagedProcess: managed != nil,
            managedProcessIsRunning: managed?.isRunning == true
        )
        guard action == .stopManagedProcess, let managed else { return }
        managed.terminate()
        // Bounded wait so the port and the model's memory are actually
        // released before this process goes away; never an unbounded block on
        // a child that refuses to exit.
        let deadline = Date().addingTimeInterval(2.0)
        while managed.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
    }

    /// System Settings changes TCC while this process is suspended in the
    /// background. Window focus alone is not a reliable lifecycle signal (the
    /// window may stay key, or dictation may have hidden it), so always refresh
    /// on app activation and give ApplicationServices a short settling window.
    func applicationDidBecomeActive(_ notification: Notification) {
        guard let permissions = permissionCoordinator else { return }
        permissions.refreshAfterApplicationActivation()
        applyInteractionSettings()
        if settings.interactionMode == .pushToTalk,
           !permissions.state.inputMonitoring.isSatisfied,
           mode == .dictation {
            cancelInteraction(.permissionLost)
        }

        accessibilityHintTask?.cancel()
        accessibilityHintTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(nanoseconds: 1_200_000_000)
            } catch {
                return
            }
            guard !Task.isCancelled, let self else { return }
            self.applyInteractionSettings()
            guard let permissions = self.permissionCoordinator,
                  permissions.consumeAccessibilityRestartHint()
            else { return }

            self.alert(
                "仍未偵測到\(SystemPermissionNaming.accessibilityTitle)權限",
                "macOS 目前仍回報這個 TEA ASR 程序沒有\(SystemPermissionNaming.accessibilityTitle)權限。"
                    + "如果你剛在系統設定打開開關，請先完全結束 TEA ASR，再重新開啟目前的 app。"
                    + "若這是 ad-hoc 開發版，重建後可能需要在\(SystemPermissionNaming.accessibilityTitle)清單移除舊的 TEA ASR，"
                    + "再加入目前這個 build；只有 Developer ID 簽章才能讓 TCC 身分跨重建穩定。"
            )
        }
    }

    // MARK: - Menu

    private func buildMenu() {
        let initialImage = Self.menuBarImage("idle")
        statusItem = NSStatusBar.system.statusItem(
            withLength: MenuBarStatusItemSizing.length
        )
        statusItem.button?.image = initialImage
        statusItem.menu = menu

        statusEntry.isEnabled = false
        menu.addItem(statusEntry)
        menu.addItem(.separator())

        toggleEntry.action = #selector(toggleDictation)
        toggleEntry.target = self
        menu.addItem(toggleEntry)

        meetingEntry.action = #selector(toggleMeeting)
        meetingEntry.target = self
        menu.addItem(meetingEntry)

        menu.addItem(.separator())

        autoInsertEntry.title = "定稿後自動貼上"
        autoInsertEntry.action = #selector(toggleAutoInsert)
        autoInsertEntry.target = self
        menu.addItem(autoInsertEntry)

        previewEntry.title = "會議記錄顯示即時預覽"
        previewEntry.action = #selector(togglePreview)
        previewEntry.target = self
        menu.addItem(previewEntry)

        menu.addItem(.separator())
        serviceEntry.action = #selector(restartService)
        serviceEntry.target = self
        serviceEntry.title = "重新啟動服務"
        menu.addItem(serviceEntry)

        // The app itself, not the service: `SMAppService.mainApp` is a
        // macOS 13+ login item, unrelated to the service's own LaunchAgent
        // (which the Settings page's "本機服務" group now controls — see
        // `MainWindowController`'s `serviceLoginItem` checkbox). This app is
        // the service's main runtime, so having *it* open at login is the
        // thing that actually matters from the menu bar.
        loginItemEntry.title = "登入時自動開啟程式"
        loginItemEntry.action = #selector(toggleLoginItem)
        loginItemEntry.target = self
        menu.addItem(loginItemEntry)

        menu.addItem(.separator())
        // 主畫面／設定合併成一個入口：這個視窗本身有側邊欄可以切到設定頁，
        // 不需要選單列另開一個項目重複這件事。
        let mainWindowEntry = NSMenuItem(
            title: "主畫面…", action: #selector(showMainWindow), keyEquivalent: "0"
        )
        mainWindowEntry.target = self
        menu.addItem(mainWindowEntry)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "結束", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
    }

    /// The status item's original solid cup mark, drawn to read at 18pt. The
    /// bundle ships one template per state with 1x, 2x and 3x representations.
    private static var menuBarImages: [String: NSImage] = [:]

    private static func menuBarImage(_ state: String) -> NSImage? {
        if let cached = menuBarImages[state] { return cached }
        guard let image = MenuBarImageLoader.image(state: state) else {
            // Running from a plain binary rather than the built bundle.
            return NSImage(
                systemSymbolName: state == "error" ? "exclamationmark.triangle" : "cup.and.saucer",
                accessibilityDescription: "TEA ASR"
            )
        }
        menuBarImages[state] = image
        return image
    }

    private func render() {
        let state: String
        switch appState.displayStatus {
        case .failed, .retryable:
            state = "error"
        case .listening:
            state = "listening"
        default:
            state = "idle"
        }
        statusItem.button?.image = Self.menuBarImage(state)
        statusEntry.title = appState.displayStatus.title

        let dictationAction = mode == .dictation ? "停止聽寫" : "開始聽寫"
        let interactionHint = settings.interactionMode == .pushToTalk
            ? "按住 " + settings.shortcut.displayString
            : settings.shortcut.displayString
        toggleEntry.title = dictationAction + "（" + interactionHint + "）"
        toggleEntry.keyEquivalent = settings.shortcut.menuKeyEquivalent
        toggleEntry.keyEquivalentModifierMask = settings.shortcut.eventModifierFlags
        if !hotKeyRegistered {
            toggleEntry.title += " · " + shortcutStatusText()
        }
        meetingEntry.title = mode == .meeting ? "停止會議記錄" : "開始會議記錄"
        // Mirrors the Settings page's own restart button exactly (same
        // `canRestartManagedService`/`restartManagedService()`), so this
        // item is never enabled for a service reachable elsewhere that this
        // app never launched.
        serviceEntry.isEnabled = mainWindow?.canRestartManagedService ?? false
        // `SMAppService.mainApp.status` is a local system-service query, not
        // a subprocess spawn, so reading it on every render (unlike the old
        // `ServiceControl.agentInstalled` probe it replaces here) is cheap.
        loginItemEntry.state = loginItem.isRegistered ? .on : .off
        autoInsertEntry.state = settings.autoInsert ? .on : .off
        previewEntry.state = settings.revisablePreview ? .on : .off
    }

    // MARK: - Actions

    @objc private func toggleDictation() {
        if settings.interactionMode == .pushToTalk {
            mode == .dictation ? stopSession() : beginDictationFromUI()
            return
        }
        handleShortcut(.shortcutDown(isRepeat: false))
        // A menu item click has no physical key-up event. Release the latch
        // immediately so the next click remains a distinct toggle press.
        _ = interactionMachine.handle(.shortcutUp)
    }

    private func beginDictationFromUI() {
        guard mode == .idle else {
            if mode == .dictation { stopSession() }
            return
        }
        interactionMachine.beginManualSession()
        start(mode: .dictation)
    }

    @objc private func toggleMeeting() {
        mode == .meeting ? stopSession() : start(mode: .meeting)
    }

    // MARK: - Service

    private func refreshServiceState() {
        let token = try? settings.token()
        serviceProbe.refresh(host: settings.host, port: settings.port, token: token) { [weak self] result in
            guard let self else { return }
            self.appState.updateService(result)
            self.render()
        }
    }

    /// This app is the service's main runtime, and quitting it already stops
    /// whatever it launched unconditionally (see `stopManagedService`).
    /// Launching it now does the symmetric thing: bring the service up too,
    /// unless something already answers on the configured host/port — a
    /// LaunchAgent-started service, a developer's own terminal-launched one,
    /// or one left running from a previous quit-that-couldn't-stop-it.
    ///
    /// A plain `/healthz` probe (not the full `ServiceProbe` used by
    /// `refreshServiceState`/`render`) is enough here: this only needs to
    /// know "does anything answer at all", not the authenticated status or
    /// capabilities payload, and it must not wait on a token round trip
    /// before deciding whether to start anything. The probe itself is
    /// asynchronous and its completion always lands on the main queue (see
    /// `ServiceControl.probeHealth`), so this never blocks app launch.
    private func autoStartServiceIfNeeded() {
        guard !didAttemptServiceAutoStart else { return }
        didAttemptServiceAutoStart = true
        ServiceControl.probeHealth(host: settings.host, port: settings.port) { [weak self] reachable in
            self?.mainWindow?.startManagedServiceAtLaunch(alreadyReachable: reachable)
        }
    }

    /// Delegates to the exact same restart implementation the Settings
    /// page's own button uses (`MainWindowController.restartManagedService`)
    /// so there is one place that ever stops or starts the service process,
    /// not two copies of the same safety reasoning.
    @objc private func restartService() {
        mainWindow?.restartManagedService()
        render()
    }

    /// Toggles whether this app itself opens at login. Never touches the
    /// service's own LaunchAgent (see the Settings page's "本機服務" group)
    /// and never needs the `tea-asr` executable to exist at all.
    @objc private func toggleLoginItem() {
        if case .failure(let error) = LoginItemToggle.toggle(loginItem) {
            alert("設定失敗", error.localizedDescription)
        }
        render()
    }

    @objc private func showMainWindow() {
        mainWindow?.show(section: .overview)
    }

    @objc private func toggleAutoInsert() {
        settings.autoInsert.toggle()
        permissionCoordinator?.updateAutoInsertRequirement(settings.autoInsert)
        if settings.autoInsert, !TextInjector.isTrusted {
            TextInjector.requestTrust()
            alert(
                "需要\(SystemPermissionNaming.accessibilityTitle)權限",
                "要把文字貼進其他 app，得在「\(SystemPermissionNaming.accessibilitySettingsPath)」裡允許 TEA ASR。\n"
                    + "沒有這個權限時，定稿文字仍會留在剪貼簿。"
            )
        }
        render()
    }

    @objc private func togglePreview() {
        settings.revisablePreview.toggle()
        render()
        if mode != .idle {
            alert("已更改設定", "下次開始聆聽時生效。")
        }
    }

    private func start(mode newMode: Mode) {
        guard mode == .idle else { stopSession(); return }
        // `mode` is only assigned in `reallyStart`, i.e. after the
        // asynchronous consent callback, so the guard above cannot stop a
        // second press that arrives before then. Without this gate those
        // presses stacked `capture.start` calls on one AVAudioEngine and
        // blocked the main thread repeatedly — the "repeat it and the app
        // stops responding" report.
        guard startGate.begin() else { return }
        if newMode == .dictation {
            // Feedback first, before anything that can block or go async: the
            // Accessibility focus query below, microphone consent, the
            // process-wide input-device lease
            // (`AudioInputLeaseCoordinator.handoffTimeout` waits up to a full
            // second for the level meter to let go) and the audio unit all
            // sit between here and a working session. `.starting` says
            // exactly that much and no more — it does not claim recording has
            // begun. Showing it first is safe: the panel is a nonactivating
            // one that can never become key, so it does not disturb the
            // focused target captured on the next line.
            dictationOverlay.startRequested()
            // Capture before the asynchronous microphone-consent flow can
            // activate our app or its prompt. The target is the app/field that
            // owned focus when this dictation request actually began.
            dictationInsertionTarget = TextInjector.captureFocusedTarget(
                excluding: NSRunningApplication.current.processIdentifier
            )
        } else {
            dictationInsertionTarget = nil
        }
        AudioCapture.requestPermission { [weak self] granted in
            guard let self else { return }
            guard granted else {
                self.startGate.finish()
                if newMode == .dictation {
                    self.dictationInsertionTarget = nil
                    self.interactionMachine.resetAfterStartFailure()
                    self.dictationOverlay.showError("沒有麥克風權限")
                }
                self.alert(
                    "沒有麥克風權限",
                    "請到「系統設定 → 隱私權與安全性 → 麥克風」允許 TEA ASR，然後再試一次。"
                )
                return
            }
            // A PTT key can be released while the asynchronous microphone
            // consent request is still in flight.  Do not start a session
            // after that release; the state machine has already cancelled it.
            if newMode == .dictation,
               self.settings.interactionMode == .pushToTalk,
               !self.interactionMachine.active {
                self.startGate.finish()
                self.dictationInsertionTarget = nil
                // The `.starting` overlay was put up on key-down; nothing
                // will follow it now, so it must not be left on screen.
                self.dictationOverlay.hide()
                return
            }
            self.reallyStart(mode: newMode)
        }
    }

    private func reallyStart(mode newMode: Mode) {
        // Every exit from here on has a session (or a reported failure), so
        // the gate reopens exactly once, at the end of this function.
        defer { startGate.finish() }
        mode = newMode
        appState.setMode(newMode == .meeting ? .meeting : .dictation)
        capture.onFrame = { [weak self] frame in self?.client.send(pcm: frame) }
        capture.onDiagnostics = { [weak self] diagnostics in
            DispatchQueue.main.async {
                self?.mainWindow?.setAudioDiagnostics(diagnostics)
            }
        }
        capture.onError = { [weak self] message in
            DispatchQueue.main.async {
                guard let self else { return }
                self.client.stop()
                self.interactionMachine.resetAfterStartFailure()
                if self.mode == .dictation {
                    self.dictationOverlay.showError(message)
                }
                self.mainWindow?.setStatus("錄音已中斷：\(message)")
                self.alert("錄音已停止", message)
                self.render()
            }
        }
        if newMode == .dictation {
            // Ordered *before* `capture.start` on purpose: it stops the input
            // level meter, which holds the process-wide input lease. Doing it
            // afterwards made every dictation start from an open Settings page
            // wait out `AudioInputLeaseCoordinator.handoffTimeout` for a device
            // this app was about to release anyway.
            //
            // It no longer hides the management window. The user's window
            // staying put is the point; `dictationInsertionTarget` above is
            // what keeps a final out of TEA ASR itself.
            mainWindow?.hideForDictation()
        }
        do {
            try capture.start(configuration: settings.audioInputConfiguration)
        } catch {
            interactionMachine.resetAfterStartFailure()
            dictationInsertionTarget = nil
            mode = .idle
            appState.setMode(.idle)
            if newMode == .dictation {
                dictationOverlay.showError(error.localizedDescription)
            }
            mainWindow?.setStatus("無法開始錄音：\(error.localizedDescription)")
            alert("無法開始錄音", error.localizedDescription)
            return
        }
        if newMode == .dictation {
            dictationOverlay.begin()
            insertedDictationSegments.removeAll()
            playFeedback(.started)
        }
        // Partials are rendered only in our own overlay; they never enter the
        // target app.  This keeps dictation safe while still making the live
        // state visible.
        client.connect(wantsPreview: settings.revisablePreview)
        if newMode == .meeting {
            mainWindow?.show(section: .operations)
        }
        render()
    }

    private func stopSession() {
        if mode == .dictation {
            dictationOverlay.stopRequested()
            playFeedback(.stopped)
        }
        capture.stop()
        client.stop()
        // Keep the active mode until ASRClient receives the server's
        // session.stopped. The server may emit one last transcript.final while
        // draining the requested through_seq; switching to idle here would
        // silently discard that final in wireClient.
        mainWindow?.setStatus("停止中…等待最後一句")
        render()
    }

    // MARK: - Client wiring

    private func wireClient() {
        client.onState = { [weak self] state in
            guard let self else { return }
            self.appState.updateClientState(state)
            self.mainWindow?.setSessionState(state)
            self.render()
            let wasDictation = self.mode == .dictation
            if case .idle = state, self.mode != .idle {
                // This is the completion edge of a normal stop. It is also
                // where a new session is allowed to begin, so the old mode is
                // retained until all final callbacks have been delivered.
                self.capture.stop()
                self.mode = .idle
                self.appState.setMode(.idle)
                self.dictationInsertionTarget = nil
                self.mainWindow?.setStatus("已停止")
                self.interactionMachine.resetAfterSessionEnd()
                if wasDictation { self.dictationOverlay.stopped() }
                self.render()
            } else if case .failed(let issue) = state {
                self.capture.stop()
                self.mode = .idle
                self.appState.setMode(.idle)
                self.dictationInsertionTarget = nil
                self.mainWindow?.setStatus("錯誤：\(issue.message)")
                self.interactionMachine.resetAfterSessionEnd()
                if wasDictation {
                    self.dictationOverlay.showError(issue.message)
                    self.playFeedback(.failed)
                }
                self.render()
            }
        }
        client.onSessionOrigin = { [weak self] origin in
            guard let self else { return }
            // sample 0 of this session was captured `startSample` ago.
            self.sessionOrigin = origin
        }
        client.onTimelineGap = { [weak self] reason in
            guard let self else { return }
            self.mainWindow?.appendGap(reason)
            guard self.mode != .idle else { return }
            // Keep recording: a closed lid should not silently end a meeting.
            self.client.connect(wantsPreview: self.settings.revisablePreview)
            self.render()
        }
        client.onPartial = { [weak self] item in
            guard let self else { return }
            if self.mode == .dictation {
                self.dictationOverlay.showPartial(item.text)
            } else {
                self.mainWindow?.showPartial(item.text, spokenAt: self.spokenAt(item.startSample))
            }
        }
        client.onFinal = { [weak self] item in
            guard let self else { return }
            guard let processed = self.transcriptEvents.process(
                .final(item),
                timestamp: self.spokenAt(item.startSample)
            ) else { return }
            switch self.mode {
            case .meeting:
                self.mainWindow?.appendFinal(processed)
            case .dictation:
                // Keep both modes visible in the same management window. The
                // clipboard/insertion path remains independent of presentation
                // so a dictation final is still inspectable when auto-insert is
                // disabled or Accessibility permission is missing.
                guard self.insertedDictationSegments.insert(processed.metadata.segmentID).inserted else {
                    return
                }
                self.mainWindow?.appendFinal(processed)
                let insertionText = TranscriptOutputPolicy.insertionText(from: processed)
                self.showLastText(TranscriptOutputPolicy.presentationText(from: processed))
                guard !insertionText.isEmpty else {
                    self.dictationOverlay.showFinal(processed.cleanedText)
                    return
                }
                // The overlay must reflect what actually happened to this
                // final, not just that one arrived: showing "已辨識" and then
                // silently falling back to the clipboard is indistinguishable
                // from "nothing happened" to the user watching the floating
                // preview (the main window is hidden for the whole session).
                if !self.settings.autoInsert {
                    TextInjector.copyToClipboard(insertionText)
                    self.mainWindow?.setStatus(
                        "自動貼上已關閉；文字已複製到剪貼簿，請確認後按 ⌘V"
                    )
                    self.dictationOverlay.showCopiedToClipboard(
                        processed.cleanedText, reason: "自動貼上已關閉"
                    )
                } else if !TextInjector.isTrusted {
                    TextInjector.copyToClipboard(insertionText)
                    self.mainWindow?.setStatus(
                        "缺少輔助使用權限；文字已複製到剪貼簿，請確認後按 ⌘V"
                    )
                    self.dictationOverlay.showCopiedToClipboard(
                        processed.cleanedText, reason: "缺少輔助使用權限"
                    )
                    self.warnAboutAccessibilityOnce()
                } else if let target = self.dictationInsertionTarget {
                    switch TextInjector.insert(insertionText, ifCurrent: target) {
                    case .inserted:
                        self.dictationOverlay.showFinal(processed.cleanedText)
                    case .copiedBecauseFocusChanged:
                        self.mainWindow?.setStatus(
                            "原始輸入焦點已變更；文字已複製到剪貼簿，請確認後按 ⌘V"
                        )
                        self.dictationOverlay.showCopiedToClipboard(
                            processed.cleanedText, reason: "原始輸入焦點已變更"
                        )
                    }
                } else {
                    TextInjector.copyToClipboard(insertionText)
                    self.mainWindow?.setStatus(
                        "沒有可安全貼上的原始焦點；文字已複製到剪貼簿，請確認後按 ⌘V"
                    )
                    self.dictationOverlay.showCopiedToClipboard(
                        processed.cleanedText, reason: "沒有可安全貼上的原始焦點"
                    )
                }
            case .idle:
                break
            }
        }
        client.onNotice = { [weak self] message in
            self?.mainWindow?.setStatus(message)
        }
    }

    /// Records the last final so the main window's overview can show it (see
    /// `AppState.lastText`/`MainWindowController`). No longer mirrored into
    /// the menu bar itself — the status-bar menu stays a control surface,
    /// not a second place to read transcript content.
    private func showLastText(_ text: String) {
        appState.updateLastText(text)
    }

    private func warnAboutAccessibilityOnce() {
        guard settings.autoInsert, !TextInjector.isTrusted, !warnedAboutAccessibility else {
            return
        }
        warnedAboutAccessibility = true
        TextInjector.requestTrust()
        alert(
            "文字放進剪貼簿了，但沒有自動貼上",
            "自動貼上需要\(SystemPermissionNaming.accessibilityTitle)權限。到「\(SystemPermissionNaming.accessibilitySettingsPath)」允許 TEA ASR 後，"
                + "重新開始聽寫即可。\n在那之前每段定稿都會放進剪貼簿，按 ⌘V 貼上。"
        )
    }

    private func spokenAt(_ startSample: Int) -> Date {
        sessionOrigin.addingTimeInterval(Double(startSample) / 16_000)
    }

    // MARK: - Hot key

    private func pauseHotKeyForShortcutEditor() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
            interactionMachine.releaseShortcutLatch()
        }
        hotKeyRegistered = false
        hotKeyRegistrationOutcome = .unavailable
        removeKeyUpMonitors()
    }

    private func registerHotKey() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
            // The pending `kEventHotKeyReleased` for a key that is physically
            // down right now dies with this registration, so the press latch
            // can never be cleared by a release that will never arrive. This
            // path runs unprompted — `applicationDidBecomeActive` and its
            // 1.2 s follow-up both call `applyInteractionSettings()` — so a
            // stale latch here silently deafens every later shortcut press.
            interactionMachine.releaseShortcutLatch()
        }

        if hotKeyEventHandler == nil {
            var eventTypes = [
                EventTypeSpec(
                    eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)
                ),
                EventTypeSpec(
                    eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased)
                ),
            ]
            InstallEventHandler(
                GetApplicationEventTarget(),
                { _, event, userData in
                    guard let userData else { return noErr }
                    let controller = Unmanaged<AppController>.fromOpaque(userData).takeUnretainedValue()
                    let eventKind = event.map(GetEventKind)
                    DispatchQueue.main.async {
                        if eventKind == UInt32(kEventHotKeyReleased) {
                            controller.handleShortcut(.shortcutUp)
                        } else {
                            controller.handleShortcut(.shortcutDown(isRepeat: false))
                        }
                    }
                    return noErr
                },
                eventTypes.count,
                &eventTypes,
                Unmanaged.passUnretained(self).toOpaque(),
                &hotKeyEventHandler
            )
        }

        guard settings.interactionMode != .pushToTalk
            || permissionCoordinator?.state.inputMonitoring.isSatisfied == true
        else {
            hotKeyRegistered = false
            hotKeyRegistrationOutcome = .unavailable
            removeKeyUpMonitors()
            mainWindow?.setShortcutStatus("需要輸入監控權限；請到權限頁開啟")
            return
        }

        let shortcut = settings.shortcut
        let id = EventHotKeyID(signature: OSType(0x54454153), id: 1)  // 'TEAS'
        let registration = ExclusiveShortcutRegistrar.register(
            shortcut,
            id: id,
            target: GetApplicationEventTarget(),
            hotKeyRef: &hotKeyRef
        )
        // Ask Carbon for exclusive ownership. A non-exclusive registration
        // can succeed while another process already owns the same key, which
        // made the old UI falsely claim that the shortcut was available.
        hotKeyRegistrationOutcome = registration.outcome
        hotKeyRegistered = hotKeyRegistrationOutcome.isRegistered
        if hotKeyRegistered, settings.interactionMode == .pushToTalk {
            installKeyUpMonitors()
        } else {
            removeKeyUpMonitors()
        }
        mainWindow?.setShortcutStatus(shortcutStatusText())
    }

    private func shortcutStatusText() -> String {
        if settings.interactionMode == .pushToTalk,
           permissionCoordinator?.state.inputMonitoring.isSatisfied != true {
            return "需要輸入監控權限"
        }
        switch hotKeyRegistrationOutcome {
        case .registered:
            return "快捷鍵已啟用"
        case .conflict:
            return "快捷鍵已被其他 app 占用"
        case .unavailable:
            return "快捷鍵無法註冊"
        }
    }

    private func installKeyUpMonitors() {
        for kind in InputMonitorInstallPolicy.missing(
            globalInstalled: globalKeyUpMonitor != nil,
            localInstalled: localKeyUpMonitor != nil
        ) {
            switch kind {
            case .globalKeyUp:
                globalKeyUpMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.keyUp]) { [weak self] event in
                    guard let self else { return }
                    Task { @MainActor [weak self] in
                        self?.handleKeyUp(event)
                    }
                }
            case .localKeyUp:
                localKeyUpMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyUp]) { [weak self] event in
                    guard let self else { return event }
                    self.handleKeyUp(event)
                    return event
                }
            }
        }
        if globalKeyUpMonitor == nil || localKeyUpMonitor == nil {
            mainWindow?.setShortcutStatus("無法監聽按鍵放開；請確認輸入監控權限")
        }
    }

    private func removeKeyUpMonitors() {
        if let globalKeyUpMonitor {
            NSEvent.removeMonitor(globalKeyUpMonitor)
            self.globalKeyUpMonitor = nil
        }
        if let localKeyUpMonitor {
            NSEvent.removeMonitor(localKeyUpMonitor)
            self.localKeyUpMonitor = nil
        }
    }

    private func handleKeyUp(_ event: NSEvent) {
        guard settings.interactionMode == .pushToTalk,
              UInt32(event.keyCode) == settings.shortcut.keyCode
        else { return }
        handleShortcut(.shortcutUp)
    }

    private func handleShortcut(_ event: DictationInteractionEvent) {
        if mode == .meeting, case .shortcutDown = event {
            for command in interactionMachine.handle(.shortcutWhileMeeting) {
                if command == .ignoredDuringMeeting {
                    mainWindow?.setStatus("會議記錄進行中；快捷鍵已忽略，會議不會停止")
                    playFeedback(.ignoredDuringMeeting)
                }
            }
            return
        }
        let commands = interactionMachine.handle(event)
        for command in commands {
            switch command {
            case .start:
                start(mode: .dictation)
            case .stop:
                stopSession()
            case .ignoredDuringMeeting:
                mainWindow?.setStatus("會議記錄進行中；快捷鍵已忽略，會議不會停止")
                playFeedback(.ignoredDuringMeeting)
            }
        }
    }

    private func applyInteractionSettings() {
        if mode == .idle {
            interactionMachine = DictationInteractionStateMachine(mode: settings.interactionMode)
        }
        permissionCoordinator?.updateInteractionRequirement(
            settings.interactionMode == .pushToTalk
        )
        registerHotKey()
        mainWindow?.setShortcutStatus(shortcutStatusText())
        render()
    }

    private func installWorkspaceObservers() {
        let center = NSWorkspace.shared.notificationCenter
        workspaceObservers.append(
            center.addObserver(
                forName: NSWorkspace.willSleepNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in self?.cancelInteraction(.systemSleep) }
            }
        )
        workspaceObservers.append(
            center.addObserver(
                forName: NSWorkspace.didWakeNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in self?.applyInteractionSettings() }
            }
        )
    }

    private func cancelInteraction(_ event: DictationInteractionEvent) {
        if event == .systemSleep {
            // A meeting is not driven by the shortcut state machine, but it
            // still owns an audio/server session and must stop before sleep.
            _ = interactionMachine.handle(event)
            if mode != .idle { stopSession() }
            return
        }
        handleShortcut(event)
    }

    private func playFeedback(_ event: InteractionFeedbackEvent) {
        guard InteractionFeedbackPolicy.shouldPlay(
            enabled: settings.startStopFeedback,
            event: event
        ) else { return }
        NSSound.beep()
    }

    private func alert(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}
