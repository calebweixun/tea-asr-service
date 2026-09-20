import AppKit

/// A small circular "i" affordance that reveals a fixed explanation only when
/// clicked, via a native `NSPopover` — never a tooltip (which needs a
/// lingering hover, not a deliberate "tell me more" click) and never a
/// permanently rendered caption.
///
/// This exists to move purely explanatory copy (what a field does, where a
/// file lives, why a limitation exists) out of the main layout without
/// deleting the copy itself: `explanation` carries the exact same text the
/// caller used to render as a standing label. Real errors and warnings that
/// the user must see at a glance stay as plain labels elsewhere; this button
/// is only ever used for the "read it if you want to" kind of text.
///
/// `info.circle` (not `exclamationmark.circle`) is deliberate: the symbol
/// itself must not read as a warning, since most of what it discloses is
/// neutral background information, and `secondaryLabelColor` keeps it quiet
/// rather than tinting it with any semantic color.
final class InfoButton: NSButton {
    /// The exact explanatory text shown in the popover. Callers assign the
    /// same string literal that used to be a standing label's `stringValue`,
    /// so no wording changes — only presentation does. Settable so a button
    /// whose explanation depends on live state (e.g. a permission row) can be
    /// refreshed in place rather than rebuilt.
    var explanation: String {
        didSet {
            guard explanation != oldValue else { return }
            explanationLabel.stringValue = explanation
        }
    }

    /// Fixed popover geometry: an "i" button is a disclosure control, not a
    /// resizable window, so the content view has one preferred width and
    /// wraps to whatever height the text needs.
    private static let popoverContentWidth: CGFloat = 260
    private static let popoverContentInset: CGFloat = 12

    private let popover = NSPopover()
    private let explanationLabel: NSTextField

    init(explanation: String) {
        self.explanation = explanation
        let label = NSTextField(wrappingLabelWithString: explanation)
        label.font = .systemFont(ofSize: 12)
        label.textColor = .labelColor
        label.preferredMaxLayoutWidth = Self.popoverContentWidth
        self.explanationLabel = label
        super.init(frame: .zero)
        configure()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("not supported")
    }

    private func configure() {
        let symbol = NSImage(systemSymbolName: "info.circle", accessibilityDescription: "說明")
        symbol?.isTemplate = true
        image = symbol
        imagePosition = .imageOnly
        imageScaling = .scaleProportionallyUpOrDown
        bezelStyle = .regularSquare
        isBordered = false
        setButtonType(.momentaryChange)
        contentTintColor = .secondaryLabelColor
        setContentHuggingPriority(.required, for: .horizontal)
        setContentCompressionResistancePriority(.required, for: .horizontal)
        // A fixed, deliberately small hit area: this is a disclosure control
        // beside a field, not a primary action — it must never compete with
        // the control it explains for visual weight or grow the row's height.
        widthAnchor.constraint(equalToConstant: 16).isActive = true
        heightAnchor.constraint(equalToConstant: 16).isActive = true
        target = self
        action = #selector(togglePopover)

        popover.behavior = .transient
        popover.animates = true
        let controller = NSViewController()
        controller.view = makeContentView()
        popover.contentViewController = controller
    }

    private func makeContentView() -> NSView {
        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        explanationLabel.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(explanationLabel)
        let inset = Self.popoverContentInset
        NSLayoutConstraint.activate([
            explanationLabel.topAnchor.constraint(equalTo: container.topAnchor, constant: inset),
            explanationLabel.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -inset),
            explanationLabel.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: inset),
            explanationLabel.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -inset),
            explanationLabel.widthAnchor.constraint(equalToConstant: Self.popoverContentWidth),
        ])
        return container
    }

    @objc private func togglePopover() {
        if popover.isShown {
            popover.close()
            return
        }
        popover.show(relativeTo: bounds, of: self, preferredEdge: .maxY)
    }
}

#if DEBUG
extension InfoButton {
    /// Test-only introspection: the text currently rendered inside the
    /// popover's content view, so a test can assert the button discloses the
    /// right explanation without driving a real click/popover show (which
    /// needs a window server this headless test environment does not have).
    var debugPopoverText: String { explanationLabel.stringValue }

    /// Whether the popover is on screen. Never true unless something actually
    /// invoked `togglePopover`; used to assert the explanation stays hidden
    /// until the button is clicked.
    var debugPopoverIsShown: Bool { popover.isShown }
}
#endif
