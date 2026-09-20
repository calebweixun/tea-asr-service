import AppKit
import Foundation

/// The one user-facing window for the menu-bar app.
///
/// This intentionally stays AppKit-only.  The controller owns presentation and
/// transcript state, while AppController remains the owner of audio/session
/// side effects.  That makes the same window useful when the app is launched
/// without opening a session (for example, to fix a missing permission).
@MainActor
final class MainWindowController: NSWindowController, NSWindowDelegate {
    private struct StatusPresentation {
        let title: String
        let detail: String
        let symbolName: String
        let tint: NSColor
    }

    /// A section's view hierarchy plus the closure that refreshes its labels
    /// in place. Each section is built exactly once; status updates call
    /// `update()` instead of tearing the view tree down, which is what keeps
    /// scroll position, first responder (e.g. a settings text field being
    /// edited, or the shortcut recorder), and hover/pressed button state
    /// stable while the app is idly reporting status in the background.
    private struct SectionRuntime {
        let view: NSView
        let update: () -> Void
    }

    enum Section: Int, CaseIterable {
        case overview
        case operations
        case permissions
        case settings
        case diagnostics

        var title: String {
            switch self {
            case .overview: return "總覽"
            case .operations: return "操作"
            case .permissions: return "權限"
            case .settings: return "設定"
            case .diagnostics: return "診斷"
            }
        }

        var symbolName: String {
            switch self {
            case .overview: return "rectangle.3.group"
            case .operations: return "mic"
            case .permissions: return "lock.shield"
            case .settings: return "gearshape"
            case .diagnostics: return "stethoscope"
            }
        }
    }

    private let settings: Settings
    private let appState: AppState
    private let permissions: PermissionCoordinator

    private let splitViewController = NSSplitViewController()
    private let sidebarController = NSViewController()
    private let detailController = NSViewController()
    private let detailView = NSView()
    private var sectionButtons: [NSButton] = []
    private var selectedSection: Section = .overview
    private var sectionRuntimes: [Section: SectionRuntime] = [:]

    private enum TranscriptEntryKind {
        case finalText
        case gap
    }

    private struct TranscriptEntry {
        let spokenAt: Date
        let text: String
        let kind: TranscriptEntryKind
        let sequence: Int
        let metadata: TranscriptSegmentMetadata?
    }

    private var audioDiagnostics: AudioDiagnostics?
    private var lastAudioDiagnosticsRenderAt = Date.distantPast
    private var sessionStatus = "尚未開始 session"
    private var partialText = ""
    private var partialSpokenAt: Date?
    private var transcriptEntries: [TranscriptEntry] = []
    private var nextTranscriptSequence = 0
    private let transcriptStartedAt = Date()
    private var autosaveStatus = "尚未寫入自動存檔"
    private var shortcutStatus = "尚未註冊"
    private lazy var autosaveURL: URL = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmm"
        let directory = FileManager.default
            .homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/TEA ASR/meetings")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent("meeting-\(formatter.string(from: transcriptStartedAt)).md")
    }()

    var onStartDictation: (() -> Void)?
    var onStartMeeting: (() -> Void)?
    var onStopSession: (() -> Void)?
    var onRefreshService: (() -> Void)?
    var onSettingsChanged: (() -> Void)?

    init(settings: Settings, appState: AppState, permissions: PermissionCoordinator) {
        self.settings = settings
        self.appState = appState
        self.permissions = permissions

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 980, height: 650),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "TEA ASR"
        window.minSize = NSSize(width: 780, height: 520)
        window.toolbarStyle = .unifiedCompact
        window.titlebarAppearsTransparent = false
        window.backgroundColor = .windowBackgroundColor
        // The app is a menu-bar resident process, so closing the management
        // window must not release it. Reopen requests should reveal this same
        // controller instead of forcing a second window to be constructed.
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)
        window.delegate = self

        permissions.onChange = { [weak self] _ in
            guard let self else { return }
            self.refreshPermissionSection()
        }
        build()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("not supported")
    }

    // MARK: - Lifecycle

    private func build() {
        buildSidebar()

        let detailScroll = NSScrollView()
        detailScroll.drawsBackground = false
        detailScroll.borderType = .noBorder
        detailScroll.hasVerticalScroller = true
        detailScroll.hasHorizontalScroller = false
        detailScroll.autohidesScrollers = true
        detailScroll.documentView = detailView
        detailView.translatesAutoresizingMaskIntoConstraints = false
        detailController.view = detailScroll
        NSLayoutConstraint.activate([
            detailView.leadingAnchor.constraint(equalTo: detailScroll.contentView.leadingAnchor),
            detailView.trailingAnchor.constraint(equalTo: detailScroll.contentView.trailingAnchor),
            detailView.topAnchor.constraint(equalTo: detailScroll.contentView.topAnchor),
            detailView.widthAnchor.constraint(equalTo: detailScroll.contentView.widthAnchor),
        ])
        splitViewController.addSplitViewItem(
            NSSplitViewItem(sidebarWithViewController: sidebarController)
        )
        splitViewController.addSplitViewItem(
            NSSplitViewItem(viewController: detailController)
        )
        splitViewController.splitView.setPosition(210, ofDividerAt: 0)
        window?.contentViewController = splitViewController

        mountSelectedSection()
    }

    private func buildSidebar() {
        let root = NSVisualEffectView()
        root.material = .sidebar
        root.blendingMode = .withinWindow
        root.state = .active
        let heading = NSTextField(labelWithString: "TEA ASR")
        heading.font = .systemFont(ofSize: 18, weight: .semibold)
        heading.translatesAutoresizingMaskIntoConstraints = false

        let hint = NSTextField(labelWithString: "本機語音服務")
        hint.textColor = .secondaryLabelColor
        hint.translatesAutoresizingMaskIntoConstraints = false

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 5
        stack.translatesAutoresizingMaskIntoConstraints = false
        sectionButtons = Section.allCases.map { section in
            let button = NSButton(
                title: section.title,
                target: self,
                action: #selector(selectSection(_:))
            )
            button.tag = section.rawValue
            button.image = NSImage(systemSymbolName: section.symbolName, accessibilityDescription: section.title)
            button.image?.isTemplate = true
            button.imagePosition = .imageLeading
            button.alignment = .left
            button.bezelStyle = .regularSquare
            button.isBordered = false
            button.setButtonType(.toggle)
            button.translatesAutoresizingMaskIntoConstraints = false
            button.controlSize = .large
            button.heightAnchor.constraint(equalToConstant: 34).isActive = true
            stack.addArrangedSubview(button)
            return button
        }

        let footer = NSTextField(wrappingLabelWithString: "狀態與設定集中在同一個主畫面。")
        footer.textColor = .tertiaryLabelColor
        footer.font = .systemFont(ofSize: 11)
        footer.translatesAutoresizingMaskIntoConstraints = false

        root.addSubview(heading)
        root.addSubview(hint)
        root.addSubview(stack)
        root.addSubview(footer)
        // Activate this constraint only after the stack has joined the root's
        // hierarchy. Activating it while both views are detached makes AppKit
        // raise an Auto Layout exception because there is no common ancestor.
        for button in sectionButtons {
            button.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        NSLayoutConstraint.activate([
            heading.topAnchor.constraint(equalTo: root.topAnchor, constant: 20),
            heading.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
            hint.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 2),
            hint.leadingAnchor.constraint(equalTo: heading.leadingAnchor),
            stack.topAnchor.constraint(equalTo: hint.bottomAnchor, constant: 22),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -12),
            footer.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
            footer.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -16),
            footer.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -16),
        ])
        sidebarController.view = root
        select(section: .overview)
    }

    func show(section: Section = .overview) {
        select(section: section)
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Return focus to the app that was active before the management window was
    /// used to start dictation. This is intentionally separate from `show` so
    /// menu-bar/hot-key dictation never activates this window implicitly.
    func hideForDictation() {
        window?.orderOut(nil)
        NSApp.hide(nil)
    }

    func refresh() {
        // Health polls and client transitions must not rebuild settings fields
        // while the user is editing unsaved values.
        guard selectedSection != .settings else { return }
        if selectedSection == .permissions {
            permissions.refresh()
        } else {
            updateSection(selectedSection)
        }
    }

    func windowDidBecomeKey(_ notification: Notification) {
        permissions.refresh()
    }

    // MARK: - Session presentation API

    func setStatus(_ text: String) {
        sessionStatus = text
        // Update every section that shows this text, not only the one the
        // user currently has open, so switching sections never reveals stale
        // content that was silently skipped while it was off-screen.
        updateSection(.overview)
        updateSection(.operations)
    }

    func setShortcutStatus(_ text: String) {
        shortcutStatus = text
        updateSection(.settings)
    }

    func setSessionState(_ state: ASRClient.State) {
        setStatus(MeetingSessionPresentation.status(for: state))
    }

    func setAudioDiagnostics(_ diagnostics: AudioDiagnostics) {
        audioDiagnostics = diagnostics
        // AudioCapture emits one snapshot per tap buffer (can be well over
        // 100/sec). Applying an update is now cheap — it assigns a string to
        // an already-built label instead of tearing down and relaying out the
        // whole detail pane — but formatting the summary strings that often
        // is still wasted work no one can perceive, so keep the throttle.
        let now = Date()
        guard now.timeIntervalSince(lastAudioDiagnosticsRenderAt) >= 0.5 else { return }
        lastAudioDiagnosticsRenderAt = now
        updateSection(.overview)
        updateSection(.operations)
        updateSection(.diagnostics)
    }

    func showPartial(_ text: String, spokenAt: Date) {
        partialText = text
        partialSpokenAt = text.isEmpty ? nil : spokenAt
        updateSection(.operations)
    }

    func appendFinal(_ processed: ProcessedTranscript) {
        partialText = ""
        partialSpokenAt = nil
        guard !processed.cleanedText.isEmpty else { return }
        appendTranscriptEntry(
            TranscriptEntry(
                spokenAt: processed.metadata.timestamp,
                text: processed.cleanedText,
                kind: .finalText,
                sequence: nextTranscriptSequence,
                metadata: processed.metadata
            )
        )
        appState.updateLastText(processed.cleanedText)
        updateSection(.operations)
        updateSection(.overview)
    }

    func appendGap(_ reason: String) {
        partialText = ""
        partialSpokenAt = nil
        appendTranscriptEntry(
            TranscriptEntry(
                spokenAt: Date(),
                text: reason,
                kind: .gap,
                sequence: nextTranscriptSequence,
                metadata: nil
            )
        )
        sessionStatus = "錄音有間隔：\(reason)"
        updateSection(.operations)
        updateSection(.diagnostics)
    }

    // MARK: - Navigation

    @objc private func selectSection(_ sender: NSButton) {
        guard let section = Section(rawValue: sender.tag) else { return }
        select(section: section)
    }

    private func select(section: Section) {
        selectedSection = section
        for button in sectionButtons {
            let selected = button.tag == section.rawValue
            button.state = selected ? .on : .off
            button.wantsLayer = true
            button.layer?.cornerRadius = 8
            button.layer?.backgroundColor = selected
                ? NSColor.controlAccentColor.withAlphaComponent(0.14).cgColor
                : NSColor.clear.cgColor
            button.contentTintColor = selected ? .controlAccentColor : .labelColor
            button.font = .systemFont(ofSize: 13, weight: selected ? .semibold : .regular)
        }
        mountSelectedSection()
    }

    private func refreshPermissionSection() {
        guard selectedSection == .permissions else { return }
        updateSection(.permissions)
    }

    // MARK: - Detail sections

    /// Mounts the currently selected section's view, building it the first
    /// time it is shown. This is the only place the detail pane's child
    /// hierarchy is torn down, and even then only the previous section's
    /// single root view is removed — never the views inside a section.
    private func mountSelectedSection() {
        let runtime = sectionRuntime(for: selectedSection)
        if detailView.subviews.first !== runtime.view {
            detailView.subviews.forEach { $0.removeFromSuperview() }
            runtime.view.translatesAutoresizingMaskIntoConstraints = false
            detailView.addSubview(runtime.view)
            NSLayoutConstraint.activate([
                runtime.view.leadingAnchor.constraint(equalTo: detailView.leadingAnchor, constant: 28),
                runtime.view.trailingAnchor.constraint(equalTo: detailView.trailingAnchor, constant: -28),
                runtime.view.topAnchor.constraint(equalTo: detailView.topAnchor, constant: 24),
                runtime.view.bottomAnchor.constraint(equalTo: detailView.bottomAnchor, constant: -24),
            ])
        }
        // Refresh unconditionally: state-change methods below update every
        // section that displays that state, not only the visible one, so a
        // section can go stale while it is not mounted and must catch up here.
        runtime.update()
    }

    private func sectionRuntime(for section: Section) -> SectionRuntime {
        if let cached = sectionRuntimes[section] { return cached }
        let runtime: SectionRuntime
        switch section {
        case .overview: runtime = buildOverviewSection()
        case .operations: runtime = buildOperationsSection()
        case .permissions: runtime = buildPermissionsSection()
        case .settings: runtime = buildSettingsSection()
        case .diagnostics: runtime = buildDiagnosticsSection()
        }
        sectionRuntimes[section] = runtime
        return runtime
    }

    /// Refreshes an already-built section's labels in place. A section that
    /// has never been mounted is skipped on purpose: `mountSelectedSection`
    /// builds it pre-populated with current state the first time it appears,
    /// so there is nothing stale to fix up before then.
    private func updateSection(_ section: Section) {
        sectionRuntimes[section]?.update()
    }

    private func buildOverviewSection() -> SectionRuntime {
        let hero = buildStatusHero()
        let service = buildMetricCard(title: "服務", symbolName: "server.rack", tint: .systemBlue)
        let model = buildMetricCard(title: "模型", symbolName: "cpu", tint: .systemPurple)
        let sessionRow = buildValueRow(title: "Session")
        let audioRow = buildValueRow(title: "輸入")
        let runtimeCard = cardStack([sessionRow.view, audioRow.view], title: "執行狀態", symbolName: "waveform")
        let recent = buildRecentTextCard()

        let startDictation = actionButton(title: "開始聽寫", action: #selector(startDictation(_:)))
        let startMeeting = actionButton(title: "開始會議記錄", action: #selector(startMeeting(_:)))
        let permissionsButton = actionButton(title: "檢查權限", action: #selector(openPermissions(_:)))
        let actions = NSStackView(views: [startDictation, startMeeting, permissionsButton])
        actions.orientation = .horizontal
        actions.spacing = 10

        let view = sectionStack(
            title: "總覽",
            subtitle: "服務、模型與目前音訊狀態",
            symbolName: "rectangle.3.group",
            views: [
                hero.view,
                metricPair([service.view, model.view]),
                runtimeCard,
                recent.view,
                cardStack([actions], symbolName: "bolt.fill"),
            ]
        )

        func update() {
            hero.update()
            service.update(serverSummary())
            model.update(modelSummary())
            sessionRow.update("\(modeTitle()) · \(sessionStatus)")
            audioRow.update(audioSummary())
            recent.update()
            let dictationTitle = appState.mode == .dictation ? "停止聽寫" : "開始聽寫"
            if startDictation.title != dictationTitle { startDictation.title = dictationTitle }
            let meetingTitle = appState.mode == .meeting ? "停止會議記錄" : "開始會議記錄"
            if startMeeting.title != meetingTitle { startMeeting.title = meetingTitle }
        }
        update()
        return SectionRuntime(view: view, update: update)
    }

    private func buildOperationsSection() -> SectionRuntime {
        let startDictation = actionButton(title: "開始聽寫", action: #selector(startDictation(_:)))
        let startMeeting = actionButton(title: "開始會議記錄", action: #selector(startMeeting(_:)))
        let stop = actionButton(title: "停止目前 session", action: #selector(stopSession(_:)))
        let controls = NSStackView(views: [startDictation, startMeeting, stop])
        controls.orientation = .horizontal
        controls.spacing = 10

        let statusRow = buildValueRow(title: "狀態")
        let audioRow = buildValueRow(title: "音訊")
        let transcriptInfoRow = buildValueRow(title: "自動存檔")
        let transcript = buildTranscriptView()
        let transcriptTitle = NSTextField(labelWithString: "最近文字／會議 transcript")
        transcriptTitle.font = .systemFont(ofSize: 14, weight: .semibold)
        let export = actionButton(title: "匯出 Markdown…", action: #selector(exportMarkdown(_:)))

        let view = sectionStack(
            title: "操作",
            subtitle: "開始、停止並即時查看辨識結果",
            symbolName: "mic.fill",
            views: [
                cardStack([controls], title: "開始錄音", symbolName: "record.circle"),
                cardStack([statusRow.view, audioRow.view, transcriptInfoRow.view], title: "目前 session", symbolName: "waveform.path.ecg"),
                cardStack([transcriptTitle, transcript.view], title: "即時逐字稿", symbolName: "text.quote"),
                cardStack([export], symbolName: "square.and.arrow.up"),
            ]
        )

        func update() {
            let dictationTitle = appState.mode == .dictation ? "停止聽寫" : "開始聽寫"
            if startDictation.title != dictationTitle { startDictation.title = dictationTitle }
            let meetingTitle = appState.mode == .meeting ? "停止會議記錄" : "開始會議記錄"
            if startMeeting.title != meetingTitle { startMeeting.title = meetingTitle }
            stop.isEnabled = appState.mode != .idle
            statusRow.update(sessionStatus)
            audioRow.update(audioSummary())
            transcriptInfoRow.update(autosaveStatus)
            transcript.update()
        }
        update()
        return SectionRuntime(view: view, update: update)
    }

    private func buildPermissionsSection() -> SectionRuntime {
        let summary = NSTextField(wrappingLabelWithString: "")
        let rows = PermissionKind.allCases.map { buildPermissionRow(for: $0) }
        let permissionRows = NSStackView(views: rows.map(\.view))
        permissionRows.orientation = .vertical
        permissionRows.alignment = .width
        permissionRows.spacing = 12
        let refresh = actionButton(title: "重新檢查權限", action: #selector(refreshPermissions(_:)))
        let view = sectionStack(
            title: "權限",
            subtitle: "完成錄音與自動貼上需要的系統授權",
            symbolName: "lock.shield.fill",
            views: [
                cardStack([summary], symbolName: "checkmark.shield"),
                cardStack([permissionRows], title: "權限清單", symbolName: "list.bullet"),
                cardStack([refresh], symbolName: "arrow.clockwise"),
            ]
        )

        func update() {
            let state = permissions.state
            let summaryText = state.requiredPermissionsGranted
                ? "必要權限已具備。若要自動貼上，仍需允許輔助使用。"
                : "請完成下列必要權限；完成後回到此視窗，狀態會自動更新。"
            if summary.stringValue != summaryText { summary.stringValue = summaryText }
            summary.textColor = state.requiredPermissionsGranted ? .systemGreen : .systemOrange
            for row in rows {
                row.update(state.item(for: row.kind))
            }
        }
        update()
        return SectionRuntime(view: view, update: update)
    }

    /// A permission row is structurally fixed (icon, title, explanation, and
    /// an optional trailing action button). Only one button can ever be
    /// meaningful per state, so it is built once and toggled with `isHidden`
    /// — an NSStackView excludes hidden arranged subviews from layout,
    /// including their spacing, so this reproduces the old
    /// add-button-only-when-needed behaviour without touching the hierarchy.
    private func buildPermissionRow(
        for kind: PermissionKind
    ) -> (view: NSView, kind: PermissionKind, update: (PermissionItemState) -> Void) {
        let statusIcon = NSImageView(image: NSImage())
        statusIcon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .semibold)
        let title = NSTextField(labelWithString: "")
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        let titleRow = NSStackView(views: [statusIcon, title])
        titleRow.orientation = .horizontal
        titleRow.alignment = .centerY
        titleRow.spacing = 6
        let explanation = NSTextField(wrappingLabelWithString: "")
        explanation.textColor = .secondaryLabelColor
        explanation.font = .systemFont(ofSize: 12)

        let text = NSStackView(views: [titleRow, explanation])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 3

        let action = NSButton(title: "", target: self, action: nil)
        action.setContentHuggingPriority(.required, for: .horizontal)
        action.isHidden = true

        let row = NSStackView(views: [text, action])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12

        func update(_ item: PermissionItemState) {
            let statusText: String
            let color: NSColor
            switch item.authorization {
            case .authorized, .notRequired:
                statusText = item.authorization == .notRequired ? "不需要" : "已允許"
                color = .systemGreen
            case .notDetermined:
                statusText = "尚未決定"
                color = .systemOrange
            case .denied:
                statusText = "未允許"
                color = .systemRed
            case .restricted:
                statusText = "受系統限制"
                color = .systemRed
            }

            let titleText = "\(item.title) · \(statusText)"
            if title.stringValue != titleText { title.stringValue = titleText }
            title.textColor = color
            let statusIconName = item.isSatisfied ? "checkmark.circle.fill" : "exclamationmark.circle.fill"
            statusIcon.image = NSImage(systemSymbolName: statusIconName, accessibilityDescription: statusText) ?? NSImage()
            statusIcon.contentTintColor = color
            if explanation.stringValue != item.explanation { explanation.stringValue = item.explanation }

            if let actionTitle = item.actionTitle {
                let isNativePrompt = item.authorization == .notDetermined
                let buttonTitle = isNativePrompt ? actionTitle : "開啟設定"
                if action.title != buttonTitle { action.title = buttonTitle }
                action.action = isNativePrompt
                    ? #selector(permissionAction(_:))
                    : #selector(openPermissionSettings(_:))
                action.tag = permissionTag(for: item.kind)
                action.isHidden = false
            } else {
                action.isHidden = true
            }
        }
        return (row, kind, update)
    }

    private func buildSettingsSection() -> SectionRuntime {
        let host = NSTextField(string: settings.host)
        let port = NSTextField(string: String(settings.port))
        let token = NSSecureTextField(string: (try? settings.token()) ?? "")
        token.placeholderString = "輸入 bearer token（儲存到本機 token 檔）"
        let hotKey = ShortcutRecorderField(shortcut: settings.shortcut)
        hotKey.identifier = NSUserInterfaceItemIdentifier("shortcut")
        hotKey.onValidationError = { [weak self] message in
            self?.setShortcutStatus("快捷鍵無效：\(message)")
        }
        let interactionMode = NSPopUpButton()
        interactionMode.identifier = NSUserInterfaceItemIdentifier("interactionMode")
        for mode in DictationInteractionMode.allCases {
            interactionMode.addItem(withTitle: mode.title)
            interactionMode.item(at: interactionMode.numberOfItems - 1)?.representedObject = mode.rawValue
        }
        interactionMode.selectItem(
            withTitle: settings.interactionMode.title
        )
        let feedback = NSButton(
            checkboxWithTitle: "開始／停止時播放系統提示音",
            target: self,
            action: #selector(saveSettings(_:))
        )
        feedback.identifier = NSUserInterfaceItemIdentifier("feedback")
        feedback.state = settings.startStopFeedback ? .on : .off
        let inputDevice = inputDevicePopup()
        let inputChannel = inputChannelPopup(deviceUID: settings.inputDeviceUID)
        host.controlSize = .large
        port.controlSize = .large
        token.controlSize = .large
        hotKey.controlSize = .large
        interactionMode.controlSize = .large
        inputDevice.controlSize = .large
        inputChannel.controlSize = .large
        host.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        port.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        token.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        hotKey.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        interactionMode.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        inputDevice.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        inputChannel.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        host.identifier = NSUserInterfaceItemIdentifier("host")
        port.identifier = NSUserInterfaceItemIdentifier("port")
        token.identifier = NSUserInterfaceItemIdentifier("token")
        inputDevice.identifier = NSUserInterfaceItemIdentifier("inputDevice")
        inputChannel.identifier = NSUserInterfaceItemIdentifier("inputChannel")
        host.target = self
        port.target = self
        token.target = self
        inputDevice.target = self
        inputDevice.action = #selector(audioDeviceSelectionChanged(_:))
        host.action = #selector(saveSettings(_:))
        port.action = #selector(saveSettings(_:))
        token.action = #selector(saveSettings(_:))
        interactionMode.target = self
        interactionMode.action = #selector(saveSettings(_:))

        let grid = NSGridView(views: [
            [NSTextField(labelWithString: "服務位址"), host],
            [NSTextField(labelWithString: "Port"), port],
            [NSTextField(labelWithString: "Token"), token],
            [NSTextField(labelWithString: "快捷鍵"), hotKey],
            [NSTextField(labelWithString: "互動模式"), interactionMode],
        ])
        let audioGrid = NSGridView(views: [
            [NSTextField(labelWithString: "輸入裝置"), inputDevice],
            [NSTextField(labelWithString: "聲道"), inputChannel],
        ])
        grid.column(at: 0).xPlacement = .trailing
        grid.columnSpacing = 12
        grid.rowSpacing = 12
        audioGrid.column(at: 0).xPlacement = .trailing
        audioGrid.columnSpacing = 12
        audioGrid.rowSpacing = 12

        let autoInsert = NSButton(
            checkboxWithTitle: "定稿後自動貼進前景 app",
            target: self,
            action: #selector(saveSettings(_:))
        )
        autoInsert.identifier = NSUserInterfaceItemIdentifier("autoInsert")
        autoInsert.state = settings.autoInsert ? .on : .off
        let preview = NSButton(
            checkboxWithTitle: "會議記錄顯示即時預覽",
            target: self,
            action: #selector(saveSettings(_:))
        )
        preview.identifier = NSUserInterfaceItemIdentifier("preview")
        preview.state = settings.revisablePreview ? .on : .off

        let tokenHint = NSTextField(
            wrappingLabelWithString: "Token 只寫入 ~/Library/Application Support/TEA ASR/token，不會放進偏好設定。"
        )
        tokenHint.textColor = .secondaryLabelColor
        tokenHint.font = .systemFont(ofSize: 12)
        let shortcutHint = NSTextField(wrappingLabelWithString: "")
        shortcutHint.font = .systemFont(ofSize: 12)
        let save = actionButton(title: "儲存設定", action: #selector(saveSettings(_:)))
        let test = actionButton(title: "測試服務連線", action: #selector(testService(_:)))
        let buttons = NSStackView(views: [save, test])
        buttons.orientation = .horizontal
        buttons.spacing = 10

        let view = sectionStack(
            title: "設定",
            subtitle: "服務連線、音訊輸入與輸入行為",
            symbolName: "gearshape.fill",
            views: [
                cardStack([grid, tokenHint], title: "服務連線", symbolName: "network"),
                cardStack([audioGrid], title: "音訊輸入", symbolName: "waveform"),
                cardStack([shortcutHint, feedback, autoInsert, preview], title: "輸入行為", symbolName: "keyboard"),
                cardStack([buttons], symbolName: "checkmark.circle"),
            ]
        )

        func update() {
            let text = "\(shortcutStatus) · 按一下快捷鍵欄位即可重新錄製。"
            if shortcutHint.stringValue != text { shortcutHint.stringValue = text }
            shortcutHint.textColor = shortcutStatus.contains("無效") ? .systemRed : .secondaryLabelColor
        }
        update()
        return SectionRuntime(view: view, update: update)
    }

    private func inputDevicePopup() -> NSPopUpButton {
        let popup = NSPopUpButton()
        let available = AudioInputDeviceCatalog.enumerate()
        let storedOption = AudioInputSettingsOptions.deviceOption(
            storedUID: settings.inputDeviceUID,
            available: available
        )
        let options = [AudioInputDeviceOption.systemDefault]
            + available.map(AudioInputDeviceOption.available)
            + (storedOption.isEnabled ? [] : [storedOption])
        for option in options {
            popup.addItem(withTitle: option.title)
            let item = popup.item(at: popup.numberOfItems - 1)
            item?.representedObject = option.uid ?? AudioInputDevice.systemDefaultUID
            item?.isEnabled = option.isEnabled
        }
        let selectedUID = storedOption.uid ?? AudioInputDevice.systemDefaultUID
        if let item = popup.itemArray.last(where: { ($0.representedObject as? String) == selectedUID }) {
            popup.select(item)
        } else {
            popup.selectItem(at: 0)
        }
        return popup
    }

    private func inputChannelPopup(deviceUID: String?) -> NSPopUpButton {
        let popup = NSPopUpButton()
        populateInputChannelPopup(popup, deviceUID: deviceUID)
        return popup
    }

    private func populateInputChannelPopup(_ popup: NSPopUpButton, deviceUID: String?) {
        popup.removeAllItems()
        let device: AudioInputDevice?
        if let deviceUID, !deviceUID.isEmpty {
            device = AudioInputDeviceCatalog.enumerate().first(where: { $0.uid == deviceUID })
        } else {
            device = AudioInputDeviceCatalog.defaultRecord()?.descriptor
        }
        let options = AudioInputSettingsOptions.channelOptions(
            storedPolicy: settings.inputChannelPolicy,
            availableChannels: device?.inputChannels
        )
        for option in options {
            popup.addItem(withTitle: option.title)
            let item = popup.item(at: popup.numberOfItems - 1)
            item?.representedObject = option.policy.rawValue
            item?.isEnabled = option.isEnabled
        }

        let rawValue = settings.inputChannelPolicy.rawValue
        if let item = popup.itemArray.last(where: { ($0.representedObject as? String) == rawValue }) {
            popup.select(item)
        }
    }

    private func buildDiagnosticsSection() -> SectionRuntime {
        let stateRow = buildValueRow(title: "App state")
        let clientRow = buildValueRow(title: "Client state")
        let audioRow = buildValueRow(title: "Audio")
        let serviceRow = buildValueRow(title: "Service probe")
        let errorRow = buildValueRow(title: "最近錯誤")
        let refresh = actionButton(title: "重新檢查服務", action: #selector(testService(_:)))
        let view = sectionStack(
            title: "診斷",
            subtitle: "確認麥克風 frame、RMS、服務與 WebSocket 狀態",
            symbolName: "stethoscope",
            views: [
                cardStack([stateRow.view, clientRow.view], title: "應用程式", symbolName: "app.badge"),
                cardStack([audioRow.view, serviceRow.view, errorRow.view], title: "連線與音訊", symbolName: "waveform.and.magnifyingglass"),
                cardStack([refresh], symbolName: "arrow.clockwise"),
            ]
        )

        func update() {
            stateRow.update(appState.displayStatus.title)
            clientRow.update(clientStateSummary())
            audioRow.update(audioSummary())
            serviceRow.update(serverSummary())
            errorRow.update(appState.serviceError?.localizedDescription ?? "無")
        }
        update()
        return SectionRuntime(view: view, update: update)
    }

    // MARK: - Actions

    @objc private func startDictation(_ sender: Any?) {
        if appState.mode == .dictation {
            onStopSession?()
        } else {
            onStartDictation?()
        }
    }

    @objc private func startMeeting(_ sender: Any?) {
        if appState.mode == .meeting {
            onStopSession?()
        } else {
            onStartMeeting?()
        }
    }

    @objc private func stopSession(_ sender: Any?) {
        onStopSession?()
    }

    @objc private func openPermissions(_ sender: Any?) {
        show(section: .permissions)
    }

    @objc private func refreshPermissions(_ sender: Any?) {
        permissions.refresh()
    }

    @objc private func permissionAction(_ sender: NSButton) {
        permissions.performPrimaryAction(for: permissionKind(for: sender))
    }

    @objc private func openPermissionSettings(_ sender: NSButton) {
        _ = permissions.openSettings(for: permissionKind(for: sender))
    }

    @objc private func audioDeviceSelectionChanged(_ sender: NSPopUpButton) {
        guard let channelPopup = controlsInSettingsView().inputChannel else { return }
        let uid = sender.selectedItem?.representedObject as? String
        populateInputChannelPopup(channelPopup, deviceUID: uid)
    }

    private func permissionTag(for kind: PermissionKind) -> Int {
        switch kind {
        case .microphone: return 0
        case .accessibility: return 1
        case .inputMonitoring: return 2
        }
    }

    private func permissionKind(for sender: NSButton) -> PermissionKind {
        switch sender.tag {
        case 0: return .microphone
        case 1: return .accessibility
        default: return .inputMonitoring
        }
    }

    @objc private func saveSettings(_ sender: Any?) {
        guard applySettingsFromForm() else { return }
        onSettingsChanged?()
        onRefreshService?()
        updateSection(.settings)
    }

    @objc private func testService(_ sender: Any?) {
        // Apply the values currently visible in the form first. Otherwise this
        // button probes the previous UserDefaults/token values and can report
        // a healthy service for a configuration the user did not enter.
        // The same action is also used by Diagnostics, where no settings form
        // is mounted; that path simply probes the already-saved configuration.
        let controls = controlsInSettingsView()
        if controls.host != nil || controls.port != nil || controls.token != nil {
            guard applySettingsFromForm() else { return }
        }
        onSettingsChanged?()
        onRefreshService?()
        updateSection(.settings)
        updateSection(.diagnostics)
    }

    @discardableResult
    private func applySettingsFromForm() -> Bool {
        let controls = controlsInSettingsView()
        guard
            let host = controls.host?.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            !host.isEmpty
        else {
            presentAlert("設定無效", "請輸入服務位址。")
            return false
        }
        guard
            let portText = controls.port?.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            let port = Int(portText),
            (1...65_535).contains(port)
        else {
            presentAlert("設定無效", "Port 必須是 1 到 65535 之間的數字。")
            return false
        }

        settings.host = host
        settings.port = port
        if let token = controls.token?.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) {
            guard saveToken(token) else { return false }
        }
        if let autoInsert = controls.autoInsert {
            settings.autoInsert = autoInsert.state == .on
        }
        if let preview = controls.preview {
            settings.revisablePreview = preview.state == .on
        }
        if let shortcut = controls.shortcut?.shortcut {
            settings.shortcut = shortcut
        }
        if let interactionMode = controls.interactionMode,
           let rawValue = interactionMode.selectedItem?.representedObject as? String,
           let mode = DictationInteractionMode(rawValue: rawValue) {
            settings.interactionMode = mode
        }
        if let feedback = controls.feedback {
            settings.startStopFeedback = feedback.state == .on
        }
        if let inputDevice = controls.inputDevice {
            settings.inputDeviceUID = inputDevice.selectedItem?.representedObject as? String
        }
        if let inputChannel = controls.inputChannel,
           let rawValue = inputChannel.selectedItem?.representedObject as? String {
            settings.inputChannelPolicy = AudioChannelPolicy(rawValue: rawValue)
        }
        return true
    }

    @objc private func exportMarkdown(_ sender: Any?) {
        let panel = NSSavePanel()
        let stamp = ISO8601DateFormatter().string(from: transcriptStartedAt).prefix(16)
        panel.nameFieldStringValue = "meeting-\(stamp).md"
        panel.allowedContentTypes = [.init(filenameExtension: "md")!]
        panel.begin { [weak self] response in
            guard response == .OK, let url = panel.url, let self else { return }
            do {
                try self.markdown().write(to: url, atomically: true, encoding: .utf8)
            } catch {
                self.presentAlert("Markdown 匯出失敗", error.localizedDescription)
            }
        }
    }

    private func appendTranscriptEntry(_ entry: TranscriptEntry) {
        transcriptEntries.append(entry)
        nextTranscriptSequence += 1
        autosaveTranscript()
    }

    private func orderedTranscriptEntries() -> [TranscriptEntry] {
        transcriptEntries.sorted {
            if $0.spokenAt == $1.spokenAt {
                return $0.sequence < $1.sequence
            }
            return $0.spokenAt < $1.spokenAt
        }
    }

    private func autosaveTranscript() {
        do {
            try markdown().write(to: autosaveURL, atomically: true, encoding: .utf8)
            autosaveStatus = "\(autosaveURL.lastPathComponent)（\(transcriptEntries.count) 段）"
        } catch {
            autosaveStatus = "失敗：\(error.localizedDescription)"
        }
    }

    private func markdown() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        var output = "# 會議記錄 \(formatter.string(from: transcriptStartedAt))\n\n"
        output += "> 由 TEA ASR 本機辨識產生，未經人工校訂。\n\n"
        for entry in orderedTranscriptEntries() {
            let text: String
            switch entry.kind {
            case .finalText:
                text = entry.text
            case .gap:
                text = "*—— \(entry.text) ——*"
            }
            let seconds = max(0, Int(entry.spokenAt.timeIntervalSince(transcriptStartedAt)))
            output += String(format: "- **[%02d:%02d]** %@\n", seconds / 60, seconds % 60, text)
        }
        return output
    }

    private func controlsInSettingsView() -> (
        host: NSTextField?, port: NSTextField?, token: NSSecureTextField?,
        autoInsert: NSButton?, preview: NSButton?, inputDevice: NSPopUpButton?,
        inputChannel: NSPopUpButton?, shortcut: ShortcutRecorderField?,
        interactionMode: NSPopUpButton?, feedback: NSButton?
    ) {
        var fields: [String: NSControl] = [:]
        func visit(_ view: NSView) {
            if let identifier = view.identifier?.rawValue, let control = view as? NSControl {
                fields[identifier] = control
            }
            view.subviews.forEach(visit)
        }
        visit(detailView)
        return (
            fields["host"] as? NSTextField,
            fields["port"] as? NSTextField,
            fields["token"] as? NSSecureTextField,
            fields["autoInsert"] as? NSButton,
            fields["preview"] as? NSButton,
            fields["inputDevice"] as? NSPopUpButton,
            fields["inputChannel"] as? NSPopUpButton,
            fields["shortcut"] as? ShortcutRecorderField,
            fields["interactionMode"] as? NSPopUpButton,
            fields["feedback"] as? NSButton
        )
    }

    @discardableResult
    private func saveToken(_ token: String) -> Bool {
        let directory = settings.tokenFile.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try token.write(to: settings.tokenFile, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes(
                [.posixPermissions: NSNumber(value: Int16(0o600))],
                ofItemAtPath: settings.tokenFile.path
            )
            return true
        } catch {
            presentAlert("Token 儲存失敗", error.localizedDescription)
            return false
        }
    }

    // MARK: - View helpers

    private func sectionStack(
        title: String,
        subtitle: String,
        symbolName: String? = nil,
        views: [NSView]
    ) -> NSView {
        let heading = NSTextField(labelWithString: title)
        heading.font = .systemFont(ofSize: 26, weight: .bold)
        let subheading = NSTextField(wrappingLabelWithString: subtitle)
        subheading.textColor = .secondaryLabelColor

        let headingRow = NSStackView()
        headingRow.orientation = .horizontal
        headingRow.alignment = .centerY
        headingRow.spacing = 10
        if let symbolName,
           let image = NSImage(systemSymbolName: symbolName, accessibilityDescription: title) {
            let icon = NSImageView(image: image)
            icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 20, weight: .semibold)
            icon.contentTintColor = .controlAccentColor
            icon.setContentHuggingPriority(.required, for: .horizontal)
            headingRow.addArrangedSubview(icon)
        }
        headingRow.addArrangedSubview(heading)

        let stack = NSStackView(views: [headingRow, subheading] + views)
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        for view in views {
            view.setContentHuggingPriority(.defaultLow, for: .horizontal)
            view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        }
        return stack
    }

    /// Builds the hero status card once. Its icon, title, detail text, mode
    /// badge and border tint all change with `statusPresentation()`, so
    /// `update()` recomputes and reassigns them directly instead of the
    /// caller rebuilding the card.
    private func buildStatusHero() -> (view: NSView, update: () -> Void) {
        let icon = NSImageView(image: NSImage())
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 25, weight: .semibold)
        icon.wantsLayer = true
        icon.layer?.cornerRadius = 15
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 54).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 54).isActive = true

        let title = NSTextField(labelWithString: "")
        title.font = .systemFont(ofSize: 18, weight: .semibold)
        let detail = stableLabel(font: .systemFont(ofSize: 12), maxLines: 2, color: .secondaryLabelColor)

        let text = NSStackView(views: [title, detail])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 4

        let badgeLabel = NSTextField(labelWithString: "")
        badgeLabel.font = .systemFont(ofSize: 11, weight: .semibold)
        badgeLabel.alignment = .center
        badgeLabel.lineBreakMode = .byTruncatingTail
        let badge = NSVisualEffectView()
        badge.material = .selection
        badge.state = .active
        badge.wantsLayer = true
        badge.layer?.cornerRadius = 8
        badge.layer?.borderWidth = 1
        badge.addSubview(badgeLabel)
        badgeLabel.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            badgeLabel.leadingAnchor.constraint(equalTo: badge.leadingAnchor, constant: 9),
            badgeLabel.trailingAnchor.constraint(equalTo: badge.trailingAnchor, constant: -9),
            badgeLabel.topAnchor.constraint(equalTo: badge.topAnchor, constant: 4),
            badgeLabel.bottomAnchor.constraint(equalTo: badge.bottomAnchor, constant: -4),
        ])
        badge.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [icon, text, badge])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12
        row.setCustomSpacing(8, after: text)

        let card = cardStack([row], material: .underWindowBackground)

        func update() {
            let presentation = statusPresentation()
            icon.image = NSImage(systemSymbolName: presentation.symbolName, accessibilityDescription: presentation.title) ?? NSImage()
            icon.contentTintColor = presentation.tint
            icon.layer?.backgroundColor = presentation.tint.withAlphaComponent(0.14).cgColor
            if title.stringValue != presentation.title { title.stringValue = presentation.title }
            if detail.stringValue != presentation.detail { detail.stringValue = presentation.detail }
            let mode = modeTitle()
            if badgeLabel.stringValue != mode { badgeLabel.stringValue = mode }
            badgeLabel.textColor = presentation.tint
            badge.layer?.borderColor = presentation.tint.withAlphaComponent(0.25).cgColor
            card.layer?.borderColor = presentation.tint.withAlphaComponent(0.26).cgColor
        }
        update()
        return (card, update)
    }

    private func statusPresentation() -> StatusPresentation {
        // AppState intentionally keeps the public status vocabulary small. The
        // service payload still tells us when the worker is idle-unloaded; show
        // that naturally as standby instead of making a healthy app look busy.
        let modelState = appState.serviceSnapshot?.status.modelState.lowercased()
        let standby = appState.mode == .idle && ["idle_unloaded", "standby"].contains(modelState)
        if standby {
            return StatusPresentation(
                title: "服務待命",
                detail: "模型尚未載入；開始聽寫或會議記錄時會自動準備。",
                symbolName: "moon.zzz.fill",
                tint: .systemOrange
            )
        }

        switch appState.displayStatus {
        case .checking:
            return StatusPresentation(title: "檢查服務中…", detail: "正在確認本機語音服務。", symbolName: "arrow.triangle.2.circlepath", tint: .systemBlue)
        case .offline:
            return StatusPresentation(title: "服務離線", detail: "啟動 tea-asr 服務後即可開始。", symbolName: "wifi.slash", tint: .systemRed)
        case .connecting:
            return StatusPresentation(title: "連線中…", detail: "正在連到本機語音服務。", symbolName: "point.3.connected.trianglepath.dotted", tint: .systemBlue)
        case .loading:
            return StatusPresentation(title: "模型準備中…", detail: "第一次開始聆聽可能需要幾秒鐘。", symbolName: "arrow.down.circle", tint: .systemOrange)
        case .standby:
            return StatusPresentation(title: "服務待命", detail: "模型尚未載入；開始聆聽時會自動準備。", symbolName: "moon.zzz.fill", tint: .systemOrange)
        case .ready:
            return StatusPresentation(title: "服務就緒", detail: "可以開始聽寫或開啟會議記錄。", symbolName: "checkmark.circle.fill", tint: .systemGreen)
        case .listening(let mode, let preview):
            let modeText = mode == .meeting ? "會議記錄" : "聽寫"
            let previewText = preview ? "，含即時預覽" : ""
            return StatusPresentation(title: "正在\(modeText)", detail: "正在接收麥克風音訊\(previewText)。", symbolName: "waveform.circle.fill", tint: .systemRed)
        case .retryable(let issue):
            return StatusPresentation(title: "可以重試", detail: issue.message, symbolName: "arrow.clockwise.circle", tint: .systemOrange)
        case .failed(let issue):
            return StatusPresentation(title: "需要處理", detail: issue.message, symbolName: "exclamationmark.triangle.fill", tint: .systemRed)
        }
    }

    private func metricPair(_ cards: [NSView]) -> NSView {
        let pair = NSStackView(views: cards)
        pair.orientation = .horizontal
        pair.alignment = .top
        pair.distribution = .fillEqually
        pair.spacing = 12
        return pair
    }

    /// A metric card's value label uses `stableLabel`, which reserves a fixed
    /// two-line height up front. That is what keeps the two overview metric
    /// cards equal height across updates without an explicit height
    /// constraint between them: neither card's content can grow past the
    /// space it already reserved.
    private func buildMetricCard(
        title: String,
        symbolName: String,
        tint: NSColor
    ) -> (view: NSView, update: (String) -> Void) {
        let icon = NSImageView(
            image: NSImage(systemSymbolName: symbolName, accessibilityDescription: title) ?? NSImage()
        )
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 16, weight: .semibold)
        icon.contentTintColor = tint
        icon.setContentHuggingPriority(.required, for: .horizontal)
        let label = NSTextField(labelWithString: title)
        label.font = .systemFont(ofSize: 11, weight: .semibold)
        label.textColor = .secondaryLabelColor
        let valueLabel = stableLabel(font: .systemFont(ofSize: 13, weight: .medium), maxLines: 2)
        let heading = NSStackView(views: [icon, label])
        heading.orientation = .horizontal
        heading.alignment = .centerY
        heading.spacing = 7
        let view = cardStack([heading, valueLabel], material: .contentBackground, accent: tint)
        return (view, { text in
            guard valueLabel.stringValue != text else { return }
            valueLabel.stringValue = text
        })
    }

    private func buildRecentTextCard() -> (view: NSView, update: () -> Void) {
        let label = stableLabel(font: .systemFont(ofSize: 14), maxLines: 3)
        let view = cardStack([label], title: "最近文字", symbolName: "text.quote")
        func update() {
            let text = appState.lastText ?? "尚無定稿文字"
            if label.stringValue != text { label.stringValue = text }
            label.textColor = appState.lastText == nil ? .secondaryLabelColor : .labelColor
        }
        update()
        return (view, update)
    }

    private func cardStack(
        _ views: [NSView],
        title: String? = nil,
        symbolName: String? = nil,
        material: NSVisualEffectView.Material = .contentBackground,
        accent: NSColor? = nil
    ) -> NSView {
        let contentViews: [NSView]
        if let title {
            let heading = NSTextField(labelWithString: title)
            heading.font = .systemFont(ofSize: 12, weight: .semibold)
            heading.textColor = .secondaryLabelColor
            if let symbolName,
               let image = NSImage(systemSymbolName: symbolName, accessibilityDescription: title) {
                let icon = NSImageView(image: image)
                icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 13, weight: .semibold)
                icon.contentTintColor = accent ?? .secondaryLabelColor
                let row = NSStackView(views: [icon, heading])
                row.orientation = .horizontal
                row.alignment = .centerY
                row.spacing = 7
                contentViews = [row] + views
            } else {
                contentViews = [heading] + views
            }
        } else {
            contentViews = views
        }

        let content = NSStackView(views: contentViews)
        content.orientation = .vertical
        content.alignment = .width
        content.spacing = 10
        content.translatesAutoresizingMaskIntoConstraints = false

        let card = NSVisualEffectView()
        card.material = material
        card.blendingMode = .withinWindow
        card.state = .active
        card.wantsLayer = true
        card.layer?.cornerRadius = 13
        card.layer?.borderWidth = 1
        card.layer?.borderColor = (accent ?? NSColor.separatorColor).withAlphaComponent(accent == nil ? 0.42 : 0.26).cgColor
        card.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 16),
            content.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -16),
            content.topAnchor.constraint(equalTo: card.topAnchor, constant: 14),
            content.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -14),
        ])
        return card
    }

    /// A label whose height is pinned to `maxLines` up front (rather than a
    /// minimum), so a status string going from one line to two — or back —
    /// never shifts the cards below it. Longer text truncates with an
    /// ellipsis instead of growing the layout.
    private func stableLabel(font: NSFont, maxLines: Int, color: NSColor? = nil) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: "")
        label.font = font
        label.maximumNumberOfLines = maxLines
        label.lineBreakMode = .byTruncatingTail
        if let color {
            label.textColor = color
        }
        let lineHeight = (font.ascender - font.descender + font.leading).rounded(.up)
        label.heightAnchor.constraint(equalToConstant: lineHeight * CGFloat(maxLines)).isActive = true
        return label
    }

    private func buildValueRow(title: String) -> (view: NSView, update: (String) -> Void) {
        let key = NSTextField(labelWithString: title)
        key.font = .systemFont(ofSize: 12, weight: .semibold)
        key.textColor = .secondaryLabelColor
        key.widthAnchor.constraint(equalToConstant: 100).isActive = true
        let value = stableLabel(font: .systemFont(ofSize: 13), maxLines: 2)
        let row = NSStackView(views: [key, value])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 12
        return (row, { text in
            guard value.stringValue != text else { return }
            value.stringValue = text
        })
    }

    private func actionButton(title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        button.controlSize = .large
        button.contentTintColor = .controlAccentColor
        button.setContentHuggingPriority(.required, for: .horizontal)
        return button
    }

    /// Builds the live transcript view once. `update()` only replaces the
    /// text-view string, and preserves the reader's scroll position unless
    /// they were already pinned to the bottom — matching the usual
    /// live-log convention of auto-following new lines only when the reader
    /// has not scrolled away to look at earlier text.
    private func buildTranscriptView() -> (view: NSScrollView, update: () -> Void) {
        let textView = NSTextView()
        textView.isEditable = false
        textView.isRichText = false
        textView.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        textView.textColor = .labelColor
        textView.backgroundColor = .clear
        textView.textContainerInset = NSSize(width: 10, height: 10)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.documentView = textView
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true

        func update() {
            let text = transcriptString()
            guard textView.string != text else { return }
            let wasAtBottom = isScrolledToBottom(scroll)
            textView.string = text
            if wasAtBottom {
                scrollToBottom(scroll)
            }
        }
        update()
        return (scroll, update)
    }

    private func isScrolledToBottom(_ scrollView: NSScrollView, tolerance: CGFloat = 24) -> Bool {
        guard let documentView = scrollView.documentView else { return true }
        let visibleMaxY = scrollView.contentView.bounds.maxY
        return documentView.frame.height - visibleMaxY <= tolerance
    }

    private func scrollToBottom(_ scrollView: NSScrollView) {
        guard let documentView = scrollView.documentView else { return }
        documentView.scrollToVisible(NSRect(x: 0, y: max(0, documentView.frame.height - 1), width: 1, height: 1))
    }

    private func transcriptString() -> String {
        var output = orderedTranscriptEntries().map { entry in
            switch entry.kind {
            case .finalText:
                return "\(timestamp(entry.spokenAt))  \(entry.text)"
            case .gap:
                return "\(timestamp(entry.spokenAt))  —— \(entry.text) ——"
            }
        }
        if !partialText.isEmpty {
            output.append("\(timestamp(partialSpokenAt ?? Date()))  … \(partialText)")
        }
        if output.isEmpty {
            return "尚無文字。開始聽寫或會議記錄後，定稿內容會顯示在這裡。"
        }
        return output.joined(separator: "\n")
    }

    private func timestamp(_ moment: Date) -> String {
        let seconds = max(0, Int(moment.timeIntervalSince(transcriptStartedAt)))
        return String(format: "[%02d:%02d]", seconds / 60, seconds % 60)
    }

    private func modeTitle() -> String {
        switch appState.mode {
        case .idle: return "閒置"
        case .dictation: return "聽寫"
        case .meeting: return "會議記錄"
        }
    }

    private func serverSummary() -> String {
        if let error = appState.serviceError {
            return error.localizedDescription
        }
        guard let snapshot = appState.serviceSnapshot else {
            return appState.serviceReachable == false ? "無法連線" : "檢查中…"
        }
        switch snapshot.status.modelState.lowercased() {
        case "idle_unloaded", "standby":
            return "待命（\(settings.host):\(settings.port)）"
        default:
            return snapshot.readyzOK ? "可用（\(settings.host):\(settings.port)）" : "未就緒"
        }
    }

    private func modelSummary() -> String {
        guard let status = appState.serviceSnapshot?.status else { return "尚未取得" }
        let state: String
        switch status.modelState.lowercased() {
        case "idle_unloaded", "standby": state = "待命（尚未載入）"
        case "loading": state = "載入中"
        case "ready": state = "就緒"
        case "recovering": state = "恢復中"
        case "failed": state = "失敗"
        default: state = status.modelState
        }
        var value = "\(state) · \(status.model)"
        if let load = status.workerLoadMs {
            value += " · 載入 \(load) ms"
        }
        return value
    }

    private func audioSummary() -> String {
        guard let diagnostics = audioDiagnostics else { return "尚未開始擷取" }
        let device = diagnostics.inputDeviceName ?? "預設輸入裝置未知"
        let rms = diagnostics.rms.map { String(format: "%.3f", $0) } ?? "--"
        return "\(device) · \(diagnostics.isRunning ? "執行中" : "已停止") · RMS \(rms) · \(diagnostics.framesProduced) 幀"
    }

    private func clientStateSummary() -> String {
        switch appState.clientState {
        case .idle: return "閒置"
        case .connecting: return "連線中"
        case .loadingModel: return "模型載入中"
        case .listening(let version, let preview):
            return "聆聽中 · 協定 \(version) · 預覽 \(preview ? "開" : "關")"
        case .failed(let issue): return "失敗 · \(issue.code)：\(issue.message)"
        }
    }

    private func presentAlert(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.runModal()
    }
}

#if DEBUG
extension MainWindowController {
    /// Test-only introspection of the build-once/update-in-place refactor:
    /// the section view currently mounted in the detail pane, so a test can
    /// assert its identity is stable (`===`) across status updates instead
    /// of being torn down and recreated. Kept behind `DEBUG` so it never
    /// ships as part of the app's runtime surface.
    var debugMountedSectionView: NSView? { detailView.subviews.first }

    /// Every `NSTextField.stringValue` currently under the detail pane, for
    /// asserting that an update actually reached the label it targeted.
    func debugLabelTexts() -> [String] {
        var texts: [String] = []
        func visit(_ view: NSView) {
            if let field = view as? NSTextField {
                texts.append(field.stringValue)
            }
            view.subviews.forEach(visit)
        }
        detailView.subviews.forEach(visit)
        return texts
    }
}
#endif
