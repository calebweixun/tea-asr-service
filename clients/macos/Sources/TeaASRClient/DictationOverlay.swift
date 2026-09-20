import AppKit

enum DictationOverlayState: Equatable {
    case hidden
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

    func begin() {
        transition(.begin)
    }

    func showPartial(_ text: String) {
        transition(.partial(text))
    }

    func showFinal(_ text: String) {
        transition(.final(text))
        guard !text.isEmpty else { return }
        let generation = renderGeneration
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            guard let self, self.renderGeneration == generation else { return }
            self.hide()
        }
    }

    /// The final text was recognised but landed in the clipboard instead of
    /// the target field. This must stay visible noticeably longer than a
    /// successful `.final` toast: a silent auto-dismiss here is exactly the
    /// "nothing happened" symptom the user has to be protected from.
    func showCopiedToClipboard(_ text: String, reason: String) {
        transition(.copiedToClipboard(text: text, reason: reason))
        let generation = renderGeneration
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 3_500_000_000)
            guard let self, self.renderGeneration == generation else { return }
            self.hide()
        }
    }

    func stopRequested() {
        transition(.stopRequested)
    }

    func stopped() {
        transition(.stopped)
    }

    func showError(_ message: String) {
        transition(.failed(message))
        let generation = renderGeneration
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard let self, self.renderGeneration == generation else { return }
            self.hide()
        }
    }

    func hide() {
        renderGeneration += 1
        state = .hidden
        panel.orderOut(nil)
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
