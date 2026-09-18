import AppKit
import ApplicationServices

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

    static func insert(_ text: String) {
        guard !text.isEmpty else { return }
        let pasteboard = NSPasteboard.general

        // Preserve whatever the user had on the clipboard: silently eating it
        // would be a nasty surprise during a long dictation session.
        let saved = pasteboard.pasteboardItems?.compactMap { item -> [NSPasteboard.PasteboardType: Data] in
            var copy: [NSPasteboard.PasteboardType: Data] = [:]
            for type in item.types {
                if let data = item.data(forType: type) { copy[type] = data }
            }
            return copy
        }

        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        paste()

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            guard let saved else { return }
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
