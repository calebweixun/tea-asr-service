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
