import Foundation

/// User-visible configuration. The bearer token is read from the service's own
/// file rather than stored here, so it never ends up in a preferences plist.
struct Settings {
    // swiftlint:disable:next identifier_name
    private enum Key {
        static let host = "serverHost"
        static let port = "serverPort"
        static let autoInsert = "autoInsertOnFinal"
        static let revisablePreview = "requestRevisablePreview"
        static let serviceExecutable = "serviceExecutablePath"
        static let inputDeviceUID = "audioInputDeviceUID"
        static let inputChannelPolicy = "audioInputChannelPolicy"
        static let shortcutKeyCode = "dictationShortcutKeyCode"
        static let shortcutModifiers = "dictationShortcutModifiers"
        static let interactionMode = "dictationInteractionMode"
        static let startStopFeedback = "dictationStartStopFeedback"
        static let stripTrailingPunctuation = "stripTrailingPunctuation"
        static let spokenSymbols = "spokenSymbols"
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    var host: String {
        get { defaults.string(forKey: Key.host) ?? "127.0.0.1" }
        nonmutating set { defaults.set(newValue, forKey: Key.host) }
    }

    var port: Int {
        get {
            let stored = defaults.integer(forKey: Key.port)
            return stored == 0 ? 8327 : stored
        }
        nonmutating set { defaults.set(newValue, forKey: Key.port) }
    }

    /// Paste the text into the frontmost app when a segment is final.
    var autoInsert: Bool {
        get { defaults.object(forKey: Key.autoInsert) as? Bool ?? true }
        nonmutating set { defaults.set(newValue, forKey: Key.autoInsert) }
    }

    /// Show live preview text while speaking. Only meeting mode uses it:
    /// dictation never types a partial, so asking for one would just spend
    /// inference the user cannot see.
    var revisablePreview: Bool {
        get { defaults.object(forKey: Key.revisablePreview) as? Bool ?? true }
        nonmutating set { defaults.set(newValue, forKey: Key.revisablePreview) }
    }

    /// Set by `--url` in self-test runs; otherwise derived from host and port.
    var overrideStreamURL: URL?

    /// Explicit path to the `tea-asr` binary. Empty means "go and find it".
    var serviceExecutable: String {
        get { defaults.string(forKey: Key.serviceExecutable) ?? "" }
        nonmutating set { defaults.set(newValue, forKey: Key.serviceExecutable) }
    }

    /// Stable CoreAudio device UID. `nil` means System Default; volatile
    /// AudioDeviceID values are intentionally never persisted.
    var inputDeviceUID: String? {
        get {
            guard let value = defaults.string(forKey: Key.inputDeviceUID), !value.isEmpty else {
                return nil
            }
            return value
        }
        nonmutating set {
            if let newValue, !newValue.isEmpty {
                defaults.set(newValue, forKey: Key.inputDeviceUID)
            } else {
                defaults.removeObject(forKey: Key.inputDeviceUID)
            }
        }
    }

    /// Persisted channel policy. The default mixes all source channels down
    /// to mono, which preserves the previous behavior for every device.
    var inputChannelPolicy: AudioChannelPolicy {
        get { AudioChannelPolicy(rawValue: defaults.string(forKey: Key.inputChannelPolicy)) }
        nonmutating set { defaults.set(newValue.rawValue, forKey: Key.inputChannelPolicy) }
    }

    var audioInputConfiguration: AudioInputConfiguration {
        get {
            AudioInputConfiguration(
                deviceUID: inputDeviceUID,
                channelPolicy: inputChannelPolicy
            )
        }
        nonmutating set {
            inputDeviceUID = newValue.deviceUID
            inputChannelPolicy = newValue.channelPolicy
        }
    }

    /// User-configurable global shortcut.  Both values are stable app-owned
    /// primitives rather than an NSEvent object, so the setting survives
    /// relaunches and macOS representation changes.
    var shortcut: GlobalShortcut {
        get {
            guard
                defaults.object(forKey: Key.shortcutKeyCode) != nil,
                defaults.object(forKey: Key.shortcutModifiers) != nil
            else { return .default }
            let keyCode = UInt32(defaults.integer(forKey: Key.shortcutKeyCode))
            let modifiers = ShortcutModifiers(
                rawValue: UInt32(defaults.integer(forKey: Key.shortcutModifiers))
            )
            return (try? GlobalShortcut(keyCode: keyCode, modifiers: modifiers)) ?? .default
        }
        nonmutating set {
            defaults.set(Int(newValue.keyCode), forKey: Key.shortcutKeyCode)
            defaults.set(Int(newValue.modifiers.rawValue), forKey: Key.shortcutModifiers)
        }
    }

    var interactionMode: DictationInteractionMode {
        get {
            guard let raw = defaults.string(forKey: Key.interactionMode) else { return .toggle }
            return DictationInteractionMode(rawValue: raw) ?? .toggle
        }
        nonmutating set { defaults.set(newValue.rawValue, forKey: Key.interactionMode) }
    }

    /// Conservative by default: a user may opt into native system beeps at
    /// session boundaries, but no bundled sound asset is shipped.
    var startStopFeedback: Bool {
        get { defaults.object(forKey: Key.startStopFeedback) as? Bool ?? false }
        nonmutating set { defaults.set(newValue, forKey: Key.startStopFeedback) }
    }

    /// Removes a trailing sentence-final full stop from recognized text
    /// (see `TrailingPeriodStripRule`).
    ///
    /// On by default. This used to be an opt-in (the processing pipeline's
    /// general default is a strict no-op, and this changes recognized text
    /// content rather than presentation), but the user reported the same
    /// complaint twice — the model appends a full stop they never spoke —
    /// so leaving it off by default no longer matched what almost everyone
    /// asking about this actually wants. The scope stays deliberately
    /// narrow even with the default flipped: only a period-class trailing
    /// character ("。"/"．"/".") is ever touched. A trailing "？"/"！" is
    /// still left alone, because unlike a bare full stop, a question or
    /// exclamation mark carries intonation the user actually spoke — removing
    /// it would silently change what the sentence means, not just tidy up an
    /// artifact the model added on its own.
    var stripTrailingPunctuation: Bool {
        get { defaults.object(forKey: Key.stripTrailingPunctuation) as? Bool ?? true }
        nonmutating set { defaults.set(newValue, forKey: Key.stripTrailingPunctuation) }
    }

    /// Replaces a spoken punctuation-mark name (e.g. "逗號") with the mark
    /// itself (see `SpokenSymbolReplacementRule`'s doc comment for the full
    /// table and the quoted-span mitigation it applies).
    ///
    /// Off by default, unlike `stripTrailingPunctuation` above. That rule
    /// only ever removes one specific, narrow, already-established nuisance
    /// (an auto-appended trailing full stop); this one rewrites arbitrary
    /// occurrences of ordinary Chinese nouns ("逗號", "括號", …) anywhere in
    /// the text, with no reliable way to tell a command ("加一個逗號") apart
    /// from a description ("逗號的用法") from text alone. That is a real,
    /// unresolved risk of corrupting genuine dictated content, not just an
    /// occasional false positive — so it stays an explicit opt-in.
    var spokenSymbols: Bool {
        get { defaults.object(forKey: Key.spokenSymbols) as? Bool ?? false }
        nonmutating set { defaults.set(newValue, forKey: Key.spokenSymbols) }
    }

    var streamURL: URL {
        overrideStreamURL ?? URL(string: "ws://\(host):\(port)/v1/stream")!
    }

    var tokenFile: URL {
        FileManager.default
            .homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/TEA ASR/token")
    }

    func token() throws -> String {
        let raw = try String(contentsOf: tokenFile, encoding: .utf8)
        return raw.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
