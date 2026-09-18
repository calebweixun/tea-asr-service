import Foundation

/// User-visible configuration. The bearer token is read from the service's own
/// file rather than stored here, so it never ends up in a preferences plist.
struct Settings {
    private enum Key {
        static let host = "serverHost"
        static let port = "serverPort"
        static let autoInsert = "autoInsertOnFinal"
        static let revisablePreview = "requestRevisablePreview"
    }

    private let defaults = UserDefaults.standard

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

    var streamURL: URL {
        URL(string: "ws://\(host):\(port)/v1/stream")!
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
