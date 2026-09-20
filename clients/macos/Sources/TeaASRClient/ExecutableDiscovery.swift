import Foundation

/// Pure candidate-list logic for finding the `tea-asr` executable when the
/// user has not configured an explicit path.
///
/// This is deliberately free of `FileManager`/`Bundle`/`ProcessInfo` calls:
/// every function here takes plain strings in and returns plain strings out,
/// so tests can drive every branch (found at position N, not found at all,
/// a path that exists but is not executable) with fabricated candidate lists
/// and a fake `isExecutable` closure — never touching the real filesystem.
/// `ServiceControl` is the thin, untested-by-design layer that supplies the
/// real `$PATH`, the real starting directory, and the real
/// `FileManager.isExecutableFile` check.
enum ExecutableDiscovery {
    static let executableName = "tea-asr"

    /// One directory to look for `tea-asr` in, plus a human-readable label
    /// for diagnostics (what the "找不到" message tells the user it tried).
    struct Candidate: Equatable {
        let path: String
        let source: String
    }

    /// Builds the ordered list of places to look, in priority order:
    /// 1. A couple of fixed Homebrew/local install locations.
    /// 2. Every directory on `$PATH`.
    /// 3. A source checkout's `.venv/bin/tea-asr`, discovered by climbing up
    ///    from `startingDirectory` (e.g. the app bundle's own location) since
    ///    the running binary can sit several directories below the checkout
    ///    root (`clients/macos/.build/…`).
    static func candidates(
        pathEnvironment: String?,
        startingDirectory: String?,
        maxVenvAncestors: Int = 8
    ) -> [Candidate] {
        var result: [Candidate] = [
            Candidate(path: "/opt/homebrew/bin/\(executableName)", source: "Homebrew"),
            Candidate(path: "/usr/local/bin/\(executableName)", source: "/usr/local/bin"),
        ]
        if let pathEnvironment {
            for directory in pathEnvironment.split(separator: ":") where !directory.isEmpty {
                result.append(
                    Candidate(
                        path: (String(directory) as NSString).appendingPathComponent(executableName),
                        source: "PATH"
                    )
                )
            }
        }
        if let startingDirectory, !startingDirectory.isEmpty {
            for path in venvCandidatePaths(startingAt: startingDirectory, maxAncestors: maxVenvAncestors) {
                result.append(Candidate(path: path, source: "專案 .venv"))
            }
        }
        return result
    }

    /// `<ancestor>/.venv/bin/tea-asr` for `startingDirectory` and each of its
    /// parents, up to `maxAncestors` levels up (or until `/` is reached,
    /// whichever comes first). Pure string manipulation: climbing a path that
    /// does not exist on disk still produces a well-formed candidate list,
    /// which is exactly what lets this be tested without a filesystem.
    static func venvCandidatePaths(startingAt startingDirectory: String, maxAncestors: Int) -> [String] {
        var result: [String] = []
        var current = startingDirectory
        for _ in 0...max(0, maxAncestors) {
            result.append((current as NSString).appendingPathComponent(".venv/bin/\(executableName)"))
            let parent = (current as NSString).deletingLastPathComponent
            if parent.isEmpty || parent == current {
                break
            }
            current = parent
        }
        return result
    }

    /// The first candidate whose path satisfies `isExecutable`, or `nil` if
    /// none do. `isExecutable` is injected so tests can simulate "exists",
    /// "missing", and "exists but not executable" without real files.
    static func firstExecutable(
        in candidates: [Candidate],
        isExecutable: (String) -> Bool
    ) -> Candidate? {
        candidates.first { isExecutable($0.path) }
    }
}
