import AppKit
import Carbon.HIToolbox

/// Menu bar app: dictation into the focused app, or a meeting transcript window.
final class AppController: NSObject, NSApplicationDelegate {
    private enum Mode {
        case idle
        case dictation
        case meeting
    }

    private let settings = Settings()
    private let capture = AudioCapture()
    private lazy var client = ASRClient(settings: settings)

    private var statusItem: NSStatusItem!
    private let menu = NSMenu()
    private let statusEntry = NSMenuItem(title: "未啟動", action: nil, keyEquivalent: "")
    private let toggleEntry = NSMenuItem()
    private let meetingEntry = NSMenuItem()
    private let autoInsertEntry = NSMenuItem()
    private let previewEntry = NSMenuItem()
    private let lastTextEntry = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var warnedAboutAccessibility = false

    private var mode: Mode = .idle
    private var meeting: MeetingWindow?
    private var preferences: PreferencesWindow?
    private var hotKeyRef: EventHotKeyRef?
    private var hotKeyRegistered = false
    /// Wall clock of the current session's sample 0, so a session restarted after
    /// a timeline gap still lands on one continuous meeting timeline.
    private var sessionOrigin = Date()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        buildMenu()
        wireClient()
        registerHotKey()
        render()
    }

    func applicationWillTerminate(_ notification: Notification) {
        capture.stop()
        client.cancel()
    }

    // MARK: - Menu

    private func buildMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.menu = menu

        statusEntry.isEnabled = false
        menu.addItem(statusEntry)
        lastTextEntry.isEnabled = false
        lastTextEntry.isHidden = true
        menu.addItem(lastTextEntry)
        menu.addItem(.separator())

        toggleEntry.action = #selector(toggleDictation)
        toggleEntry.target = self
        toggleEntry.keyEquivalent = "d"
        toggleEntry.keyEquivalentModifierMask = [.command, .option]
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
        let settingsEntry = NSMenuItem(
            title: "設定…", action: #selector(showPreferences), keyEquivalent: ","
        )
        settingsEntry.target = self
        menu.addItem(settingsEntry)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "結束", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
    }

    private func render() {
        let symbol: String
        switch (mode, client.state) {
        case (_, .failed):
            symbol = "exclamationmark.triangle"
        case (.idle, _):
            symbol = "mic"
        case (_, .connecting), (_, .loadingModel):
            symbol = "mic.badge.plus"
        default:
            symbol = "waveform"
        }
        statusItem.button?.image = NSImage(
            systemSymbolName: symbol, accessibilityDescription: "TEA ASR"
        )

        switch client.state {
        case .idle:
            statusEntry.title = mode == .idle ? "未啟動" : "連線中…"
        case .connecting:
            statusEntry.title = "連線中…"
        case .loadingModel:
            statusEntry.title = "模型載入中…"
        case .listening(_, let preview):
            statusEntry.title = preview ? "聆聽中（含串流預覽）" : "聆聽中"
        case .failed(let message):
            statusEntry.title = "錯誤：\(message.prefix(60))"
        }

        toggleEntry.title = (mode == .dictation ? "停止聽寫" : "開始聽寫")
            + (hotKeyRegistered ? "" : "（⌥⌘D 被其他 app 占用）")
        meetingEntry.title = mode == .meeting ? "停止會議記錄" : "開始會議記錄"
        autoInsertEntry.state = settings.autoInsert ? .on : .off
        previewEntry.state = settings.revisablePreview ? .on : .off
    }

    // MARK: - Actions

    @objc private func toggleDictation() {
        mode == .dictation ? stopSession() : start(mode: .dictation)
    }

    @objc private func toggleMeeting() {
        mode == .meeting ? stopSession() : start(mode: .meeting)
    }

    @objc private func showPreferences() {
        if preferences == nil {
            let window = PreferencesWindow(settings: settings)
            window.onClose = { [weak self] in
                self?.preferences = nil
                self?.render()
            }
            preferences = window
        }
        preferences?.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func toggleAutoInsert() {
        settings.autoInsert.toggle()
        if settings.autoInsert, !TextInjector.isTrusted {
            TextInjector.requestTrust()
            alert(
                "需要輔助使用權限",
                "要把文字貼進其他 app，得在「系統設定 → 隱私權與安全性 → 輔助使用」裡允許 TEA ASR。\n"
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
        AudioCapture.requestPermission { [weak self] granted in
            guard let self else { return }
            guard granted else {
                self.alert(
                    "沒有麥克風權限",
                    "請到「系統設定 → 隱私權與安全性 → 麥克風」允許 TEA ASR，然後再試一次。"
                )
                return
            }
            self.reallyStart(mode: newMode)
        }
    }

    private func reallyStart(mode newMode: Mode) {
        if newMode == .meeting {
            let window = MeetingWindow()
            window.onClose = { [weak self] in self?.stopSession() }
            window.showWindow(nil)
            NSApp.activate(ignoringOtherApps: true)
            meeting = window
        }
        mode = newMode
        capture.onFrame = { [weak self] frame in self?.client.send(pcm: frame) }
        do {
            try capture.start()
        } catch {
            mode = .idle
            meeting?.close()
            meeting = nil
            alert("無法開始錄音", error.localizedDescription)
            return
        }
        // Dictation only ever inserts finals, so a preview there would cost
        // inference nobody sees.
        client.connect(wantsPreview: newMode == .meeting && settings.revisablePreview)
        render()
    }

    private func stopSession() {
        capture.stop()
        client.stop()
        mode = .idle
        meeting?.setStatus("已停止")
        render()
    }

    // MARK: - Client wiring

    private func wireClient() {
        client.onState = { [weak self] _ in
            guard let self else { return }
            self.render()
            if case .failed(let message) = self.client.state {
                self.capture.stop()
                self.mode = .idle
                self.meeting?.setStatus("錯誤：\(message)")
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
            self.meeting?.appendGap(reason)
            guard self.mode != .idle else { return }
            // Keep recording: a closed lid should not silently end a meeting.
            self.client.connect(wantsPreview: self.mode == .meeting && self.settings.revisablePreview)
            self.render()
        }
        client.onPartial = { [weak self] item in
            guard let self else { return }
            self.meeting?.showPartial(item.text, spokenAt: self.spokenAt(item.startSample))
        }
        client.onFinal = { [weak self] item in
            guard let self else { return }
            switch self.mode {
            case .meeting:
                self.meeting?.appendFinal(item.text, spokenAt: self.spokenAt(item.startSample))
            case .dictation:
                self.showLastText(item.text)
                if self.settings.autoInsert, TextInjector.isTrusted {
                    TextInjector.insert(item.text)
                } else {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(item.text, forType: .string)
                    self.warnAboutAccessibilityOnce()
                }
            case .idle:
                break
            }
        }
        client.onNotice = { [weak self] message in
            self?.meeting?.setStatus(message)
        }
    }

    /// Show the last final in the menu so the user can tell recognition from
    /// insertion problems without any extra permission.
    private func showLastText(_ text: String) {
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
            "自動貼上需要輔助使用權限。到「系統設定 → 隱私權與安全性 → 輔助使用」允許 TEA ASR 後，"
                + "重新開始聽寫即可。\n在那之前每段定稿都會放進剪貼簿，按 ⌘V 貼上。"
        )
    }

    private func spokenAt(_ startSample: Int) -> Date {
        sessionOrigin.addingTimeInterval(Double(startSample) / 16_000)
    }

    // MARK: - Hot key

    private func registerHotKey() {
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)
        )
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, _, userData in
                guard let userData else { return noErr }
                let controller = Unmanaged<AppController>.fromOpaque(userData).takeUnretainedValue()
                DispatchQueue.main.async { controller.toggleDictation() }
                return noErr
            },
            1,
            &eventType,
            Unmanaged.passUnretained(self).toOpaque(),
            nil
        )
        let id = EventHotKeyID(signature: OSType(0x54454153), id: 1)  // 'TEAS'
        let status = RegisterEventHotKey(
            UInt32(kVK_ANSI_D),
            UInt32(cmdKey | optionKey),
            id,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
        // Another app may already own the combination. Saying so beats letting
        // the user press it and wonder why nothing happens.
        hotKeyRegistered = status == noErr
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
