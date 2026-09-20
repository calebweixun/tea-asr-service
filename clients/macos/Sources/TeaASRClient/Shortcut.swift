import AppKit
import Carbon.HIToolbox

/// Stable, app-owned modifier bits.  We deliberately do not persist
/// NSEvent.ModifierFlags.rawValue because those bits are an AppKit detail and
/// have changed meaning across APIs.  The value stored in UserDefaults is
/// instead a small protocol owned by this client.
struct ShortcutModifiers: OptionSet, Codable, Hashable {
    let rawValue: UInt32

    static let command = ShortcutModifiers(rawValue: 1 << 0)
    static let option = ShortcutModifiers(rawValue: 1 << 1)
    static let control = ShortcutModifiers(rawValue: 1 << 2)
    static let shift = ShortcutModifiers(rawValue: 1 << 3)
    static let function = ShortcutModifiers(rawValue: 1 << 4)

    static let keyboard = ShortcutModifiers([.command, .option, .control, .shift])
}

enum DictationInteractionMode: String, CaseIterable, Codable {
    case toggle
    case pushToTalk

    var title: String {
        switch self {
        case .toggle:
            return "切換模式（按一下開始／再按一下停止）"
        case .pushToTalk:
            return "按住說話（Push-to-Talk）"
        }
    }
}

struct GlobalShortcut: Equatable, Codable, Hashable {
    enum ValidationError: LocalizedError, Equatable {
        case keyCodeOutOfRange
        case modifierRequired
        case reservedCombination

        var errorDescription: String? {
            switch self {
            case .keyCodeOutOfRange:
                return "這個按鍵無法作為全域快捷鍵。"
            case .modifierRequired:
                return "快捷鍵至少需要 Command、Option、Control 或 Shift 其中一個修飾鍵。"
            case .reservedCombination:
                return "這個組合是 macOS 保留快捷鍵，請選擇其他組合。"
            }
        }
    }

    static let `default` = try! GlobalShortcut(
        keyCode: 2,
        modifiers: [.command, .option]
    )

    let keyCode: UInt32
    let modifiers: ShortcutModifiers

    init(keyCode: UInt32, modifiers: ShortcutModifiers) throws {
        guard keyCode <= 127 else { throw ValidationError.keyCodeOutOfRange }
        guard !modifiers.intersection(.keyboard).isEmpty else {
            throw ValidationError.modifierRequired
        }
        if Self.reservedCombinations.contains(
            ReservedCombination(keyCode: keyCode, modifiers: modifiers)
        ) {
            throw ValidationError.reservedCombination
        }
        self.keyCode = keyCode
        self.modifiers = modifiers
    }

    /// The few combinations macOS and common desktop conventions reserve so
    /// that changing the dictation shortcut cannot make the app interfere with
    /// quitting, window management, app switching, or Spotlight.
    private struct ReservedCombination: Hashable {
        let keyCode: UInt32
        let modifiers: ShortcutModifiers
    }

    private static let reservedCombinations: Set<ReservedCombination> = [
        ReservedCombination(keyCode: 0, modifiers: [.command]), // ⌘A is a common editing command; don't claim it globally.
        ReservedCombination(keyCode: 12, modifiers: [.command]), // ⌘Q
        ReservedCombination(keyCode: 13, modifiers: [.command]), // ⌘W
        ReservedCombination(keyCode: 4, modifiers: [.command]), // ⌘H
        ReservedCombination(keyCode: 46, modifiers: [.command]), // ⌘M
        ReservedCombination(keyCode: 49, modifiers: [.command]), // ⌘Space
        ReservedCombination(keyCode: 48, modifiers: [.command]), // ⌘Tab
    ]

    var displayString: String {
        var result = ""
        if modifiers.contains(.control) { result += "⌃" }
        if modifiers.contains(.option) { result += "⌥" }
        if modifiers.contains(.shift) { result += "⇧" }
        if modifiers.contains(.command) { result += "⌘" }
        if modifiers.contains(.function) { result += "fn" }
        return result + Self.keyName(for: keyCode)
    }

    var carbonModifierFlags: UInt32 {
        var value: UInt32 = 0
        if modifiers.contains(.command) { value |= UInt32(cmdKey) }
        if modifiers.contains(.option) { value |= UInt32(optionKey) }
        if modifiers.contains(.control) { value |= UInt32(controlKey) }
        if modifiers.contains(.shift) { value |= UInt32(shiftKey) }
        // Carbon exposes the function modifier only through event flags; it
        // is the stable 0x800000 device-independent bit.
        if modifiers.contains(.function) { value |= (1 << 23) }
        return value
    }

    var eventModifierFlags: NSEvent.ModifierFlags {
        var result: NSEvent.ModifierFlags = []
        if modifiers.contains(.command) { result.insert(.command) }
        if modifiers.contains(.option) { result.insert(.option) }
        if modifiers.contains(.control) { result.insert(.control) }
        if modifiers.contains(.shift) { result.insert(.shift) }
        if modifiers.contains(.function) { result.insert(.function) }
        return result
    }

    var menuKeyEquivalent: String {
        let names: [UInt32: String] = [
            0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z",
            7: "x", 8: "c", 9: "v", 11: "b", 12: "q", 13: "w", 14: "e",
            15: "r", 16: "y", 17: "t", 31: "o", 32: "u", 34: "i", 35: "p",
            37: "l", 38: "j", 40: "k", 41: ";", 45: "n", 46: "m",
            18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6",
            26: "7", 28: "8", 25: "9", 29: "0", 49: " ", 48: "\t",
        ]
        return names[keyCode] ?? ""
    }

    static func from(keyCode: UInt32, modifiers: ShortcutModifiers) -> Result<GlobalShortcut, ValidationError> {
        do {
            return .success(try GlobalShortcut(keyCode: keyCode, modifiers: modifiers))
        } catch let error as ValidationError {
            return .failure(error)
        } catch {
            return .failure(.keyCodeOutOfRange)
        }
    }

    static func from(event: NSEvent) -> Result<GlobalShortcut, ValidationError> {
        from(keyCode: UInt32(event.keyCode), modifiers: ShortcutModifiers(event.modifierFlags))
    }

    static func keyName(for keyCode: UInt32) -> String {
        let names: [UInt32: String] = [
            36: "↩", 48: "Tab", 49: "Space", 51: "⌫", 53: "Esc",
            55: "⌘", 56: "⇧", 57: "Caps", 58: "⌥", 59: "⌃",
            71: "Clear", 76: "↩", 115: "Home", 116: "⇞", 117: "⌦",
            119: "End", 121: "⇟", 122: "F1", 120: "F2", 99: "F3",
            118: "F4", 96: "F5", 97: "F6", 98: "F7", 100: "F8",
            101: "F9", 109: "F10", 103: "F11", 111: "F12",
        ]
        if let name = names[keyCode] { return name }
        let letters: [UInt32: String] = [
            0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X",
            8: "C", 9: "V", 11: "B", 12: "Q", 13: "W", 14: "E", 15: "R",
            16: "Y", 17: "T", 31: "O", 32: "U", 34: "I", 35: "P", 37: "L",
            38: "J", 40: "K", 41: ";", 45: "N", 46: "M",
        ]
        if let letter = letters[keyCode] { return letter }
        return "Key " + String(keyCode)
    }
}

extension ShortcutModifiers {
    init(_ eventFlags: NSEvent.ModifierFlags) {
        var result: ShortcutModifiers = []
        if eventFlags.contains(.command) { result.insert(.command) }
        if eventFlags.contains(.option) { result.insert(.option) }
        if eventFlags.contains(.control) { result.insert(.control) }
        if eventFlags.contains(.shift) { result.insert(.shift) }
        if eventFlags.contains(.function) { result.insert(.function) }
        self = result
    }
}

/// The Carbon registration path shared by the real app hot key and the
/// settings editor's temporary conflict probe. Both use exclusive ownership;
/// a probe releases its temporary registration immediately after a successful
/// check.
enum ExclusiveShortcutRegistrar {
    private static let signature = OSType(0x54454153) // 'TEAS'

    static func register(
        _ shortcut: GlobalShortcut,
        id: EventHotKeyID,
        target: EventTargetRef,
        hotKeyRef: UnsafeMutablePointer<EventHotKeyRef?>
    ) -> ShortcutRegistrationResult {
        let status = RegisterEventHotKey(
            shortcut.keyCode,
            shortcut.carbonModifierFlags,
            id,
            target,
            UInt32(kEventHotKeyExclusive),
            hotKeyRef
        )
        return ShortcutRegistrationPolicy.result(for: status)
    }

    static func probe(_ shortcut: GlobalShortcut) -> ShortcutRegistrationResult {
        var hotKeyRef: EventHotKeyRef?
        let id = EventHotKeyID(signature: signature, id: 2)
        let result = register(
            shortcut,
            id: id,
            target: GetApplicationEventTarget(),
            hotKeyRef: &hotKeyRef
        )
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
        }
        return result
    }
}

/// A settings button that displays the current shortcut but never becomes a
/// key-capture first responder. The modal editor below owns all listening.
final class ShortcutButton: NSButton {
    var shortcut: GlobalShortcut {
        didSet { title = shortcut.displayString }
    }
    var onRequestEdit: (() -> Void)?

    init(shortcut: GlobalShortcut) {
        self.shortcut = shortcut
        super.init(frame: .zero)
        title = shortcut.displayString
        bezelStyle = .rounded
        setButtonType(.momentaryPushIn)
        target = self
        action = #selector(requestEdit(_:))
        toolTip = "按一下以設定快捷鍵"
        setContentHuggingPriority(.required, for: .horizontal)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("not supported")
    }

    @objc private func requestEdit(_ sender: Any?) {
        onRequestEdit?()
    }
}

enum ShortcutEditorModalResult: Equatable {
    case saved(GlobalShortcut)
    case cancelled
}

/// Native app-modal shortcut editor. Its local event monitor exists only for
/// the lifetime of `runModal()`, so opening the Settings section alone cannot
/// consume typing or alter the stored shortcut.
final class ShortcutRecorderPanelController: NSWindowController, NSWindowDelegate {
    private var session: ShortcutEditorSession
    private let registrationProbe: (GlobalShortcut) -> ShortcutRegistrationResult
    private var keyMonitor: Any?
    private var modalResult: ShortcutEditorModalResult?
    private var modalIsRunning = false

    private let capturedLabel = NSTextField(labelWithString: "")
    private let errorLabel = NSTextField(wrappingLabelWithString: "")

    init(
        shortcut: GlobalShortcut,
        registrationProbe: @escaping (GlobalShortcut) -> ShortcutRegistrationResult = ExclusiveShortcutRegistrar.probe
    ) {
        session = ShortcutEditorSession(original: shortcut)
        self.registrationProbe = registrationProbe

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 220),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        panel.title = "設定快捷鍵"
        panel.isFloatingPanel = true
        panel.level = .modalPanel
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.isReleasedWhenClosed = false
        super.init(window: panel)
        panel.delegate = self
        buildView()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("not supported")
    }

    deinit {
        stopKeyMonitoring()
    }

    func runModal(relativeTo parent: NSWindow?) -> ShortcutEditorModalResult {
        modalResult = nil
        session.open()
        updateCapturedLabel()

        if let parent {
            window?.setFrameOrigin(NSPoint(
                x: parent.frame.midX - (window?.frame.width ?? 0) / 2,
                y: parent.frame.midY - (window?.frame.height ?? 0) / 2
            ))
        } else {
            window?.center()
        }
        modalIsRunning = true
        window?.makeKeyAndOrderFront(nil)
        startKeyMonitoring()
        NSApp.runModal(for: window!)
        modalIsRunning = false
        stopKeyMonitoring()
        window?.orderOut(nil)
        if modalResult == nil {
            session.cancel()
            modalResult = .cancelled
        }
        return modalResult ?? .cancelled
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        cancel(sender)
        return false
    }

    private func buildView() {
        guard let contentView = window?.contentView else { return }

        let prompt = NSTextField(
            wrappingLabelWithString: "按下新的快捷鍵組合。至少需要 Command、Option、Control 或 Shift。"
        )
        prompt.textColor = .secondaryLabelColor

        capturedLabel.alignment = .center
        capturedLabel.font = .systemFont(ofSize: 22, weight: .medium)
        capturedLabel.setContentHuggingPriority(.required, for: .vertical)
        capturedLabel.heightAnchor.constraint(greaterThanOrEqualToConstant: 32).isActive = true

        errorLabel.textColor = .systemRed
        errorLabel.maximumNumberOfLines = 0
        errorLabel.isHidden = true

        let cancelButton = NSButton(
            title: "取消",
            target: self,
            action: #selector(cancel(_:))
        )
        cancelButton.keyEquivalent = ""
        let saveButton = NSButton(
            title: "儲存",
            target: self,
            action: #selector(save(_:))
        )
        saveButton.keyEquivalent = ""
        let buttons = NSStackView(views: [cancelButton, saveButton])
        buttons.orientation = .horizontal
        buttons.alignment = .centerY
        buttons.spacing = 12
        buttons.setContentHuggingPriority(.required, for: .horizontal)

        let stack = NSStackView(views: [prompt, capturedLabel, errorLabel, buttons])
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 22),
            stack.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -22),
            buttons.trailingAnchor.constraint(equalTo: stack.trailingAnchor),
        ])
    }

    private func startKeyMonitoring() {
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
            guard let self, self.modalIsRunning else { return event }
            self.capture(event)
            return nil
        }
    }

    private func stopKeyMonitoring() {
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
    }

    private func capture(_ event: NSEvent) {
        switch session.capture(
            keyCode: UInt32(event.keyCode),
            modifiers: ShortcutModifiers(event.modifierFlags)
        ) {
        case .captured:
            updateCapturedLabel()
            errorLabel.isHidden = true
        case .invalid(let error):
            errorLabel.stringValue = error.localizedDescription
            errorLabel.isHidden = false
            NSSound.beep()
        case .ignoredWhileClosed:
            break
        }
    }

    private func updateCapturedLabel() {
        capturedLabel.stringValue = session.candidate.displayString
    }

    @objc private func save(_ sender: Any?) {
        switch session.save(using: registrationProbe) {
        case .saved(let shortcut):
            finish(.saved(shortcut))
        case .rejected(let error):
            errorLabel.stringValue = error.localizedDescription
            errorLabel.isHidden = false
            NSSound.beep()
        }
    }

    @objc private func cancel(_ sender: Any?) {
        session.cancel()
        finish(.cancelled)
    }

    private func finish(_ result: ShortcutEditorModalResult) {
        modalResult = result
        stopKeyMonitoring()
        window?.orderOut(nil)
        if modalIsRunning {
            NSApp.stopModal(withCode: result == .cancelled ? .cancel : .OK)
        }
    }
}
