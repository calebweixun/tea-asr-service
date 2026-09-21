import AppKit
import Foundation

/// The one user-facing window for the menu-bar app.
///
/// This intentionally stays AppKit-only.  The controller owns presentation and
/// transcript state, while AppController remains the owner of audio/session
/// side effects.  That makes the same window useful when the app is launched
/// without opening a session (for example, to fix a missing permission).
/// A document view for the detail scroll view that grows downwards.
///
/// AppKit's default (non-flipped) coordinate system parks a document view that
/// is shorter than its clip view against the *bottom* of the scroll view, which
/// left every section floating in the lower half of the window once the pages
/// stopped being tall padded cards. Flipping it makes short content start at
/// the top, the way every scrolling pane in a Mac app behaves.
private final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

/// A split view controller whose sidebar is furniture, not a resizable pane.
///
/// `minimumThickness == maximumThickness` already pins the sidebar's width,
/// but AppKit still draws a draggable divider there: the user can grab it,
/// see the cursor change, and get nothing — a control that does not respond
/// is worse than no control. Reporting an empty effective drag rect removes
/// the hit area entirely, which is exactly how Mail and System Settings
/// behave (their sidebars are one fixed width and the divider is inert).
private final class FixedSidebarSplitViewController: NSSplitViewController {
    override func splitView(
        _ splitView: NSSplitView,
        effectiveRect proposedEffectiveRect: NSRect,
        forDrawnRect drawnRect: NSRect,
        ofDividerAt dividerIndex: Int
    ) -> NSRect {
        .zero
    }
}

@MainActor
final class MainWindowController: NSWindowController, NSWindowDelegate {
    /// Fixed sidebar thickness (see `sidebarItem.minimumThickness` in
    /// `build()`, and `windowWillResize` for why the window itself also
    /// needs defending).
    private let sidebarWidth: CGFloat = 210
    private let minimumWindowSize = NSSize(width: 780, height: 520)

    /// The one spacing/sizing scale for the detail pane. Everything here is a
    /// multiple of 4 so neighbouring groups, rows and controls share a visible
    /// rhythm instead of each picking its own number (the layout used to mix
    /// 5/7/10/12/13/14/16/18/22).
    private enum Metrics {
        /// Margin between the detail pane's edge and its content.
        static let edge: CGFloat = 20
        /// Between two top-level groups in a section.
        static let group: CGFloat = 24
        /// Between rows inside a group, and between adjacent buttons.
        static let row: CGFloat = 12
        /// Group title to its separator, and settings-grid row spacing.
        static let tight: CGFloat = 8
        /// Title to its own subordinate line.
        static let hair: CGFloat = 4
        /// Fixed width of the sidebar icon column.
        static let sidebarIconColumn: CGFloat = 20
        /// Shared width of the leading label column, so every value and every
        /// settings control starts on the same vertical line.
        static let labelColumn: CGFloat = 100
        /// Floor for a row-trailing action button, so buttons in the same
        /// column line up on both edges instead of being ragged.
        static let actionWidth: CGFloat = 116
        /// Minimum width of a settings control.
        static let fieldWidth: CGFloat = 250
    }

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
        case settings
        case diagnostics
        case logs

        var title: String {
            switch self {
            case .overview: return "總覽"
            case .operations: return "操作"
            case .settings: return "設定"
            case .diagnostics: return "診斷與權限"
            case .logs: return "日誌"
            }
        }

        var symbolName: String {
            switch self {
            case .overview: return "rectangle.3.group"
            case .operations: return "mic"
            case .settings: return "gearshape"
            case .diagnostics: return "stethoscope"
            case .logs: return "doc.plaintext"
            }
        }
    }

    private let settings: Settings
    private let appState: AppState
    private let permissions: PermissionCoordinator
    private let logsClient: LogsFetching

    private let splitViewController = FixedSidebarSplitViewController()
    private let sidebarController = NSViewController()
    private let detailController = NSViewController()
    private let detailView = FlippedView()
    private var sectionButtons: [NSButton] = []
    private var selectedSection: Section = .overview
    private var sectionRuntimes: [Section: SectionRuntime] = [:]
    private let audioLevelMonitor = AudioLevelMonitor(callbackQueue: .main)
    private let audioLevelBar = AudioLevelBarView()
    private static let inputDeviceEnumerationErrorUID = "__tea_audio_input_enumeration_error__"
    private static let inputDeviceSkippedDevicesUID = "__tea_audio_input_skipped_devices__"
    private var isWindowOpen = true
    #if DEBUG
    private(set) var monitorStartCount = 0
    #endif

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

    // MARK: - 本機服務控制（設定頁）
    //
    // Only ever set by `toggleManagedService`/`runModelPrepare` below, and
    // only ever holding a `Process` this controller itself launched — see
    // `ManagedProcess`'s own doc comment for why that is what makes `stop`
    // safe to offer at all. `nil` means "this app has not started a service
    // this run", which is also the trigger for the Logs page's "服務不是由
    // 這個 app 啟動" notice — a service can be reachable and healthy while
    // this stays `nil` if it was started by the menu bar, a LaunchAgent, or
    // a developer's own terminal.
    private var managedService: ManagedProcess?
    private var modelPrepareProcess: ManagedProcess?

    /// Cached answer from `ServiceControl.agentInstalled`, refreshed off the
    /// main thread by `refreshServiceLoginItemState()`. `nil` means "not
    /// probed yet" — the same tri-state `AppController`'s old menu-bar
    /// auto-start item used, and for the same reason.
    private var serviceLoginItemInstalled: Bool?
    private var serviceLoginItemProbeInFlight = false

    // MARK: - 日誌頁狀態
    //
    // The level defaults to `.warning` (warning + error), not `.debug`: the
    // whole point of this page is that an error is visible at a glance the
    // moment it opens, without being buried under routine debug/info noise.
    // `limit` sits well inside the server's 1–500 ceiling — enough recent
    // context without a pathologically large single response.
    private var logsSelectedLevel: LogLevel = .warning
    private let logsLimit = 200
    private var logsEntries: [LogEntry] = []
    private var logsHasMore = false
    private var logsFetchState: LogsFetchState = .idle
    /// Kept only so `#if DEBUG` tests can read back the rendered log lines;
    /// the text view itself lives inside the section's own view tree and is
    /// otherwise addressed only through `update()`.
    private var logsTextView: NSTextView?
    /// Which of the Logs page's two tabs is showing: the structured
    /// `/v1/logs` feed, or the managed service process's own stdout/stderr.
    private var logsTab: LogsTab = .structured
    /// Same reasoning as `logsTextView` above, for the "服務輸出" tab.
    private var logsServiceOutputTextView: NSTextView?

    private enum LogsTab: Int {
        case structured
        case serviceOutput
    }
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
    var onShortcutEditorWillBegin: (() -> Void)?
    var onShortcutEditorDidEnd: (() -> Void)?

    init(
        settings: Settings,
        appState: AppState,
        permissions: PermissionCoordinator,
        logsClient: LogsFetching = LogsClient()
    ) {
        self.settings = settings
        self.appState = appState
        self.permissions = permissions
        self.logsClient = logsClient

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 980, height: 650),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "TEA ASR"
        window.minSize = minimumWindowSize
        // `minSize` only floors interactive (mouse-drag) resizing. Once the
        // detail pane's content is fully width-determinate bottom-up (see
        // `stretchArrangedSubviewsToFullWidth`), assigning `contentViewController`
        // below makes AppKit auto-size the window to that content's computed
        // fitting size on every layout pass — including the very first one,
        // where the fitting size can come out smaller than the window we
        // actually want. `contentMinSize` is the floor that mechanism itself
        // respects; without it the window could shrink itself well under
        // 780x520 the moment a section's view tree finishes laying out.
        window.contentMinSize = minimumWindowSize
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
        audioLevelMonitor.onState = { [weak self] state in
            self?.audioLevelBar.setState(state)
        }
        audioLevelMonitor.onLevel = { [weak self] sample in
            self?.audioLevelBar.setLevel(sample)
        }
        build()
    }

    deinit {
        audioLevelMonitor.stop()
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
            // Once the detail pane's content is fully width-determinate
            // bottom-up (see `stretchArrangedSubviewsToFullWidth`), AppKit's
            // split-view divider negotiation starts consulting the detail
            // pane's own "compressed fitting size" to decide how much room
            // it actually needs — and that computation treats every
            // wrapping label in it as shrinkable to near zero (low
            // compression resistance is exactly what lets long text wrap
            // instead of overflowing). Left alone, that tiny reported need
            // starves the negotiation and the sidebar absorbs the
            // difference, growing far past its intended width. Giving the
            // scroll view itself a real floor — exactly the width the
            // detail pane gets at the window's minimum size — means it
            // never reports needing less room than that, regardless of what
            // its content is currently able to compress to.
            detailScroll.widthAnchor.constraint(greaterThanOrEqualToConstant: minimumWindowSize.width - sidebarWidth),
        ])
        let sidebarItem = NSSplitViewItem(sidebarWithViewController: sidebarController)
        sidebarItem.minimumThickness = sidebarWidth
        sidebarItem.maximumThickness = sidebarWidth
        // A sidebar item collapses on two triggers that have nothing to do
        // with the divider: the toolbar's sidebar button, and AppKit's own
        // "window got narrow, fold the sidebar away" behaviour. With only
        // five destinations and a floored window width there is nothing to
        // gain from either, and a page whose navigation can silently vanish
        // is not something System Settings does.
        sidebarItem.canCollapse = false
        // `minimumThickness == maximumThickness` already fixes the width, so
        // the holding priority is deliberately left at the stock sidebar
        // value — raising it would re-enter the width negotiation that the
        // detail scroll view's floor above was added to settle.
        splitViewController.addSplitViewItem(sidebarItem)
        splitViewController.addSplitViewItem(
            NSSplitViewItem(viewController: detailController)
        )
        splitViewController.splitView.setPosition(sidebarWidth, ofDividerAt: 0)
        window?.contentViewController = splitViewController
        splitViewController.view.widthAnchor
            .constraint(greaterThanOrEqualToConstant: minimumWindowSize.width).isActive = true
        splitViewController.view.heightAnchor
            .constraint(greaterThanOrEqualToConstant: minimumWindowSize.height).isActive = true
        // Assigning `contentViewController` makes AppKit size the window to the
        // content's fitting size (see the `contentMinSize` note above). Now
        // that the sections are plain rows rather than tall padded cards, that
        // fitting size is small enough that the window would open at its bare
        // minimum. Restore the intended roomy default afterwards; the minimum
        // still applies to everything the user does from here.
        window?.setContentSize(NSSize(width: 980, height: 650))
        window?.center()

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
        stack.spacing = Metrics.hair
        stack.translatesAutoresizingMaskIntoConstraints = false
        sectionButtons = Section.allCases.map { section in
            let button = NSButton(
                title: section.title,
                target: self,
                action: #selector(selectSection(_:))
            )
            button.tag = section.rawValue
            let symbol = NSImage(systemSymbolName: section.symbolName, accessibilityDescription: section.title)
            let imageSize = NSSize(
                width: Metrics.sidebarIconColumn + Metrics.tight,
                height: Metrics.sidebarIconColumn
            )
            button.image = NSImage(size: imageSize, flipped: false) { _ in
                guard let symbol else { return false }
                let symbolSize = symbol.size
                let symbolRect = NSRect(
                    x: 0,
                    y: max(0, (Metrics.sidebarIconColumn - symbolSize.height) / 2),
                    width: symbolSize.width,
                    height: symbolSize.height
                )
                symbol.draw(
                    in: symbolRect,
                    from: .zero,
                    operation: .sourceOver,
                    fraction: 1
                )
                return true
            }
            button.image?.isTemplate = true
            button.imagePosition = .imageLeading
            button.imageHugsTitle = false
            button.imageScaling = .scaleNone
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

        root.addSubview(heading)
        root.addSubview(hint)
        root.addSubview(stack)
        // Activate this constraint only after the stack has joined the root's
        // hierarchy. Activating it while both views are detached makes AppKit
        // raise an Auto Layout exception because there is no common ancestor.
        for button in sectionButtons {
            button.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        NSLayoutConstraint.activate([
            heading.topAnchor.constraint(equalTo: root.topAnchor, constant: Metrics.edge),
            heading.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
            hint.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 0),
            hint.leadingAnchor.constraint(equalTo: heading.leadingAnchor),
            stack.topAnchor.constraint(equalTo: hint.bottomAnchor, constant: Metrics.edge),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: Metrics.row),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -Metrics.row),
        ])
        sidebarController.view = root
        select(section: .overview)
    }

    func show(section: Section = .overview) {
        isWindowOpen = true
        select(section: section)
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Kept as a session-start hook for AppController.  The level monitor and
    /// recorder now subscribe to one process-wide input source, so starting a
    /// dictation session must not stop the settings meter.
    func hideForDictation() {
        // TextInjector captures the target before this hook is called.  There
        // is intentionally no audio work here.
    }

    func refresh() {
        if selectedSection == .settings && isWindowOpen {
            if audioLevelMonitor.state == .idle {
                startAudioLevelMonitor()
            }
        }
        // Health polls and client transitions must not rebuild settings fields
        // while the user is editing unsaved values.
        guard selectedSection != .settings else { return }
        // Diagnostics now also carries the permission list, so a visible
        // Diagnostics page must re-read TCC as well as the app's own state.
        if selectedSection == .diagnostics {
            permissions.refresh()
        }
        updateSection(selectedSection)
    }

    func windowWillClose(_ notification: Notification) {
        isWindowOpen = false
        stopAudioLevelMonitor()
    }

    func windowDidBecomeKey(_ notification: Notification) {
        permissions.refresh()
    }

    /// Defends the window's minimum size against AppKit's own auto-layout
    /// window sizing (see the long comment in `build()`): once the detail
    /// pane's content became fully width-determinate, that mechanism could
    /// shrink the window below `contentMinSize` on the very first layout
    /// pass, before this delegate method — or `contentMinSize` itself —
    /// otherwise gets a chance to push back. Clamping here catches every
    /// resize attempt, internal or user-driven.
    func windowWillResize(_ sender: NSWindow, to frameSize: NSSize) -> NSSize {
        // `frameSize` is the whole window (title bar included); `minSize`
        // is already expressed in that same frame coordinate space, unlike
        // `contentMinSize`, so compare against it directly.
        NSSize(
            width: max(frameSize.width, sender.minSize.width),
            height: max(frameSize.height, sender.minSize.height)
        )
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
        // Logs are pulled on demand only: switching into the page or hitting
        // its own refresh button, never a background timer (see the note on
        // `refresh()`). This is the "switching to the page" half of that
        // contract.
        if section == .logs {
            fetchLogs()
        }
    }

    /// The permission rows live in Diagnostics now, so a TCC change refreshes
    /// that section — through the same build-once + update closure every other
    /// state change uses, never by rebuilding the view tree.
    private func refreshPermissionSection() {
        updateSection(.diagnostics)
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
                runtime.view.leadingAnchor.constraint(equalTo: detailView.leadingAnchor, constant: Metrics.edge),
                runtime.view.trailingAnchor.constraint(equalTo: detailView.trailingAnchor, constant: -Metrics.edge),
                runtime.view.topAnchor.constraint(equalTo: detailView.topAnchor, constant: Metrics.edge),
                runtime.view.bottomAnchor.constraint(equalTo: detailView.bottomAnchor, constant: -Metrics.edge),
            ])
        }
        // Refresh unconditionally: state-change methods below update every
        // section that displays that state, not only the visible one, so a
        // section can go stale while it is not mounted and must catch up here.
        runtime.update()
        if selectedSection == .settings {
            startAudioLevelMonitor()
        } else {
            stopAudioLevelMonitor()
        }
    }

    private func sectionRuntime(for section: Section) -> SectionRuntime {
        if let cached = sectionRuntimes[section] { return cached }
        let runtime: SectionRuntime
        switch section {
        case .overview: runtime = buildOverviewSection()
        case .operations: runtime = buildOperationsSection()
        case .settings: runtime = buildSettingsSection()
        case .diagnostics: runtime = buildDiagnosticsSection()
        case .logs: runtime = buildLogsSection()
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
        // "服務" can render a full service error (via serverSummary()), so it
        // opts out of the fixed-height row and is allowed to grow — see
        // `stableLabel`. "模型" stays fixed since its value is always a short
        // status/model-name summary.
        let service = buildValueRow(title: "服務", maxLines: nil)
        let model = buildValueRow(title: "模型")
        let sessionRow = buildValueRow(title: "Session")
        // audioSummary() concatenates a user-named input device with three
        // more fields, so it is the one row here that really can wrap.
        let audioRow = buildValueRow(title: "輸入", maxLines: 2)
        let runtimeGroup = group(
            title: "執行狀態",
            views: [service.view, model.view, sessionRow.view, audioRow.view]
        )
        let recent = buildRecentTextCard()

        let startDictation = actionButton(title: "開始聽寫", action: #selector(startDictation(_:)))
        let startMeeting = actionButton(title: "開始會議記錄", action: #selector(startMeeting(_:)))
        let permissionsButton = actionButton(title: "檢查權限", action: #selector(openPermissions(_:)))
        let actions = buttonRow([startDictation, startMeeting, permissionsButton])

        let view = sectionStack(views: [
            hero.view,
            runtimeGroup,
            recent.view,
            actions,
        ])

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
        let controls = buttonRow([startDictation, startMeeting, stop])

        // All three can wrap: sessionStatus grows a "錄音有間隔：<reason>"
        // suffix, audioSummary() carries a device name, and autosaveStatus
        // becomes "失敗：<error>". They are also the rows that change on
        // every session event, so they keep a fixed (two-line) height rather
        // than resizing with their text.
        let statusRow = buildValueRow(title: "狀態", maxLines: 2)
        let audioRow = buildValueRow(title: "音訊", maxLines: 2)
        let transcriptInfoRow = buildValueRow(title: "自動存檔", maxLines: 2)
        let transcript = buildTranscriptView()
        let export = actionButton(title: "匯出 Markdown…", action: #selector(exportMarkdown(_:)))

        let view = sectionStack(views: [
            group(title: "開始錄音", views: [controls]),
            group(title: "目前 session", views: [statusRow.view, audioRow.view, transcriptInfoRow.view]),
            group(title: "即時逐字稿", views: [transcript.view]),
            export,
        ])

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

    /// The permission checklist, as one group that Diagnostics places
    /// alongside its read-only groups. It used to be a section of its own; a
    /// sidebar destination that holds three rows the user visits twice in the
    /// app's lifetime is not a destination, it is a group on the page where
    /// you already go to find out why something is not working.
    private func buildPermissionsGroup() -> (view: NSView, update: () -> Void) {
        let summary = NSTextField(wrappingLabelWithString: "")
        let rows = PermissionKind.allCases.map { buildPermissionRow(for: $0) }
        let permissionRows = NSStackView(views: rows.map(\.view))
        permissionRows.orientation = .vertical
        permissionRows.alignment = .width
        permissionRows.spacing = Metrics.row
        stretchArrangedSubviewsToFullWidth(permissionRows)
        summary.textColor = .secondaryLabelColor
        summary.font = .systemFont(ofSize: 12)
        let refresh = actionButton(title: "重新檢查權限", action: #selector(refreshPermissions(_:)))
        let refreshRow = buttonRow([refresh])
        let view = group(title: "權限", views: [summary, permissionRows, refreshRow])

        func update() {
            let state = permissions.state
            let summaryText = state.requiredPermissionsGranted
                ? "必要權限已具備。若要自動貼上，仍需允許\(SystemPermissionNaming.accessibilityTitle)。"
                : "請完成下列必要權限；完成後回到此視窗，狀態會自動更新。"
            if summary.stringValue != summaryText { summary.stringValue = summaryText }
            for row in rows {
                row.update(state.item(for: row.kind))
            }
        }
        update()
        return (view, update)
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
        statusIcon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)
        statusIcon.setContentHuggingPriority(.required, for: .horizontal)
        let title = NSTextField(labelWithString: "")
        title.font = .systemFont(ofSize: 13)
        title.textColor = .labelColor
        // "Why this permission" is explanatory copy, not the status itself —
        // the status (已允許/未允許/…) stays on `title` below, always
        // visible; only the "why" moves behind a click. `explanation` is
        // initialised with the kind's current text so the button is never
        // momentarily empty before the first `update()` call.
        let explanationInfo = InfoButton(explanation: kind.explanation)
        let titleRow = NSStackView(views: [statusIcon, title, explanationInfo])
        titleRow.orientation = .horizontal
        titleRow.alignment = .centerY
        titleRow.spacing = Metrics.hair + 2

        let text = NSStackView(views: [titleRow])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = Metrics.hair

        let action = NSButton(title: "", target: self, action: nil)
        action.bezelStyle = .rounded
        action.setContentHuggingPriority(.required, for: .horizontal)
        // Every row's button shares one floor width, so "允許麥克風" and
        // "開啟設定" line up on both edges instead of being ragged.
        action.widthAnchor.constraint(greaterThanOrEqualToConstant: Metrics.actionWidth).isActive = true
        action.isHidden = true

        let row = NSStackView(views: [text, action])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = Metrics.row
        // NSStackView's default gravity layout packs every subview against the
        // leading edge, which is what left each row's button parked right
        // after its own explanation text — so the buttons formed a ragged
        // diagonal instead of a column. `.fill` hands the slack to the text
        // (low hugging) and keeps the button (required hugging) at its floor
        // width, pinned to the row's trailing edge.
        row.distribution = .fill

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
            // The row's text stays in the system label colour; only the small
            // leading symbol carries the state, and only red/orange mean
            // something (see severityColor).
            let statusIconName = item.isSatisfied ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
            statusIcon.image = NSImage(systemSymbolName: statusIconName, accessibilityDescription: statusText) ?? NSImage()
            statusIcon.contentTintColor = severityColor(color)
            explanationInfo.explanation = item.explanation

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
        let hotKey = ShortcutButton(shortcut: settings.shortcut)
        hotKey.identifier = NSUserInterfaceItemIdentifier("shortcut")
        hotKey.onRequestEdit = { [weak self] in
            self?.editShortcut()
        }
        let serviceExecutable = NSTextField(string: settings.serviceExecutable)
        serviceExecutable.identifier = NSUserInterfaceItemIdentifier("serviceExecutable")
        serviceExecutable.placeholderString = "留空以自動尋找（PATH、專案 .venv/bin/tea-asr…）"
        let chooseExecutable = actionButton(title: "選擇檔案…", action: #selector(chooseServiceExecutable(_:)))
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
        for field in [host, port, token, hotKey, serviceExecutable] as [NSControl] {
            // An exact width, not a floor: a bordered NSTextField has no
            // intrinsic width to hug, so a floor alone lets it absorb all the
            // slack in the grid's control column and run to the pane's edge —
            // which left the text fields full-width next to 250pt popups.
            field.widthAnchor.constraint(equalToConstant: Metrics.fieldWidth).isActive = true
            field.setContentHuggingPriority(.required, for: .horizontal)
        }
        for popup in [interactionMode, inputDevice, inputChannel] {
            popup.widthAnchor.constraint(greaterThanOrEqualToConstant: Metrics.fieldWidth).isActive = true
            popup.setContentHuggingPriority(.required, for: .horizontal)
        }
        audioLevelBar.widthAnchor.constraint(equalToConstant: Metrics.fieldWidth).isActive = true
        audioLevelBar.heightAnchor.constraint(equalToConstant: 18).isActive = true
        audioLevelBar.setContentHuggingPriority(.required, for: .horizontal)
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
        inputChannel.target = self
        inputChannel.action = #selector(audioChannelSelectionChanged(_:))
        host.action = #selector(saveSettings(_:))
        port.action = #selector(saveSettings(_:))
        token.action = #selector(saveSettings(_:))
        serviceExecutable.target = self
        serviceExecutable.action = #selector(saveSettings(_:))
        interactionMode.target = self
        interactionMode.action = #selector(saveSettings(_:))

        // Explanatory (not warning) copy that used to sit as standing labels
        // under the connection grid. The wording is untouched — see the
        // classification in the task report — only the presentation moved
        // into a click-to-reveal disclosure next to the field it explains.
        let addressAndLanExplanation = "這個 app 本身就是服務所在的電腦，服務位址預設是本機（127.0.0.1）。"
            + "只有在要連到另一台主機上執行的服務時才需要修改。"
            + "\n\n"
            + "尚未開放外部（區網）連入：TLS 加密、身分授權與流量限制都還沒有實作，"
            + "貿然開放會讓區網內任何裝置未經驗證就能存取語音與逐字稿，因此這個版本只能連本機。"
        let hostInfo = InfoButton(explanation: addressAndLanExplanation)
        let hostRow = NSStackView(views: [host, hostInfo])
        hostRow.orientation = .horizontal
        hostRow.alignment = .centerY
        hostRow.spacing = Metrics.hair + 2

        let tokenExplanation = "Token 只寫入 ~/Library/Application Support/TEA ASR/token，不會放進偏好設定。"
        let tokenInfo = InfoButton(explanation: tokenExplanation)
        let tokenRow = NSStackView(views: [token, tokenInfo])
        tokenRow.orientation = .horizontal
        tokenRow.alignment = .centerY
        tokenRow.spacing = Metrics.hair + 2

        let shortcutTipExplanation = "按一下快捷鍵按鈕即可修改。"
        let shortcutInfo = InfoButton(explanation: shortcutTipExplanation)
        let hotKeyRow = NSStackView(views: [hotKey, shortcutInfo])
        hotKeyRow.orientation = .horizontal
        hotKeyRow.alignment = .centerY
        hotKeyRow.spacing = Metrics.hair + 2

        let executableRow = NSStackView(views: [serviceExecutable, chooseExecutable])
        executableRow.orientation = .horizontal
        executableRow.alignment = .centerY
        executableRow.spacing = Metrics.row

        // Read-only: reflects the address/port already saved in `settings` and
        // whatever the last service probe found, both already available on
        // `appState`. It never invents a field the server doesn't expose.
        let connectionSummary = stableLabel(font: .systemFont(ofSize: 13), maxLines: 2, color: .secondaryLabelColor)
        let grid = settingsGrid([
            [settingsLabel("連線狀態"), connectionSummary],
            [settingsLabel("服務位址（本機）"), hostRow],
            [settingsLabel("Port"), port],
            [settingsLabel("Token"), tokenRow],
            [settingsLabel("快捷鍵"), hotKeyRow],
            [settingsLabel("互動模式"), interactionMode],
        ])
        // Diagnostic, not decorative: unlike the info buttons above, this
        // must stay a plain always-visible label. "找不到 tea-asr 執行檔" is
        // exactly the kind of actionable failure reason the app used to have
        // nowhere to show — hiding it behind a click would recreate the same
        // problem this page exists to fix.
        let executableStatus = stableLabel(font: .systemFont(ofSize: 12), maxLines: nil, color: .secondaryLabelColor)
        let executableGrid = settingsGrid([
            [settingsLabel("服務執行檔"), executableRow],
            [NSGridCell.emptyContentView, executableStatus],
        ])

        // Start/stop control. The status line is the one place this app can
        // honestly say what it knows: whether *it* started the process that
        // is running, not merely whether something is answering on the port.
        // The button's own semantics are "restart" rather than plain start:
        // this app is the service's main runtime, so quitting it always
        // stops whatever it launched (see `ServiceQuitPolicy`), and there is
        // no separate opt-out setting for that any more — the one control
        // this page keeps is the one that gets the service running again.
        let serviceControlStatus = stableLabel(font: .systemFont(ofSize: 12), maxLines: nil, color: .secondaryLabelColor)
        let serviceControlButton = actionButton(title: "重新啟動服務", action: #selector(toggleManagedService(_:)))
        serviceControlButton.identifier = NSUserInterfaceItemIdentifier("serviceControlButton")
        // TEA ASR is the service's main runtime: there is no setting to opt
        // out of stopping it on quit any more (see `ServiceQuitPolicy`), so
        // this note explains the unconditional behaviour and its one honest
        // limit, rather than a checkbox that used to control it.
        let quitBehaviorNote = stableLabel(font: .systemFont(ofSize: 12), maxLines: nil, color: .secondaryLabelColor)
        quitBehaviorNote.stringValue =
            "結束 TEA ASR 時，一定會一併停止本 app 啟動的服務。"
            + "只對這個 app 啟動的服務行程有效——由 LaunchAgent 或你自己在終端機啟動的服務不會被動到。"
            + "強制結束（Force Quit）或當機時也無法停止服務。"
        let serviceControlGrid = settingsGrid([
            [settingsLabel("執行狀態"), serviceControlStatus],
            [NSGridCell.emptyContentView, buttonRow([serviceControlButton])],
            [NSGridCell.emptyContentView, quitBehaviorNote],
        ])

        // Login-time autostart for the *service* (a LaunchAgent installed by
        // `tea-asr service install`) — distinct from the app's own "登入時
        // 自動開啟程式" in the menu bar, which uses `SMAppService.mainApp` to
        // open this app itself at login. This checkbox acts immediately, the
        // same way `serviceControlButton` does, rather than waiting for
        // "儲存設定": it is a direct reflection of `launchctl` state, not a
        // persisted preference.
        let serviceLoginItem = NSButton(
            checkboxWithTitle: "登入時自動啟動服務（LaunchAgent）",
            target: self,
            action: #selector(toggleServiceLoginItem(_:))
        )
        serviceLoginItem.identifier = NSUserInterfaceItemIdentifier("serviceLoginItem")
        let serviceLoginItemStatus = stableLabel(font: .systemFont(ofSize: 12), maxLines: nil, color: .secondaryLabelColor)
        let serviceLoginItemGrid = settingsGrid([
            [settingsLabel("登入時啟動"), serviceLoginItem],
            [NSGridCell.emptyContentView, serviceLoginItemStatus],
        ])

        // Model info + the one model action this build actually has: asking
        // the server to (re-)run its own `model-prepare`. There is no
        // variant switch here — see the info button's explanation — because
        // neither the CLI nor the API currently accept one.
        let modelValue = stableLabel(font: .systemFont(ofSize: 12), maxLines: nil, color: .secondaryLabelColor)
        let modelExplanation = "目前只有一個固定模型（tea-asr model-prepare 沒有指定模型的參數），"
            + "所以這裡還做不到在原版與 MLX 版之間切換——那需要 server 端先提供模型目錄／切換的 API。"
            + "這個按鈕做得到的，是觸發既有的 tea-asr model-prepare，重新準備／驗證目前這個固定模型。"
        let modelInfo = InfoButton(explanation: modelExplanation)
        let modelValueRow = NSStackView(views: [modelValue, modelInfo])
        modelValueRow.orientation = .horizontal
        modelValueRow.alignment = .centerY
        modelValueRow.spacing = Metrics.hair + 2
        let modelPrepareButton = actionButton(title: "重新準備模型", action: #selector(runModelPrepare(_:)))
        modelPrepareButton.identifier = NSUserInterfaceItemIdentifier("modelPrepareButton")
        let modelPrepareStatus = stableLabel(font: .systemFont(ofSize: 12), maxLines: nil, color: .secondaryLabelColor)
        let modelGrid = settingsGrid([
            [settingsLabel("目前模型"), modelValueRow],
            [NSGridCell.emptyContentView, buttonRow([modelPrepareButton])],
            [NSGridCell.emptyContentView, modelPrepareStatus],
        ])

        let audioGrid = settingsGrid([
            [settingsLabel("輸入裝置"), inputDevice],
            [settingsLabel("聲道"), inputChannel],
            [settingsLabel("輸入電平"), audioLevelBar],
        ])

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
        let stripTrailingPunctuation = NSButton(
            checkboxWithTitle: "移除句尾句點（。／.）",
            target: self,
            action: #selector(saveSettings(_:))
        )
        stripTrailingPunctuation.identifier = NSUserInterfaceItemIdentifier("stripTrailingPunctuation")
        stripTrailingPunctuation.state = settings.stripTrailingPunctuation ? .on : .off
        let spokenSymbols = NSButton(
            checkboxWithTitle: "念出符號名稱時打出符號（例如「逗號」→「，」）",
            target: self,
            action: #selector(saveSettings(_:))
        )
        spokenSymbols.identifier = NSUserInterfaceItemIdentifier("spokenSymbols")
        spokenSymbols.state = settings.spokenSymbols ? .on : .off

        // Dynamic status only now — see `shortcutInfo` above for the static
        // "按一下快捷鍵按鈕即可修改" usage tip that used to be appended here.
        // This label must stay a plain, always-visible line: it can turn red
        // to report a shortcut conflict/registration failure, which is a
        // warning the user needs at a glance, not explanatory copy.
        let shortcutHint = NSTextField(wrappingLabelWithString: "")
        shortcutHint.font = .systemFont(ofSize: 12)
        let save = actionButton(title: "儲存設定", action: #selector(saveSettings(_:)))
        let test = actionButton(title: "測試服務連線", action: #selector(testService(_:)))
        let buttons = buttonRow([save, test])
        // The checkboxes and the buttons sit in the same grid geometry as the
        // labelled fields, so every control on the page — field, popup,
        // checkbox, button — shares one left edge, the way System Settings
        // lays a pane out.
        let behaviourGrid = settingsGrid([
            [NSGridCell.emptyContentView, feedback],
            [NSGridCell.emptyContentView, autoInsert],
            [NSGridCell.emptyContentView, preview],
            [NSGridCell.emptyContentView, stripTrailingPunctuation],
            [NSGridCell.emptyContentView, spokenSymbols],
        ])
        let buttonGrid = settingsGrid([[NSGridCell.emptyContentView, buttons]])

        let view = sectionStack(views: [
            group(title: "服務連線", views: [grid]),
            group(title: "本機服務", views: [executableGrid, serviceControlGrid, serviceLoginItemGrid, modelGrid]),
            group(title: "音訊輸入", views: [audioGrid]),
            group(title: "輸入行為", views: [shortcutHint, behaviourGrid]),
            buttonGrid,
        ])

        func update() {
            hotKey.shortcut = settings.shortcut
            if shortcutHint.stringValue != shortcutStatus { shortcutHint.stringValue = shortcutStatus }
            shortcutHint.textColor = shortcutStatus.contains("無效")
                || shortcutStatus.contains("無法")
                || shortcutStatus.contains("占用")
                ? .systemRed
                : .secondaryLabelColor
            let summary = connectionSummaryText()
            if connectionSummary.stringValue != summary { connectionSummary.stringValue = summary }
            let executableText = executableStatusText()
            if executableStatus.stringValue != executableText { executableStatus.stringValue = executableText }

            let presentation = ServiceRuntimeControl.presentation(for: serviceRuntimeState())
            if serviceControlButton.title != presentation.buttonTitle {
                serviceControlButton.title = presentation.buttonTitle
            }
            serviceControlButton.isEnabled = presentation.buttonEnabled
            if serviceControlStatus.stringValue != presentation.statusText {
                serviceControlStatus.stringValue = presentation.statusText
            }

            let loginItemExecutableFound = ServiceControl.search(configured: settings.serviceExecutable).executable != nil
            serviceLoginItem.isEnabled = loginItemExecutableFound
            serviceLoginItem.state = (serviceLoginItemInstalled ?? false) ? .on : .off
            let loginItemText = serviceLoginItemStatusText(
                executableFound: loginItemExecutableFound,
                installed: serviceLoginItemInstalled
            )
            if serviceLoginItemStatus.stringValue != loginItemText { serviceLoginItemStatus.stringValue = loginItemText }
            // Never a synchronous `agentInstalled` call from here: `update()`
            // runs after every unrelated action on this page (restart,
            // model-prepare, save…), not only when the page is first shown,
            // so probing off the main thread is what keeps those actions
            // from paying for a `tea-asr service status` spawn every time.
            refreshServiceLoginItemState()

            let modelText = modelInfoText()
            if modelValue.stringValue != modelText { modelValue.stringValue = modelText }
            let prepareText = modelPrepareStatusText()
            if modelPrepareStatus.stringValue != prepareText { modelPrepareStatus.stringValue = prepareText }
            modelPrepareButton.isEnabled = modelPrepareProcess?.isRunning != true
        }
        update()
        return SectionRuntime(view: view, update: update)
    }

    /// The pure inputs to `ServiceRuntimeControl.state`, gathered from the
    /// three places that actually know them: the executable search, this
    /// controller's own `managedService` handle, and the last health probe
    /// result already tracked on `appState`.
    private func serviceRuntimeState() -> ServiceRuntimeControl.State {
        ServiceRuntimeControl.state(
            managedRunning: managedService?.isRunning == true,
            reachableElsewhere: managedService?.isRunning != true && appState.serviceReachable == true,
            executableFound: ServiceControl.search(configured: settings.serviceExecutable).executable != nil,
            exitStatus: managedService?.exitStatus
        )
    }

    private func modelInfoText() -> String {
        guard let status = appState.serviceSnapshot?.status else {
            return "尚未取得（服務未連線）"
        }
        return "\(status.model) · revision \(status.modelRevision)"
    }

    private func modelPrepareStatusText() -> String {
        guard let managed = modelPrepareProcess else { return "尚未執行過 model-prepare。" }
        let snapshot = managed.output.snapshot()
        let tail = snapshot.lines.suffix(5).joined(separator: "\n")
        if managed.isRunning {
            return tail.isEmpty ? "執行中…" : "執行中…\n\(tail)"
        }
        let status = managed.exitStatus ?? managed.process.terminationStatus
        let outcome = status == 0 ? "已完成。" : "失敗（結束碼 \(status)）。"
        return tail.isEmpty ? outcome : "\(outcome)\n\(tail)"
    }

    /// Explains, in plain always-visible text, where `tea-asr` was found (or
    /// every location that was tried and came up empty). This is the "why"
    /// this app used to have nowhere to show: a service that never started
    /// because the executable could not be located rendered identically to
    /// any other "無法連線" — offline for an unrelated reason, wrong
    /// host/port, service still starting up.
    private func executableStatusText() -> String {
        let search = ServiceControl.search(configured: settings.serviceExecutable)
        if let url = search.executable {
            return "已找到：\(url.path)"
        }
        guard !search.searchedPaths.isEmpty else {
            return "找不到 tea-asr 執行檔，也沒有可自動搜尋的位置。請手動指定路徑。"
        }
        let locations = search.searchedPaths.map { "• \($0)" }.joined(separator: "\n")
        return "找不到 tea-asr 執行檔。已找過以下位置：\n\(locations)\n\n"
            + "請在上方指定路徑（通常是專案的 .venv/bin/tea-asr），或先安裝到 PATH 上。"
    }

    /// The Settings page's own start/stop control, wired to
    /// `restartManagedService()` below.
    @objc private func toggleManagedService(_ sender: Any?) {
        restartManagedService()
    }

    /// Whether `restartManagedService()` would actually do something right
    /// now. Mirrors the Settings page button's own enabled state so the menu
    /// bar's "重新啟動服務" item can stay in lockstep with it without
    /// duplicating the reachable-elsewhere / executable-missing reasoning.
    var canRestartManagedService: Bool {
        ServiceRuntimeControl.presentation(for: serviceRuntimeState()).buttonEnabled
    }

    /// Restarts the local service this window controls: if a process it
    /// holds a handle to is currently running, that exact `Process` is
    /// stopped first (bounded wait for the port to actually free up) and a
    /// fresh one is launched; if nothing this window holds is running, a
    /// fresh one is simply started. Never touches a process this window has
    /// no handle for — a service reachable elsewhere is left alone, exactly
    /// as the disabled button on the Settings page already implies (see
    /// `ManagedProcess`, `ServiceRuntimeControl`).
    func restartManagedService() {
        switch serviceRuntimeState() {
        case .reachableElsewhere:
            presentAlert(
                "無法重新啟動服務",
                ServiceRuntimeControl.presentation(for: .reachableElsewhere).statusText
            )
            return
        case .managedRunning, .stopped:
            break
        }
        if let managed = managedService, managed.isRunning {
            managed.terminate()
            // Bounded wait so a restart does not race the old process for
            // the port; never an unbounded block on a child that refuses to
            // exit (same reasoning as `AppController.stopManagedService`).
            let deadline = Date().addingTimeInterval(2.0)
            while managed.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.02)
            }
        }
        let search = ServiceControl.search(configured: settings.serviceExecutable)
        guard let binary = search.executable else {
            presentAlert(
                "找不到服務執行檔",
                ServiceControl.ControlError.executableNotFound(searched: search.searchedPaths).localizedDescription
            )
            updateSection(.settings)
            updateSection(.logs)
            return
        }
        do {
            let managed = try ServiceControl.launchService(executable: binary)
            managed.onExit = { [weak self] _ in
                DispatchQueue.main.async {
                    self?.updateSection(.settings)
                    self?.updateSection(.logs)
                }
            }
            managedService = managed
            onRefreshService?()
        } catch {
            presentAlert("無法啟動服務", error.localizedDescription)
        }
        updateSection(.settings)
        updateSection(.logs)
    }

    /// The service process this window launched, if any, so the app's quit
    /// path can stop it unconditionally without ever discovering a process
    /// by name. `nil` means this app launched nothing it can stop.
    var managedServiceProcess: ManagedProcess? { managedService }

    /// Re-probes the service's LaunchAgent state off the main thread, and
    /// re-renders only when the answer actually changed — the exact same
    /// shape as `AppController`'s old auto-start probe, kept for the same
    /// reason: `update()` above runs after every unrelated action on this
    /// page, so a synchronous `tea-asr service status` spawn there would
    /// make every one of those actions pay for a subprocess round trip.
    private func refreshServiceLoginItemState() {
        guard !serviceLoginItemProbeInFlight,
              let binary = ServiceControl.search(configured: settings.serviceExecutable).executable
        else { return }
        serviceLoginItemProbeInFlight = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let installed = ServiceControl.agentInstalled(executable: binary)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.serviceLoginItemProbeInFlight = false
                guard self.serviceLoginItemInstalled != installed else { return }
                self.serviceLoginItemInstalled = installed
                self.updateSection(.settings)
            }
        }
    }

    private func serviceLoginItemStatusText(executableFound: Bool, installed: Bool?) -> String {
        guard executableFound else {
            return "找不到執行檔，請先在上方指定路徑。"
        }
        guard let installed else {
            return "確認中…"
        }
        return installed
            ? "已設定：登入時透過 LaunchAgent 啟動服務。"
            : "尚未設定。"
    }

    /// Installs/uninstalls the service's LaunchAgent immediately, the same
    /// way `restartManagedService()` acts immediately rather than waiting
    /// for "儲存設定" — this checkbox is a direct reflection of `launchctl`
    /// state, not a persisted preference. Reading the current state and
    /// acting on it both happen synchronously here, deliberately: this is a
    /// single explicit user action, not the passive per-`update()` probe
    /// above, so paying for one subprocess round trip at click time is the
    /// same trade the old menu-bar `toggleAutoStart` made. The result is
    /// also applied optimistically to `serviceLoginItemInstalled` so the
    /// checkbox does not have to wait for the next background probe to
    /// catch up. On failure the checkbox is reset to the state that is
    /// still actually true on disk.
    @objc private func toggleServiceLoginItem(_ sender: NSButton) {
        guard let binary = ServiceControl.search(configured: settings.serviceExecutable).executable else {
            sender.state = .off
            presentAlert(
                "找不到服務執行檔",
                ServiceControl.ControlError.executableNotFound(
                    searched: ServiceControl.search(configured: settings.serviceExecutable).searchedPaths
                ).localizedDescription
            )
            return
        }
        let installed = ServiceControl.agentInstalled(executable: binary)
        do {
            try ServiceControl.run(executable: binary, arguments: ["service", installed ? "uninstall" : "install"])
        } catch {
            sender.state = installed ? .on : .off
            presentAlert("設定失敗", error.localizedDescription)
            return
        }
        serviceLoginItemInstalled = !installed
        updateSection(.settings)
    }

    /// Triggers the one model action that already exists server-side:
    /// `tea-asr model-prepare`. This is not a model *switch* — the CLI takes
    /// no model argument today — only a re-run of preparing/verifying the
    /// single fixed model, which is exactly what `modelExplanation` above
    /// tells the user.
    @objc private func runModelPrepare(_ sender: Any?) {
        guard modelPrepareProcess?.isRunning != true else { return }
        let search = ServiceControl.search(configured: settings.serviceExecutable)
        guard let binary = search.executable else {
            presentAlert(
                "找不到服務執行檔",
                ServiceControl.ControlError.executableNotFound(searched: search.searchedPaths).localizedDescription
            )
            return
        }
        do {
            let managed = try ServiceControl.launchModelPrepare(executable: binary)
            managed.onExit = { [weak self] _ in
                DispatchQueue.main.async { self?.updateSection(.settings) }
            }
            modelPrepareProcess = managed
        } catch {
            presentAlert("無法執行 model-prepare", error.localizedDescription)
        }
        updateSection(.settings)
    }

    @objc private func chooseServiceExecutable(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.title = "選擇 tea-asr 執行檔"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.begin { [weak self] response in
            guard response == .OK, let url = panel.url, let self else { return }
            guard let field = self.controlsInSettingsView().serviceExecutable else { return }
            field.stringValue = url.path
            self.saveSettings(nil)
        }
    }

    /// One shared grid geometry for every settings row: a right-aligned label
    /// column of a fixed width, then the control column. Because the label
    /// column's width is pinned (rather than sized to whichever labels happen
    /// to be in that particular grid), controls in different groups still
    /// start on the same vertical line.
    private func settingsGrid(_ rows: [[NSView]]) -> NSGridView {
        let grid = NSGridView(views: rows)
        grid.column(at: 0).xPlacement = .trailing
        grid.column(at: 0).width = Metrics.labelColumn
        grid.column(at: 1).xPlacement = .leading
        grid.columnSpacing = Metrics.row
        grid.rowSpacing = Metrics.tight
        grid.translatesAutoresizingMaskIntoConstraints = false
        return grid
    }

    private func settingsLabel(_ title: String) -> NSTextField {
        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.lineBreakMode = .byTruncatingTail
        return label
    }

    private func inputDevicePopup() -> NSPopUpButton {
        let popup = NSPopUpButton()
        var skippedDevices: [AudioInputDeviceCatalog.SkippedDevice] = []
        let options = AudioInputSettingsOptions.deviceOptions(
            storedUID: settings.inputDeviceUID,
            enumeration: AudioInputDeviceCatalog.enumerationResult(skipped: &skippedDevices),
            skipped: skippedDevices
        )
        for option in options {
            popup.addItem(withTitle: option.title)
            let item = popup.item(at: popup.numberOfItems - 1)
            if case .enumerationError = option {
                // Do not use the empty UID here: it is the real represented
                // value of System Default, and would select the disabled
                // diagnostic row instead of the usable default row.
                item?.representedObject = Self.inputDeviceEnumerationErrorUID
                item?.toolTip = option.title
            } else if case .skippedDevices = option {
                // Same reasoning as the enumeration-error row above: this
                // diagnostic sits alongside a real System Default row and
                // must not steal its UID.
                item?.representedObject = Self.inputDeviceSkippedDevicesUID
                item?.toolTip = option.title
            } else {
                item?.representedObject = option.uid ?? AudioInputDevice.systemDefaultUID
            }
            item?.isEnabled = option.isEnabled
        }
        let selectedUID = settings.inputDeviceUID ?? AudioInputDevice.systemDefaultUID
        if let item = popup.itemArray.last(where: { ($0.representedObject as? String) == selectedUID }) {
            popup.select(item)
        } else {
            popup.selectItem(at: 0)
        }
        return popup
    }

    private func editShortcut() {
        onShortcutEditorWillBegin?()
        let editor = ShortcutRecorderPanelController(shortcut: settings.shortcut)
        let result = editor.runModal(relativeTo: window)
        defer {
            onShortcutEditorDidEnd?()
            updateSection(.settings)
        }

        if case .saved(let shortcut) = result {
            // The panel only returns .saved after GlobalShortcut validation and
            // the exclusive Carbon registration probe have both succeeded.
            // Cancel, close, conflict, and other failures never reach this
            // write.
            settings.shortcut = shortcut
        }
    }

    private func inputChannelPopup(deviceUID: String?) -> NSPopUpButton {
        let popup = NSPopUpButton()
        populateInputChannelPopup(popup, deviceUID: deviceUID)
        return popup
    }

    private func populateInputChannelPopup(_ popup: NSPopUpButton, deviceUID: String?) {
        popup.removeAllItems()
        let device: AudioInputDevice?
        switch AudioInputDeviceCatalog.enumerationResult() {
        case .success(let available):
            if let deviceUID, !deviceUID.isEmpty {
                device = available.first(where: { $0.uid == deviceUID })
            } else if available.isEmpty {
                // The device popup already contains the visible catalog
                // diagnostic. Avoid a second default-device CoreAudio query
                // when there is no catalog to resolve it against.
                device = nil
            } else {
                device = AudioInputDeviceCatalog.defaultRecord()?.descriptor
            }
        case .failure:
            device = nil
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
        // Permissions go first: they are the only thing on this page the user
        // can act on, and the read-only rows below are usually consulted to
        // explain a failure the permissions may well be the cause of.
        let permissionsGroup = buildPermissionsGroup()
        let stateRow = buildValueRow(title: "App state")
        // Client state's `.failed` case, the service probe summary, and the
        // last-error row can all carry a full error message (see
        // clientStateSummary()'s `.failed` branch and serverSummary()'s error
        // passthrough). Truncating those to two lines hides the only clue to
        // what broke, which is worse than the row growing — so these three
        // opt out of the fixed-height row. "App state" stays on a single
        // fixed line (its vocabulary is a handful of short words); "Audio"
        // keeps two, because it embeds a user-named input device.
        let clientRow = buildValueRow(title: "Client state", maxLines: nil)
        let audioRow = buildValueRow(title: "Audio", maxLines: 2)
        let serviceRow = buildValueRow(title: "Service probe", maxLines: nil)
        let errorRow = buildValueRow(title: "最近錯誤", maxLines: nil)
        let refresh = actionButton(title: "重新檢查服務", action: #selector(testService(_:)))
        let view = sectionStack(views: [
            permissionsGroup.view,
            group(title: "應用程式", views: [stateRow.view, clientRow.view]),
            group(title: "連線與音訊", views: [audioRow.view, serviceRow.view, errorRow.view]),
            refresh,
        ])

        func update() {
            permissionsGroup.update()
            stateRow.update(appState.displayStatus.title)
            clientRow.update(clientStateSummary())
            audioRow.update(audioSummary())
            serviceRow.update(serverSummary())
            errorRow.update(appState.serviceError?.localizedDescription ?? "無")
        }
        update()
        return SectionRuntime(view: view, update: update)
    }

    /// The log page: a level filter, a refresh button, one status/notice line
    /// (loading / error / empty / "there is more but no paging"), and the
    /// event list itself. Built once like every other section — `update()`
    /// only reassigns the popup selection, the status label's text/colour,
    /// and the text view's attributed string; it never rebuilds the row
    /// stack, so switching pages and back preserves scroll position.
    private func buildLogsSection() -> SectionRuntime {
        let levelPopup = NSPopUpButton()
        levelPopup.identifier = NSUserInterfaceItemIdentifier("logsLevel")
        for level in LogLevel.allCases {
            levelPopup.addItem(withTitle: level.filterTitle)
            levelPopup.item(at: levelPopup.numberOfItems - 1)?.representedObject = level.rawValue
        }
        levelPopup.target = self
        levelPopup.action = #selector(logsLevelChanged(_:))
        levelPopup.widthAnchor.constraint(greaterThanOrEqualToConstant: Metrics.fieldWidth).isActive = true
        levelPopup.setContentHuggingPriority(.required, for: .horizontal)

        let refresh = actionButton(title: "重新整理", action: #selector(refreshLogs(_:)))
        let controlsRow = buttonRow([levelPopup, refresh])

        // Can carry a full connection/auth error message (see
        // logsStatusPresentation()), so it opts out of the fixed-height,
        // truncating row the same way "最近錯誤" does on Diagnostics.
        let statusLabel = stableLabel(font: .systemFont(ofSize: 12), maxLines: nil, color: .secondaryLabelColor)

        let textView = NSTextView()
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = true
        textView.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.drawsBackground = false
        textView.textContainerInset = NSSize(width: Metrics.tight, height: Metrics.tight)
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = true
        scroll.backgroundColor = .textBackgroundColor
        scroll.borderType = .bezelBorder
        scroll.documentView = textView
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 320).isActive = true
        logsTextView = textView

        // Second tab: the managed service process's own stdout/stderr,
        // separate from the structured `/v1/logs` feed above — see
        // `ServiceOutputPresentation`'s doc comment for why the two must
        // never be merged into one view.
        let tabControl = NSSegmentedControl(
            labels: ["結構化日誌", "服務輸出"],
            trackingMode: .selectOne,
            target: self,
            action: #selector(logsTabChanged(_:))
        )
        tabControl.identifier = NSUserInterfaceItemIdentifier("logsTab")
        tabControl.selectedSegment = logsTab.rawValue
        tabControl.setContentHuggingPriority(.required, for: .horizontal)

        let structuredView = group(views: [controlsRow, statusLabel, scroll])

        let serviceOutputStatus = stableLabel(font: .systemFont(ofSize: 12), maxLines: nil, color: .secondaryLabelColor)
        let serviceOutputTextView = NSTextView()
        serviceOutputTextView.isEditable = false
        serviceOutputTextView.isSelectable = true
        serviceOutputTextView.isRichText = false
        serviceOutputTextView.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        serviceOutputTextView.drawsBackground = false
        serviceOutputTextView.textContainerInset = NSSize(width: Metrics.tight, height: Metrics.tight)
        serviceOutputTextView.minSize = NSSize(width: 0, height: 0)
        serviceOutputTextView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        serviceOutputTextView.isVerticallyResizable = true
        serviceOutputTextView.isHorizontallyResizable = false
        serviceOutputTextView.autoresizingMask = [.width]
        serviceOutputTextView.textContainer?.widthTracksTextView = true
        let serviceOutputScroll = NSScrollView()
        serviceOutputScroll.hasVerticalScroller = true
        serviceOutputScroll.drawsBackground = true
        serviceOutputScroll.backgroundColor = .textBackgroundColor
        serviceOutputScroll.borderType = .bezelBorder
        serviceOutputScroll.documentView = serviceOutputTextView
        serviceOutputScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 320).isActive = true
        logsServiceOutputTextView = serviceOutputTextView
        let serviceOutputView = group(views: [serviceOutputStatus, serviceOutputScroll])

        let view = sectionStack(views: [
            group(title: "日誌", views: [tabControl, structuredView, serviceOutputView]),
        ])

        func update() {
            tabControl.selectedSegment = logsTab.rawValue
            structuredView.isHidden = logsTab != .structured
            serviceOutputView.isHidden = logsTab != .serviceOutput

            if let item = levelPopup.itemArray.first(where: {
                ($0.representedObject as? String) == logsSelectedLevel.rawValue
            }) {
                levelPopup.select(item)
            }
            let presentation = logsStatusPresentation()
            if statusLabel.stringValue != presentation.text { statusLabel.stringValue = presentation.text }
            statusLabel.textColor = presentation.color

            let rendered = logsRenderedText()
            if textView.attributedString().string != rendered.string {
                let wasAtBottom = isScrolledToBottom(scroll)
                textView.textStorage?.setAttributedString(rendered)
                if wasAtBottom {
                    scrollToBottom(scroll)
                }
            }

            // This controller's own `managedService` (set only by the
            // Settings page's start button) is checked first; if the
            // service was instead started from the menu bar's fire-and-
            // forget `ServiceControl.start(executable:)`, that path stashes
            // its handle in `ServiceControl.lastManagedServeProcess` for
            // exactly this fallback — see that property's doc comment for
            // why this is still safe to only ever read, never terminate.
            let managed = managedService ?? ServiceControl.lastManagedServeProcess
            let statusText = ServiceOutputPresentation.statusText(
                isManaged: managed != nil,
                isRunning: managed?.isRunning == true,
                reachableElsewhere: appState.serviceReachable == true
            )
            if serviceOutputStatus.stringValue != statusText { serviceOutputStatus.stringValue = statusText }
            let snapshot = managed?.output.snapshot()
            let bodyText = ServiceOutputPresentation.body(
                lines: snapshot?.lines ?? [],
                droppedLines: snapshot?.droppedLines ?? 0
            )
            if serviceOutputTextView.string != bodyText {
                let wasAtBottom = isScrolledToBottom(serviceOutputScroll)
                serviceOutputTextView.string = bodyText
                if wasAtBottom {
                    scrollToBottom(serviceOutputScroll)
                }
            }
        }
        update()
        return SectionRuntime(view: view, update: update)
    }

    @objc private func logsTabChanged(_ sender: NSSegmentedControl) {
        logsTab = LogsTab(rawValue: sender.selectedSegment) ?? .structured
        updateSection(.logs)
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
        show(section: .diagnostics)
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
        startAudioLevelMonitor()
    }

    @objc private func audioChannelSelectionChanged(_ sender: NSPopUpButton) {
        startAudioLevelMonitor()
    }

    // MARK: - 日誌

    @objc private func refreshLogs(_ sender: Any?) {
        fetchLogs()
    }

    @objc private func logsLevelChanged(_ sender: NSPopUpButton) {
        guard
            let rawValue = sender.selectedItem?.representedObject as? String,
            let level = LogLevel(rawValue: rawValue)
        else { return }
        logsSelectedLevel = level
        fetchLogs()
    }

    /// The only place `/v1/logs` is ever requested: called when the user
    /// switches into the Logs page (`select(section:)`) or presses its own
    /// "重新整理" button/changes the level filter. There is intentionally no
    /// timer here — a log page that refreshes itself in the background would
    /// also be a page that quietly scrolls out from under someone reading it.
    private func fetchLogs() {
        logsFetchState = .loading
        updateSection(.logs)
        let token = try? settings.token()
        logsClient.fetch(
            host: settings.host,
            port: settings.port,
            token: token,
            level: logsSelectedLevel,
            limit: logsLimit
        ) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let response):
                self.logsEntries = response.items
                self.logsHasMore = response.hasMore
                self.logsFetchState = .loaded
            case .failure(let error):
                self.logsEntries = []
                self.logsHasMore = false
                self.logsFetchState = .failed(error)
            }
            self.updateSection(.logs)
        }
    }

    /// The status line above the log list. A failed fetch and a genuinely
    /// empty result must never render the same text (see
    /// `LogsFetchState`/`LogsPresentation`): a service that is offline, or a
    /// token the server rejected, says so explicitly instead of looking like
    /// "there is nothing to report".
    private func logsStatusPresentation() -> (text: String, color: NSColor) {
        switch logsFetchState {
        case .idle:
            return (LogsPresentation.idleNotice(), .secondaryLabelColor)
        case .loading:
            return (LogsPresentation.loadingNotice(), .secondaryLabelColor)
        case .failed(let error):
            return (error.localizedDescription, .systemRed)
        case .loaded:
            var message = logsEntries.isEmpty
                ? LogsPresentation.emptyNotice()
                : LogsPresentation.loadedSummary(count: logsEntries.count)
            if logsHasMore {
                message += " " + LogsPresentation.hasMoreNotice(shown: logsEntries.count)
            }
            return (message, .secondaryLabelColor)
        }
    }

    /// Renders the fetched entries as one line per row. Colour is used only
    /// for its semantic meaning — error in red, warning in orange — every
    /// other level (and every other part of the line) stays in the system's
    /// neutral label colour rather than a per-level rainbow.
    private func logsRenderedText() -> NSAttributedString {
        let font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        let result = NSMutableAttributedString()
        guard !logsEntries.isEmpty else {
            let placeholder = logsFetchState == .loading ? "" : logsStatusPresentation().text
            result.append(NSAttributedString(
                string: placeholder,
                attributes: [.foregroundColor: NSColor.secondaryLabelColor, .font: font]
            ))
            return result
        }
        for (index, entry) in logsEntries.enumerated() {
            let color: NSColor
            switch entry.level.uppercased() {
            case "ERROR": color = .systemRed
            case "WARNING", "WARN": color = .systemOrange
            default: color = .labelColor
            }
            result.append(NSAttributedString(
                string: LogsPresentation.line(for: entry),
                attributes: [.foregroundColor: color, .font: font]
            ))
            if index < logsEntries.count - 1 {
                result.append(NSAttributedString(string: "\n"))
            }
        }
        return result
    }

    // MARK: - 音訊電平監看

    private func startAudioLevelMonitor() {
        stopAudioLevelMonitor()

        guard selectedSection == .settings, isWindowOpen else { return }

        #if DEBUG
        monitorStartCount += 1
        #endif

        let controls = controlsInSettingsView()
        let rawSelectedUID = controls.inputDevice?.selectedItem?.representedObject as? String
        let selectedUID = rawSelectedUID ?? settings.inputDeviceUID ?? AudioInputDevice.systemDefaultUID
        let selectedName = controls.inputDevice?.selectedItem?.title
        let configuration = AudioLevelMonitor.Configuration(
            deviceUID: selectedUID,
            deviceName: selectedName,
            channelPolicy: settings.inputChannelPolicy
        )
        audioLevelMonitor.startAsync(configuration: configuration) { [weak self] result in
            guard let self else { return }
            guard self.selectedSection == .settings, self.isWindowOpen else { return }
            if case .failure(let error) = result,
               let monitorError = error as? AudioLevelMonitorError,
               case .startCancelled = monitorError { return }
            if case .failure(let error) = result,
               self.audioLevelMonitor.state == .idle {
                self.audioLevelBar.setState(.failed(error.localizedDescription))
            }
        }
    }

    private func stopAudioLevelMonitor() {
        audioLevelMonitor.stop()
        audioLevelBar.setState(.idle)
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
        if let serviceExecutable = controls.serviceExecutable {
            settings.serviceExecutable = serviceExecutable.stringValue
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if let stripTrailingPunctuation = controls.stripTrailingPunctuation {
            settings.stripTrailingPunctuation = stripTrailingPunctuation.state == .on
        }
        if let spokenSymbols = controls.spokenSymbols {
            settings.spokenSymbols = spokenSymbols.state == .on
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
        inputChannel: NSPopUpButton?, shortcut: ShortcutButton?,
        interactionMode: NSPopUpButton?, feedback: NSButton?,
        serviceExecutable: NSTextField?, stripTrailingPunctuation: NSButton?,
        spokenSymbols: NSButton?
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
            fields["shortcut"] as? ShortcutButton,
            fields["interactionMode"] as? NSPopUpButton,
            fields["feedback"] as? NSButton,
            // `serviceExecutable` is an `NSTextField`, same as `host`/`port`,
            // but `fields["host"] as? NSTextField` above would just as
            // happily match it if it shared a key — the dictionary lookup
            // here is keyed on the identifier string, not the type, so a
            // distinct identifier is what actually keeps them apart.
            fields["serviceExecutable"] as? NSTextField,
            fields["stripTrailingPunctuation"] as? NSButton,
            fields["spokenSymbols"] as? NSButton
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

    /// The root stack of a section: just its groups, stacked on one rhythm.
    ///
    /// There is deliberately no per-page hero title/subtitle any more. The
    /// sidebar already says which page the user is on, and no Apple app
    /// (System Settings, Mail, Shortcuts) repeats that as a 26pt landing-page
    /// headline inside the content pane.
    private func sectionStack(views: [NSView]) -> NSView {
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = Metrics.group
        stack.translatesAutoresizingMaskIntoConstraints = false
        for view in views {
            // A view that opted out of growing (a lone button, a button row)
            // keeps its natural width; see stretchArrangedSubviewsToFullWidth.
            guard view.contentHuggingPriority(for: .horizontal) != .required else { continue }
            view.setContentHuggingPriority(.defaultLow, for: .horizontal)
            view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        }
        stretchArrangedSubviewsToFullWidth(stack)
        return stack
    }

    /// A group of related rows, in the plain AppKit vocabulary: an optional
    /// quiet title over a hairline separator, then the rows. This replaces the
    /// rounded, tinted, bordered "card" (and its decorative per-card icon),
    /// neither of which appears in Apple's own apps. A lone control is not
    /// wrapped in a group at all — it is simply placed in the section.
    private func group(title: String? = nil, views: [NSView]) -> NSView {
        let content = NSStackView(views: views)
        content.orientation = .vertical
        content.alignment = .width
        content.spacing = Metrics.tight
        content.translatesAutoresizingMaskIntoConstraints = false
        stretchArrangedSubviewsToFullWidth(content)
        guard let title else { return content }

        let heading = NSTextField(labelWithString: title)
        heading.font = .systemFont(ofSize: 13, weight: .semibold)
        heading.textColor = .labelColor
        let separator = NSBox()
        separator.boxType = .separator

        let stack = NSStackView(views: [heading, separator, content])
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = Metrics.tight
        stack.setCustomSpacing(Metrics.row, after: separator)
        stack.translatesAutoresizingMaskIntoConstraints = false
        stretchArrangedSubviewsToFullWidth(stack)
        return stack
    }

    /// A horizontal run of buttons that stays at its natural size and hugs the
    /// leading edge, so buttons in different groups start on the same line.
    private func buttonRow(_ buttons: [NSView]) -> NSStackView {
        let row = NSStackView(views: buttons)
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = Metrics.row
        row.setContentHuggingPriority(.required, for: .horizontal)
        return row
    }

    /// `NSStackView`'s built-in `.width` cross-axis alignment (used above,
    /// and by `cardStack`'s content stack and the permissions list) pins
    /// each arranged subview's leading/trailing edges to the stack's own
    /// edges, but at a priority below `.required`. Every arranged subview
    /// here — headings, cards, buttons — has its own narrower intrinsic
    /// width, so those two edge constraints tie against that intrinsic
    /// width and against each other; AppKit's solver resolved that tie by
    /// keeping only the trailing edge pinned and leaving the leading edge
    /// free, which is the "整片靠右" bug: every section's content packs
    /// against the right side of the detail pane with a blank gap on the
    /// left, and — because each subview's intrinsic width differs — the
    /// left edges of neighbouring cards land at different x positions
    /// instead of lining up. Confirmed by dumping the live view hierarchy
    /// from a headless test (`swift test` can build real AppKit views
    /// without a window server): every arranged subview's frame had
    /// `x + width == stack.frame.width` while `x` varied per view. Pinning
    /// both edges ourselves at `.required` priority removes the tie, so
    /// this — not `.alignment = .width` — is what actually stretches an
    /// arranged subview to the stack's width.
    private func stretchArrangedSubviewsToFullWidth(_ stack: NSStackView) {
        for view in stack.arrangedSubviews {
            view.leadingAnchor.constraint(equalTo: stack.leadingAnchor).isActive = true
            // A view that explicitly opted out of growing horizontally (e.g.
            // `actionButton`'s `.required` hugging, used for a lone button
            // like "重新檢查權限") should stay at its natural compact width
            // instead of being stretched edge-to-edge — pinning only its
            // leading edge still fixes the "everything starts flush right"
            // bug for it without turning it into a giant full-width button.
            guard view.contentHuggingPriority(for: .horizontal) != .required else { continue }
            view.trailingAnchor.constraint(equalTo: stack.trailingAnchor).isActive = true
        }
    }

    /// Builds the status header once. It used to be a tinted "hero card" —
    /// a 54pt icon in a coloured rounded tile, an 18pt title, and a bordered
    /// pill badge — which is web-dashboard vocabulary. It is now a plain
    /// status line: one small SF Symbol, the state, the mode on the trailing
    /// side, and one line of explanation. Only the symbol carries colour, and
    /// only when the colour means something (see `severityColor`).
    private func buildStatusHero() -> (view: NSView, update: () -> Void) {
        let icon = NSImageView(image: NSImage())
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.setContentHuggingPriority(.required, for: .horizontal)

        let title = NSTextField(labelWithString: "")
        title.font = .systemFont(ofSize: 15, weight: .semibold)
        title.lineBreakMode = .byTruncatingTail

        let modeLabel = NSTextField(labelWithString: "")
        modeLabel.font = .systemFont(ofSize: 13)
        modeLabel.textColor = .secondaryLabelColor
        modeLabel.alignment = .right
        modeLabel.lineBreakMode = .byTruncatingTail
        modeLabel.setContentHuggingPriority(.required, for: .horizontal)
        modeLabel.setContentCompressionResistancePriority(.required, for: .horizontal)

        let titleRow = NSStackView(views: [icon, title, modeLabel])
        titleRow.orientation = .horizontal
        titleRow.alignment = .centerY
        titleRow.spacing = Metrics.tight
        // See the note in buildPermissionRow: `.fill` is what actually puts
        // the mode on the trailing edge instead of right after the title.
        titleRow.distribution = .fill

        let detail = stableLabel(font: .systemFont(ofSize: 13), maxLines: 2, color: .secondaryLabelColor)

        let stack = NSStackView(views: [titleRow, detail])
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = Metrics.hair
        stretchArrangedSubviewsToFullWidth(stack)

        func update() {
            let presentation = statusPresentation()
            icon.image = NSImage(systemSymbolName: presentation.symbolName, accessibilityDescription: presentation.title) ?? NSImage()
            icon.contentTintColor = severityColor(presentation.tint)
            if title.stringValue != presentation.title { title.stringValue = presentation.title }
            if detail.stringValue != presentation.detail { detail.stringValue = presentation.detail }
            let mode = modeTitle()
            if modeLabel.stringValue != mode { modeLabel.stringValue = mode }
        }
        update()
        return (stack, update)
    }

    /// Keeps colour for the two cases where it carries meaning — a problem
    /// (red) and something that needs attention (orange) — and lets every
    /// other state fall back to the system's neutral label colours, so the
    /// window stops using blue/purple/green as decoration.
    private func severityColor(_ tint: NSColor) -> NSColor {
        if tint == .systemRed { return .systemRed }
        if tint == .systemOrange { return .systemOrange }
        return .secondaryLabelColor
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

    private func buildRecentTextCard() -> (view: NSView, update: () -> Void) {
        let label = stableLabel(font: .systemFont(ofSize: 13), maxLines: 3)
        let view = group(title: "最近文字", views: [label])
        func update() {
            let text = appState.lastText ?? "尚無定稿文字"
            if label.stringValue != text { label.stringValue = text }
            label.textColor = appState.lastText == nil ? .secondaryLabelColor : .labelColor
        }
        update()
        return (view, update)
    }

    /// A label whose height is pinned to `maxLines` up front (rather than a
    /// minimum), so a status string going from one line to two — or back —
    /// never shifts the cards below it. Longer text truncates with an
    /// ellipsis instead of growing the layout.
    ///
    /// `maxLines: nil` opts out of that trade entirely: the label gets no
    /// line cap and no fixed height, so it wraps and grows like the label
    /// this refactor replaced. Error/diagnostic text (service errors,
    /// "Service probe", "Client state" failures) uses this — hiding the
    /// only clue to what went wrong behind an ellipsis is worse than the
    /// card occasionally growing. Everything else keeps the fixed-height
    /// path, since those fields update far more often (every status tick)
    /// and are the ones the earlier jitter bug was actually about.
    private func stableLabel(font: NSFont, maxLines: Int?, color: NSColor? = nil) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: "")
        label.font = font
        if let color {
            label.textColor = color
        }
        let lineHeight = (font.ascender - font.descender + font.leading).rounded(.up)
        guard let maxLines else {
            label.maximumNumberOfLines = 0
            label.lineBreakMode = .byWordWrapping
            // Floor at one line so an empty/short value still occupies the
            // same box as its single-line neighbours (one vertical rhythm per
            // group) while a long error is free to grow past it. This used to
            // floor at two lines, which is what made every row in 總覽/診斷與權限
            // 40pt tall even when every value was a short single line.
            label.heightAnchor.constraint(greaterThanOrEqualToConstant: lineHeight).isActive = true
            return label
        }
        label.maximumNumberOfLines = maxLines
        label.lineBreakMode = .byTruncatingTail
        label.heightAnchor.constraint(equalToConstant: lineHeight * CGFloat(maxLines)).isActive = true
        return label
    }

    /// `maxLines` defaults to **one** line. The fixed-height trick exists to
    /// stop a row from jittering as its value changes, and one fixed line does
    /// that just as well as two — while two lines of reserved space under a
    /// value that is always one line is exactly what made these pages feel
    /// twice as loose as a System Settings pane. Callers pass `2` only where
    /// the value genuinely wraps at the window's minimum width (device names,
    /// gap reasons, autosave error text), and `nil` where the value is an
    /// error message that must not be truncated at all.
    private func buildValueRow(title: String, maxLines: Int? = 1) -> (view: NSView, update: (String) -> Void) {
        let key = NSTextField(labelWithString: title)
        key.font = .systemFont(ofSize: 13)
        key.textColor = .secondaryLabelColor
        key.alignment = .right
        key.lineBreakMode = .byTruncatingTail
        // Same column width as the settings grid, so a value on one page and a
        // control on another start on the same vertical line.
        key.widthAnchor.constraint(equalToConstant: Metrics.labelColumn).isActive = true
        let value = stableLabel(font: .systemFont(ofSize: 13), maxLines: maxLines)
        let row = NSStackView(views: [key, value])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = Metrics.row
        // `value` has no floor on how narrow it can go (a wrapping label with
        // `maxLines: nil` can always wrap into more lines), so once this row
        // itself is pinned to a `.required` width from above (see
        // `stretchArrangedSubviewsToFullWidth`), the horizontal `.fill`
        // distribution has nothing to anchor `value`'s trailing edge to and
        // the width negotiation degenerates. Pinning it explicitly gives
        // `value` a definite width instead of an unbounded one.
        value.trailingAnchor.constraint(equalTo: row.trailingAnchor).isActive = true
        return (row, { text in
            guard value.stringValue != text else { return }
            value.stringValue = text
        })
    }

    private func actionButton(title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        // A standard push button draws its own label in the system's control
        // text colour; tinting it with the accent colour is a web-ish accent
        // that no stock macOS button has.
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
        textView.drawsBackground = false
        textView.textContainerInset = NSSize(width: Metrics.tight, height: Metrics.tight)
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        // A read-only text area in an AppKit window reads as a text area
        // because of its bezel, not because of a rounded custom card.
        scroll.drawsBackground = true
        scroll.backgroundColor = .textBackgroundColor
        scroll.borderType = .bezelBorder
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

    /// Settings-page connection summary. Unlike `serverSummary()` (used on
    /// Overview, where the address is redundant with the settings page) this
    /// always leads with the address/port actually in effect, since the whole
    /// point of this row is "is this the machine I think it is, and is it
    /// answering". It draws only on state `appState`/`settings` already have
    /// — the server exposes no additional connection metadata to show here.
    private func connectionSummaryText() -> String {
        let address = "\(settings.host):\(settings.port)"
        if let error = appState.serviceError {
            return "\(address) · \(error.localizedDescription)"
        }
        guard let snapshot = appState.serviceSnapshot else {
            return appState.serviceReachable == false ? "\(address) · 無法連線" : "\(address) · 檢查中…"
        }
        switch snapshot.status.modelState.lowercased() {
        case "idle_unloaded", "standby":
            return "\(address) · 已連線，待命中"
        default:
            return snapshot.readyzOK ? "\(address) · 已連線，服務就緒" : "\(address) · 已連線，尚未就緒"
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
    /// 提供測試檢查音訊電平監看器狀態。
    var debugAudioLevelMonitor: AudioLevelMonitor { audioLevelMonitor }

    /// 提供測試檢查音訊電平視圖狀態。
    var debugAudioLevelBar: AudioLevelBarView { audioLevelBar }

    /// 提供測試檢查電平監看器啟動次數。
    var debugMonitorStartCount: Int { monitorStartCount }

    /// Test-only introspection of the build-once/update-in-place refactor:
    /// the section view currently mounted in the detail pane, so a test can
    /// assert its identity is stable (`===`) across status updates instead
    /// of being torn down and recreated. Kept behind `DEBUG` so it never
    /// ships as part of the app's runtime surface.
    var debugMountedSectionView: NSView? { detailView.subviews.first }

    /// The sidebar split item, so a test can assert it is fixed furniture
    /// (pinned width, never collapsible) rather than a resizable pane.
    var debugSidebarSplitItem: NSSplitViewItem { splitViewController.splitViewItems[0] }

    /// The effective drag rect AppKit would hand the divider. `.zero` means
    /// there is no hit area, i.e. the divider cannot be grabbed at all.
    func debugDividerDragRect() -> NSRect {
        let splitView = splitViewController.splitView
        let drawn = NSRect(x: sidebarWidth, y: 0, width: splitView.dividerThickness, height: splitView.bounds.height)
        return splitViewController.splitView(
            splitView,
            effectiveRect: drawn,
            forDrawnRect: drawn,
            ofDividerAt: 0
        )
    }

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

    /// The Logs page's rendered event list, for asserting on content that
    /// lives in an `NSTextView` rather than an `NSTextField` (so it never
    /// shows up in `debugLabelTexts()`).
    var debugLogsRenderedText: String? { logsTextView?.string }

    /// The Logs page's "服務輸出" tab content, same reasoning as
    /// `debugLogsRenderedText` above.
    var debugServiceOutputText: String? { logsServiceOutputTextView?.string }

    /// Lets a test simulate "this app already has a service process handle"
    /// without going through `toggleManagedService` (which needs a real,
    /// resolvable executable path). Tests construct a real `ManagedProcess`
    /// around a short-lived helper process (e.g. `/bin/sh -c 'sleep 1'`) so
    /// `isRunning`/`terminate()` behave exactly as they would in production.
    var debugManagedService: ManagedProcess? {
        get { managedService }
        set { managedService = newValue }
    }

    /// Same idea as `debugManagedService`, for the model-prepare action.
    var debugModelPrepareProcess: ManagedProcess? {
        get { modelPrepareProcess }
        set { modelPrepareProcess = newValue }
    }

    /// Lets a test seed the service login-item's cached probe result
    /// directly, the same way `debugManagedService` seeds a fake process
    /// handle — the real probe runs off the main thread (see
    /// `refreshServiceLoginItemState()`), so a synchronous test would
    /// otherwise race it.
    var debugServiceLoginItemInstalled: Bool? {
        get { serviceLoginItemInstalled }
        set { serviceLoginItemInstalled = newValue }
    }
}
#endif
