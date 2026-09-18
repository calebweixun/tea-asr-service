import AppKit

/// Live transcript for meeting notes, with source timestamps and Markdown export.
final class MeetingWindow: NSWindowController, NSWindowDelegate {
    private let textView = NSTextView()
    private let statusLabel = NSTextField(labelWithString: "準備中…")
    private var lines: [(spokenAt: Date, text: String)] = []
    private var partialRange: NSRange?
    private var gaps: [(at: Date, reason: String)] = []
    private let startedAt = Date()

    /// Written after every final so a crash or a forgotten window does not lose
    /// an hour of meeting. The user still chooses where the real copy goes.
    private lazy var autosaveURL: URL = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmm"
        let directory = FileManager.default
            .homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/TEA ASR/meetings")
        try? FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true
        )
        return directory.appendingPathComponent("meeting-\(formatter.string(from: startedAt)).md")
    }()

    var onClose: (() -> Void)?

    convenience init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 620, height: 460),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "會議記錄"
        window.center()
        self.init(window: window)
        window.delegate = self
        build()
    }

    private func build() {
        guard let window else { return }
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.translatesAutoresizingMaskIntoConstraints = false
        textView.isEditable = false
        textView.isRichText = true
        textView.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        textView.textContainerInset = NSSize(width: 12, height: 12)
        scroll.documentView = textView

        let save = NSButton(title: "存成 Markdown…", target: self, action: #selector(save(_:)))
        save.translatesAutoresizingMaskIntoConstraints = false
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        statusLabel.textColor = .secondaryLabelColor

        let content = NSView()
        content.addSubview(scroll)
        content.addSubview(statusLabel)
        content.addSubview(save)
        window.contentView = content

        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: content.topAnchor, constant: 12),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            scroll.bottomAnchor.constraint(equalTo: save.topAnchor, constant: -12),
            statusLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            statusLabel.centerYAnchor.constraint(equalTo: save.centerYAnchor),
            save.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            save.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -12),
        ])
    }

    func setStatus(_ text: String) {
        statusLabel.stringValue = text
    }

    var autosavePath: String { autosaveURL.path }

    /// A tentative line, replaced wholesale on every revision.
    func showPartial(_ text: String, spokenAt: Date) {
        clearPartial()
        guard !text.isEmpty else { return }
        let rendered = "\(timestamp(spokenAt))  \(text)\n"
        let attributed = NSAttributedString(
            string: rendered,
            attributes: [
                .foregroundColor: NSColor.tertiaryLabelColor,
                .font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular),
            ]
        )
        let location = textView.textStorage?.length ?? 0
        textView.textStorage?.append(attributed)
        partialRange = NSRange(location: location, length: attributed.length)
        scrollToEnd()
    }

    func appendFinal(_ text: String, spokenAt: Date) {
        clearPartial()
        guard !text.isEmpty else { return }
        lines.append((spokenAt, text))
        let attributed = NSAttributedString(
            string: "\(timestamp(spokenAt))  \(text)\n",
            attributes: [
                .foregroundColor: NSColor.labelColor,
                .font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular),
            ]
        )
        textView.textStorage?.append(attributed)
        scrollToEnd()
        autosave()
    }

    private func autosave() {
        try? markdown().write(to: autosaveURL, atomically: true, encoding: .utf8)
        statusLabel.stringValue = "自動存檔：\(autosaveURL.lastPathComponent)（\(lines.count) 段）"
    }

    private func clearPartial() {
        guard let range = partialRange, let storage = textView.textStorage else { return }
        if NSMaxRange(range) <= storage.length {
            storage.deleteCharacters(in: range)
        }
        partialRange = nil
    }

    private func scrollToEnd() {
        textView.scrollToEndOfDocument(nil)
    }

    /// A gap the recording could not cover, made visible rather than hidden.
    func appendGap(_ reason: String) {
        clearPartial()
        let attributed = NSAttributedString(
            string: "\(timestamp(Date()))  —— \(reason) ——\n",
            attributes: [
                .foregroundColor: NSColor.systemOrange,
                .font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular),
            ]
        )
        textView.textStorage?.append(attributed)
        scrollToEnd()
        gaps.append((Date(), reason))
        autosave()
    }

    private func timestamp(_ moment: Date) -> String {
        let seconds = max(0, Int(moment.timeIntervalSince(startedAt)))
        return String(format: "[%02d:%02d]", seconds / 60, seconds % 60)
    }

    @objc private func save(_ sender: Any?) {
        let panel = NSSavePanel()
        let stamp = ISO8601DateFormatter().string(from: startedAt).prefix(16)
        panel.nameFieldStringValue = "meeting-\(stamp).md"
        panel.allowedContentTypes = [.init(filenameExtension: "md")!]
        panel.begin { [weak self] response in
            guard response == .OK, let url = panel.url, let self else { return }
            try? self.markdown().write(to: url, atomically: true, encoding: .utf8)
        }
    }

    private func markdown() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        var out = "# 會議記錄 \(formatter.string(from: startedAt))\n\n"
        out += "> 由 TEA ASR 本機辨識產生，未經人工校訂。\n\n"
        var entries: [(Date, String)] = lines.map { ($0.spokenAt, $0.text) }
        entries += gaps.map { ($0.at, "*—— \($0.reason) ——*") }
        for (moment, text) in entries.sorted(by: { $0.0 < $1.0 }) {
            let seconds = max(0, Int(moment.timeIntervalSince(startedAt)))
            out += String(format: "- **[%02d:%02d]** %@\n", seconds / 60, seconds % 60, text)
        }
        return out
    }

    func windowWillClose(_ notification: Notification) {
        onClose?()
    }
}
