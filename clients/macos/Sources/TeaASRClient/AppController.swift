import AppKit
import Carbon.HIToolbox

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
    private let lastTextEntry = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var warnedAboutAccessibility = false
    private let serviceEntry = NSMenuItem()
    private let autoStartEntry = NSMenuItem()
    private var serviceRunning = false
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
    private var interactionMachine = DictationInteractionStateMachine(mode: .toggle)
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
        lastTextEntry.isEnabled = false
        lastTextEntry.isHidden = true
        menu.addItem(lastTextEntry)
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
        serviceEntry.action = #selector(toggleService)
        serviceEntry.target = self
        serviceEntry.title = "啟動服務"
        menu.addItem(serviceEntry)

        autoStartEntry.title = "登入時自動啟動服務"
        autoStartEntry.action = #selector(toggleAutoStart)
        autoStartEntry.target = self
        menu.addItem(autoStartEntry)

        menu.addItem(.separator())
        let mainWindowEntry = NSMenuItem(
            title: "主畫面…", action: #selector(showMainWindow), keyEquivalent: "0"
        )
        mainWindowEntry.target = self
        menu.addItem(mainWindowEntry)
        let settingsEntry = NSMenuItem(
            title: "設定…", action: #selector(showPreferences), keyEquivalent: ","
        )
        settingsEntry.target = self
        menu.addItem(settingsEntry)

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
        serviceEntry.title = serviceRunning ? "服務執行中" : "啟動服務"
        serviceEntry.isEnabled = !serviceRunning
        if let binary = executable() {
            autoStartEntry.state = ServiceControl.agentInstalled(executable: binary) ? .on : .off
            autoStartEntry.isEnabled = true
        } else {
            autoStartEntry.isEnabled = false
        }
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
            self.serviceRunning = self.appState.serviceReachable == true
            self.render()
        }
    }

    private func executable() -> URL? {
        ServiceControl.resolveExecutable(configured: settings.serviceExecutable)
    }

    @objc private func toggleService() {
        guard let binary = executable() else {
            alert("找不到服務執行檔", ServiceControl.ControlError.executableNotFound.localizedDescription)
            return
        }
        if serviceRunning {
            alert(
                "請從啟動服務的終端機停止",
                "服務是獨立的程序，可能由 LaunchAgent 或你自己的終端機啟動。"
                    + "要停止請在終端機執行：pkill -f 'tea-asr serve'"
            )
            return
        }
        do {
            try ServiceControl.start(executable: binary)
            statusEntry.title = "服務啟動中，模型載入需要幾秒…"
        } catch {
            alert("無法啟動服務", error.localizedDescription)
        }
    }

    @objc private func toggleAutoStart() {
        guard let binary = executable() else {
            alert("找不到服務執行檔", ServiceControl.ControlError.executableNotFound.localizedDescription)
            return
        }
        let installed = ServiceControl.agentInstalled(executable: binary)
        do {
            let output = try ServiceControl.run(
                executable: binary, arguments: ["service", installed ? "uninstall" : "install"]
            )
            alert(installed ? "已取消登入時自動啟動" : "已設定登入時自動啟動", output)
        } catch {
            alert("設定失敗", error.localizedDescription)
        }
        render()
    }

    @objc private func showPreferences() {
        mainWindow?.show(section: .settings)
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
        if newMode == .dictation {
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
                self.dictationInsertionTarget = nil
                return
            }
            self.reallyStart(mode: newMode)
        }
    }

    private func reallyStart(mode newMode: Mode) {
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
            // The final text is inserted into the app that was active before
            // the user opened TEA ASR. Hiding the management window from every
            // dictation entry point before the socket can produce a final
            // prevents TEA ASR from becoming the paste target.
            mainWindow?.hideForDictation()
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
                self.dictationOverlay.showFinal(processed.cleanedText)
                let insertionText = TranscriptOutputPolicy.insertionText(from: processed)
                self.showLastText(TranscriptOutputPolicy.presentationText(from: processed))
                guard !insertionText.isEmpty else { return }
                if self.settings.autoInsert, TextInjector.isTrusted {
                    guard let target = self.dictationInsertionTarget else {
                        TextInjector.copyToClipboard(insertionText)
                        self.mainWindow?.setStatus(
                            "沒有可安全貼上的原始焦點；文字已複製到剪貼簿，請確認後按 ⌘V"
                        )
                        return
                    }
                    switch TextInjector.insert(insertionText, ifCurrent: target) {
                    case .inserted:
                        break
                    case .copiedBecauseFocusChanged:
                        self.mainWindow?.setStatus(
                            "原始輸入焦點已變更；文字已複製到剪貼簿，請確認後按 ⌘V"
                        )
                    }
                } else {
                    TextInjector.copyToClipboard(insertionText)
                    if self.settings.autoInsert {
                        self.warnAboutAccessibilityOnce()
                    }
                }
            case .idle:
                break
            }
        }
        client.onNotice = { [weak self] message in
            self?.mainWindow?.setStatus(message)
        }
    }

    /// Show the last final in the menu so the user can tell recognition from
    /// insertion problems without any extra permission.
    private func showLastText(_ text: String) {
        appState.updateLastText(text)
        let trimmed = text.count > 48 ? String(text.prefix(48)) + "…" : text
        lastTextEntry.title = "最近一句：\(trimmed)"
        lastTextEntry.isHidden = trimmed.isEmpty
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

    private func registerHotKey() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
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
        let status = RegisterEventHotKey(
            shortcut.keyCode,
            shortcut.carbonModifierFlags,
            id,
            GetApplicationEventTarget(),
            UInt32(kEventHotKeyExclusive),
            &hotKeyRef
        )
        // Ask Carbon for exclusive ownership. A non-exclusive registration
        // can succeed while another process already owns the same key, which
        // made the old UI falsely claim that the shortcut was available.
        hotKeyRegistrationOutcome = ShortcutRegistrationPolicy.outcome(for: status)
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
