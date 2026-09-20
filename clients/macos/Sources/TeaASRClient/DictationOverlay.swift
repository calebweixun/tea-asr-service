import AppKit

enum DictationOverlayState: Equatable {
    case hidden
    /// The shortcut was pressed and the start path is running, but nothing is
    /// being recorded yet: microphone consent, the process-wide input-device
    /// lease and the audio unit can all still fail after this point. It is a
    /// separate state from `.listening` precisely so the overlay can appear
    /// immediately without claiming a recording that has not begun.
    case starting
    case listening
    case partial(String)
    case final(String)
    /// The final text could not be inserted (the captured focus target no
    /// longer matches, there is no captured target, Accessibility is not
    /// granted, or auto-insert is off) and was left on the clipboard
    /// instead. Distinct from `.final` so the overlay can tell the user
    /// explicitly rather than looking identical to a successful insertion.
    case copiedToClipboard(text: String, reason: String)
    case stopping
    case error(String)
}

enum DictationOverlayEvent: Equatable {
    /// The user asked for a session. Shown before the audio path is ready.
    case startRequested
    /// Audio capture is actually running.
    case begin
    case partial(String)
    case final(String)
    case copiedToClipboard(text: String, reason: String)
    case stopRequested
    case stopped
    case failed(String)
    case clear
}

enum DictationOverlayPolicy {
    static func transition(
        _ state: DictationOverlayState,
        event: DictationOverlayEvent
    ) -> DictationOverlayState {
        switch event {
        case .startRequested:
            return .starting
        case .begin:
            return .listening
        case .partial(let text):
            return text.isEmpty ? .listening : .partial(text)
        case .final(let text):
            return text.isEmpty ? .listening : .final(text)
        case .copiedToClipboard(let text, let reason):
            return .copiedToClipboard(text: text, reason: reason)
        case .stopRequested:
            return .stopping
        case .stopped, .clear:
            return .hidden
        case .failed(let message):
            return .error(message)
        }
    }
}

/// What a timed auto-dismiss must do once its timer fires.
///
/// A dictation session is continuous: toggle mode keeps one session open
/// across many utterances. Hiding the overlay after every final would leave
/// a still-recording session with no visible indication at all, which reads
/// to the user as "it stopped" — and then the next shortcut press, which
/// actually *does* stop it, looks like the app ignored them. So a timed
/// dismissal only hides when the session is really over; otherwise it falls
/// back to the honest "still listening" state.
enum DictationOverlayAutoDismiss: Equatable {
    case hide
    case returnToListening
}

extension DictationOverlayState {
    /// Whether this state puts the floating panel on screen. `.starting` is
    /// deliberately visible: the whole point of the state is that feedback
    /// appears before the audio path is ready.
    var isVisible: Bool { self != .hidden }
}

enum DictationOverlayAutoDismissPolicy {
    static func outcome(sessionIsActive: Bool) -> DictationOverlayAutoDismiss {
        sessionIsActive ? .returnToListening : .hide
    }
}

/// A panel that can be ordered above other windows without ever becoming the
/// key/main window.  That is important for dictation: showing a partial must
/// not change the target app receiving the eventual final insertion.
private final class NonActivatingOverlayPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class DictationOverlayController {
    private let panel: NonActivatingOverlayPanel
    private let titleLabel = NSTextField(labelWithString: "")
    private let detailLabel = NSTextField(wrappingLabelWithString: "")
    private var state: DictationOverlayState = .hidden
    private var renderGeneration = 0
    /// Whether a dictation session is still running behind the overlay. Drives
    /// `DictationOverlayAutoDismissPolicy`; see its documentation.
    private var sessionIsActive = false

    init() {
        panel = NonActivatingOverlayPanel(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 108),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.ignoresMouseEvents = true
        panel.isMovable = false
        panel.title = "TEA ASR"

        let visual = NSVisualEffectView()
        visual.material = .hudWindow
        visual.blendingMode = .behindWindow
        visual.state = .active
        visual.wantsLayer = true
        visual.layer?.cornerRadius = 18
        visual.layer?.masksToBounds = true

        let icon = NSImageView(
            image: NSImage(systemSymbolName: "waveform", accessibilityDescription: "TEA ASR")
                ?? NSImage()
        )
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 22, weight: .semibold)
        icon.contentTintColor = .controlAccentColor
        icon.setContentHuggingPriority(.required, for: .horizontal)

        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        titleLabel.textColor = .labelColor
        detailLabel.font = .systemFont(ofSize: 13)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 2
        detailLabel.lineBreakMode = .byTruncatingTail

        let labels = NSStackView(views: [titleLabel, detailLabel])
        labels.orientation = .vertical
        labels.alignment = .leading
        labels.spacing = 4

        let row = NSStackView(views: [icon, labels])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12
        row.translatesAutoresizingMaskIntoConstraints = false
        visual.addSubview(row)
        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: visual.leadingAnchor, constant: 18),
            row.trailingAnchor.constraint(equalTo: visual.trailingAnchor, constant: -18),
            row.topAnchor.constraint(equalTo: visual.topAnchor, constant: 14),
            row.bottomAnchor.constraint(equalTo: visual.bottomAnchor, constant: -14),
        ])
        panel.contentView = visual
    }

    /// Called the moment the user asks for a session, before microphone
    /// consent, the device lease and the audio unit have been dealt with.
    func startRequested() {
        sessionIsActive = true
        transition(.startRequested)
    }

    func begin() {
        sessionIsActive = true
        transition(.begin)
    }

    func showPartial(_ text: String) {
        transition(.partial(text))
    }

    func showFinal(_ text: String) {
        transition(.final(text))
        guard !text.isEmpty else { return }
        scheduleAutoDismiss(after: 1_400_000_000)
    }

    /// The final text was recognised but landed in the clipboard instead of
    /// the target field. This must stay visible noticeably longer than a
    /// successful `.final` toast: a silent auto-dismiss here is exactly the
    /// "nothing happened" symptom the user has to be protected from.
    func showCopiedToClipboard(_ text: String, reason: String) {
        transition(.copiedToClipboard(text: text, reason: reason))
        scheduleAutoDismiss(after: 3_500_000_000)
    }

    func stopRequested() {
        sessionIsActive = false
        transition(.stopRequested)
    }

    func stopped() {
        sessionIsActive = false
        transition(.stopped)
    }

    func showError(_ message: String) {
        sessionIsActive = false
        transition(.failed(message))
        scheduleAutoDismiss(after: 2_000_000_000)
    }

    func hide() {
        sessionIsActive = false
        renderGeneration += 1
        state = .hidden
        panel.orderOut(nil)
    }

    /// One timed dismissal, guarded by the render generation so any newer
    /// transition wins, and routed through the auto-dismiss policy so an
    /// utterance inside a still-running session returns to `.listening`
    /// instead of hiding the only feedback the session has.
    private func scheduleAutoDismiss(after nanoseconds: UInt64) {
        let generation = renderGeneration
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: nanoseconds)
            guard let self, self.renderGeneration == generation else { return }
            self.applyAutoDismiss()
        }
    }

    private func applyAutoDismiss() {
        switch DictationOverlayAutoDismissPolicy.outcome(sessionIsActive: sessionIsActive) {
        case .hide:
            hide()
        case .returnToListening:
            transition(.begin)
        }
    }

    private func transition(_ event: DictationOverlayEvent) {
        renderGeneration += 1
        state = DictationOverlayPolicy.transition(state, event: event)
        render()
    }

    private func render() {
        switch state {
        case .hidden:
            panel.orderOut(nil)
        case .starting:
            titleLabel.stringValue = "準備聽寫"
            detailLabel.stringValue = "正在啟動麥克風…（尚未開始錄音）"
            present()
        case .listening:
            titleLabel.stringValue = "正在聽寫"
            detailLabel.stringValue = "正在聆聽…"
            present()
        case .partial(let text):
            titleLabel.stringValue = "正在聽寫"
            detailLabel.stringValue = text
            present()
        case .final(let text):
            titleLabel.stringValue = "已辨識"
            detailLabel.stringValue = text
            present()
        case .copiedToClipboard(let text, let reason):
            titleLabel.stringValue = "已複製到剪貼簿（\(reason)）"
            detailLabel.stringValue = text
            present()
        case .stopping:
            titleLabel.stringValue = "正在停止"
            detailLabel.stringValue = "等待最後一句…"
            present()
        case .error(let message):
            titleLabel.stringValue = "聽寫已停止"
            detailLabel.stringValue = message
            present()
        }
    }

    private func present() {
        guard let screen = NSScreen.main else {
            panel.orderFrontRegardless()
            return
        }
        let visible = screen.visibleFrame
        let frame = panel.frame
        panel.setFrameOrigin(
            NSPoint(
                x: visible.midX - frame.width / 2,
                y: visible.minY + 64
            )
        )
        // `orderFrontRegardless` orders a nonactivating panel without making
        // it key, so the previously focused target application stays active.
        panel.orderFrontRegardless()
    }
}

#if DEBUG
extension DictationOverlayController {
    /// The state currently rendered, so a test can assert that a shortcut
    /// press alone — with no audio path involved — already produced visible
    /// feedback, and that it is not the "recording" state.
    var debugState: DictationOverlayState { state }

    var debugSessionIsActive: Bool { sessionIsActive }

    /// Runs the timed dismissal's body immediately, so the continuous-session
    /// behaviour can be asserted without sleeping out the real 1.4 s timer.
    func debugApplyAutoDismiss() { applyAutoDismiss() }
}
#endif
