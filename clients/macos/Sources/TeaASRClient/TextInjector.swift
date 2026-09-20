import AppKit
import ApplicationServices

/// The application and focused accessibility element that owned the input at
/// dictation start.  A PID alone is too coarse: moving focus within an app
/// must not make a later final paste land in a different field.
struct TextInsertionTarget: Equatable {
    let processIdentifier: pid_t
    let focusedElementIdentifier: UInt64
    let accessibilityIdentifier: String?

    init(
        processIdentifier: pid_t,
        focusedElementIdentifier: UInt64,
        accessibilityIdentifier: String? = nil
    ) {
        self.processIdentifier = processIdentifier
        self.focusedElementIdentifier = focusedElementIdentifier
        self.accessibilityIdentifier = accessibilityIdentifier
    }
}

enum TextInsertionResult: Equatable {
    case inserted
    case copiedBecauseFocusChanged
}

enum TextInsertionTargetPolicy {
    static func allowsInsertion(
        captured: TextInsertionTarget?,
        current: TextInsertionTarget?
    ) -> Bool {
        guard let captured, let current else { return false }
        guard captured.processIdentifier == current.processIdentifier else { return false }
        if let capturedIdentifier = captured.accessibilityIdentifier,
           let currentIdentifier = current.accessibilityIdentifier {
            return capturedIdentifier == currentIdentifier
        }
        return captured.focusedElementIdentifier == current.focusedElementIdentifier
    }
}

/// Types recognised text into whatever app has focus.
///
/// Only `final` text is ever inserted. docs/01 is explicit that a preview must
/// not delete characters the user already typed, so partials never come here.
enum TextInjector {
    static var isTrusted: Bool {
        AXIsProcessTrusted()
    }

    /// Shows the system prompt once; the user still has to flip the switch in
    /// System Settings, so callers must handle the untrusted case.
    static func requestTrust() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        _ = AXIsProcessTrustedWithOptions(options as CFDictionary)
    }

    /// Capture the current focused application and AX element. When the
    /// focused app is the management window itself, return nil rather than
    /// treating TEA ASR as a valid paste target.
    static func captureFocusedTarget(excluding excludedProcessIdentifier: pid_t? = nil) -> TextInsertionTarget? {
        guard let focused = focusedElementAndProcess() else { return nil }
        if let excludedProcessIdentifier, focused.processIdentifier == excludedProcessIdentifier {
            return nil
        }
        return focused
    }

    /// Insert only if the same application and AX element still own focus.
    /// Otherwise leave the final visible in the clipboard for an explicit
    /// user paste; this method never activates or refocuses another app.
    @discardableResult
    static func insert(_ text: String, ifCurrent target: TextInsertionTarget) -> TextInsertionResult {
        guard !text.isEmpty else { return .copiedBecauseFocusChanged }
        guard TextInsertionTargetPolicy.allowsInsertion(
            captured: target,
            current: focusedElementAndProcess()
        ) else {
            copyToClipboard(text)
            return .copiedBecauseFocusChanged
        }
        let saved = savePasteboard()
        copyToClipboard(text)
        paste()
        restoreClipboardAfterPaste(saved)
        return .inserted
    }

    /// Compatibility entry point for callers that have explicitly decided
    /// that current-focus insertion is safe. New dictation code should pass a
    /// captured target through `insert(_:ifCurrent:)`.
    @discardableResult
    static func insert(_ text: String) -> TextInsertionResult {
        guard let target = captureFocusedTarget(
            excluding: NSRunningApplication.current.processIdentifier
        ) else {
            copyToClipboard(text)
            return .copiedBecauseFocusChanged
        }
        return insert(text, ifCurrent: target)
    }

    static func copyToClipboard(_ text: String) {
        guard !text.isEmpty else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }

    private static func focusedElementAndProcess() -> TextInsertionTarget? {
        let system = AXUIElementCreateSystemWide()
        var focusedApplicationValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            system,
            kAXFocusedApplicationAttribute as CFString,
            &focusedApplicationValue
        ) == .success,
        let focusedApplicationValue
        else { return nil }
        let focusedApplication = focusedApplicationValue as! AXUIElement

        var processIdentifier: pid_t = 0
        guard AXUIElementGetPid(focusedApplication, &processIdentifier) == .success else {
            return nil
        }

        var focusedElementValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            focusedApplication,
            kAXFocusedUIElementAttribute as CFString,
            &focusedElementValue
        ) == .success,
        let focusedElementValue
        else { return nil }
        let focusedElement = focusedElementValue as! AXUIElement

        let identifier = UInt64(
            UInt(bitPattern: Unmanaged.passUnretained(focusedElement as AnyObject).toOpaque())
        )
        return TextInsertionTarget(
            processIdentifier: processIdentifier,
            focusedElementIdentifier: identifier,
            accessibilityIdentifier: accessibilityIdentifier(of: focusedElement)
        )
    }

    private static func accessibilityIdentifier(of element: AXUIElement) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            element,
            kAXIdentifierAttribute as CFString,
            &value
        ) == .success,
        let value
        else { return nil }
        return value as? String
    }

    private static func savePasteboard() -> [[NSPasteboard.PasteboardType: Data]]? {
        NSPasteboard.general.pasteboardItems?.map { item in
            var copy: [NSPasteboard.PasteboardType: Data] = [:]
            for type in item.types {
                if let data = item.data(forType: type) { copy[type] = data }
            }
            return copy
        }
    }

    private static func restoreClipboardAfterPaste(
        _ saved: [[NSPasteboard.PasteboardType: Data]]?
    ) {
        // Preserve whatever the user had on the clipboard: silently eating it
        // would be a nasty surprise during a long dictation session. The
        // caller has already placed the final text there for the paste event.
        // This helper is kept separate from the focus guard so no clipboard
        // restoration can occur on the no-paste path.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            guard let saved else { return }
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            for entry in saved {
                let item = NSPasteboardItem()
                for (type, data) in entry { item.setData(data, forType: type) }
                pasteboard.writeObjects([item])
            }
        }
    }

    private static func paste() {
        guard let source = CGEventSource(stateID: .combinedSessionState) else { return }
        let v: CGKeyCode = 0x09
        guard
            let down = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: true),
            let up = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: false)
        else { return }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: .cgAnnotatedSessionEventTap)
        up.post(tap: .cgAnnotatedSessionEventTap)
    }

}
