import Foundation
import Carbon.HIToolbox

/// The interaction state machine receives only semantic events.  AppKit,
/// Carbon, sleep notifications, and tests can all feed it the same events,
/// which prevents a repeated keyDown or a late keyUp from toggling a second
/// session.
enum DictationInteractionEvent: Equatable {
    case shortcutDown(isRepeat: Bool)
    case shortcutUp
    /// A shortcut press while a meeting session owns the microphone. It is
    /// deliberately a no-op for the session, but remains semantic so the
    /// controller can provide an explicit status/feedback signal.
    case shortcutWhileMeeting
    case sessionEnded
    case cancelled
    case focusLost
    case systemSleep
    case permissionLost
}

enum DictationInteractionCommand: Equatable {
    case start
    case stop
    case ignoredDuringMeeting
}

struct DictationInteractionStateMachine: Equatable {
    let mode: DictationInteractionMode
    private(set) var active = false
    private(set) var shortcutIsDown = false

    init(mode: DictationInteractionMode) {
        self.mode = mode
    }

    mutating func handle(_ event: DictationInteractionEvent) -> [DictationInteractionCommand] {
        switch event {
        case .shortcutDown(let isRepeat):
            guard !isRepeat else { return [] }
            // Carbon's callback does not reliably identify auto-repeat. A
            // physical press latch is authoritative until matching release or
            // cancellation, so duplicate callbacks cannot toggle a session.
            guard !shortcutIsDown else { return [] }
            shortcutIsDown = true
            switch mode {
            case .toggle:
                active.toggle()
                return [active ? .start : .stop]
            case .pushToTalk:
                guard !active else { return [] }
                active = true
                return [.start]
            }

        case .shortcutUp:
            // Deliberately not guarded on `shortcutIsDown`. The latch exists
            // to suppress duplicate *downs*; making the release depend on it
            // means a latch that was cleared for any other reason (the hot
            // key being re-registered under the user's finger, a manual
            // session start) turns a real key release into a no-op and leaves
            // push-to-talk recording with nothing left to stop it. An
            // unmatched release while nothing is active is still a no-op.
            shortcutIsDown = false
            guard mode == .pushToTalk else { return [] }
            guard active else { return [] }
            active = false
            return [.stop]

        case .shortcutWhileMeeting:
            return [.ignoredDuringMeeting]

        case .sessionEnded:
            shortcutIsDown = false
            active = false
            return []

        case .cancelled, .focusLost, .systemSleep, .permissionLost:
            shortcutIsDown = false
            guard active else { return [] }
            active = false
            return [.stop]
        }
    }

    /// Drops the physical-press latch without touching `active`.
    ///
    /// Unregistering a Carbon hot key discards the pending
    /// `kEventHotKeyReleased` for a key that is physically down right now, so
    /// after a re-registration the latch can never be cleared by a release
    /// that will never arrive. Every later key-down is then read as a
    /// duplicate and silently dropped — the "pressed it and nothing
    /// happened" symptom. Callers that (un)register the hot key must call
    /// this, because from that moment on the latch is a claim about the
    /// keyboard that this process can no longer substantiate.
    mutating func releaseShortcutLatch() {
        shortcutIsDown = false
    }

    mutating func resetAfterStartFailure() {
        active = false
        shortcutIsDown = false
    }

    mutating func beginManualSession() {
        active = true
        shortcutIsDown = false
    }

    mutating func resetAfterSessionEnd() {
        active = false
        shortcutIsDown = false
    }
}

enum InteractionFeedbackEvent: Equatable {
    case started
    case stopped
    case ignoredDuringMeeting
    case failed
}

enum InteractionFeedbackPolicy {
    static func shouldPlay(
        enabled: Bool,
        event: InteractionFeedbackEvent
    ) -> Bool {
        guard enabled else { return false }
        switch event {
        case .started, .stopped, .ignoredDuringMeeting:
            return true
        case .failed:
            // Errors are already presented in the management window.  Avoid
            // turning a transient network/device failure into a surprise beep.
            return false
        }
    }
}

/// Pure seam for Carbon registration results. The controller requests an
/// exclusive registration and must not claim the shortcut is active when the
/// OS reports an existing owner.
enum ShortcutRegistrationOutcome: Equatable {
    case registered
    case conflict
    case unavailable

    var isRegistered: Bool { self == .registered }
}

enum ShortcutRegistrationPolicy {
    static func outcome(for status: OSStatus) -> ShortcutRegistrationOutcome {
        if status == noErr { return .registered }
        if status == OSStatus(eventHotKeyExistsErr) { return .conflict }
        return .unavailable
    }

    static func result(for status: OSStatus) -> ShortcutRegistrationResult {
        ShortcutRegistrationResult(status: status)
    }
}

struct ShortcutRegistrationResult: Equatable {
    let status: OSStatus

    var outcome: ShortcutRegistrationOutcome {
        ShortcutRegistrationPolicy.outcome(for: status)
    }

    var isRegistered: Bool {
        outcome.isRegistered
    }

    var errorDescription: String {
        switch outcome {
        case .registered:
            return "快捷鍵已註冊。"
        case .conflict:
            return "這個快捷鍵已被其他 app 占用。macOS 不提供公開 API 告知是哪個 app。"
        case .unavailable:
            return "無法註冊快捷鍵（OSStatus: \(status)）。"
        }
    }
}

enum ShortcutEditorCaptureResult: Equatable {
    case ignoredWhileClosed
    case captured(GlobalShortcut)
    case invalid(GlobalShortcut.ValidationError)
}

enum ShortcutEditorSaveError: LocalizedError, Equatable {
    case modalNotOpen
    case registrationConflict
    case registrationFailed(OSStatus)

    var errorDescription: String? {
        switch self {
        case .modalNotOpen:
            return "快捷鍵設定面板尚未開啟。"
        case .registrationConflict:
            return "這個快捷鍵已被其他 app 占用。macOS 不提供公開 API 告知是哪個 app。"
        case .registrationFailed(let status):
            return "無法註冊快捷鍵（OSStatus: \(status)）。"
        }
    }
}

enum ShortcutEditorSaveResult: Equatable {
    case saved(GlobalShortcut)
    case rejected(ShortcutEditorSaveError)
}

/// Pure state for the app-modal shortcut editor. The AppKit panel forwards
/// key events here only while it is open; keeping this seam pure makes it
/// possible to prove that a closed settings page cannot capture a key and
/// that a failed registration never mutates the persisted setting.
struct ShortcutEditorSession: Equatable {
    let original: GlobalShortcut
    private(set) var candidate: GlobalShortcut
    private(set) var isModalOpen = false

    init(original: GlobalShortcut) {
        self.original = original
        candidate = original
    }

    mutating func open() {
        candidate = original
        isModalOpen = true
    }

    mutating func capture(
        keyCode: UInt32,
        modifiers: ShortcutModifiers
    ) -> ShortcutEditorCaptureResult {
        guard isModalOpen else { return .ignoredWhileClosed }
        switch GlobalShortcut.from(keyCode: keyCode, modifiers: modifiers) {
        case .success(let shortcut):
            candidate = shortcut
            return .captured(shortcut)
        case .failure(let error):
            return .invalid(error)
        }
    }

    mutating func save(
        using probe: (GlobalShortcut) -> ShortcutRegistrationResult
    ) -> ShortcutEditorSaveResult {
        guard isModalOpen else { return .rejected(.modalNotOpen) }
        let registration = probe(candidate)
        switch registration.outcome {
        case .registered:
            isModalOpen = false
            return .saved(candidate)
        case .conflict:
            return .rejected(.registrationConflict)
        case .unavailable:
            return .rejected(.registrationFailed(registration.status))
        }
    }

    mutating func cancel() {
        candidate = original
        isModalOpen = false
    }
}

/// Global and local monitors are independent AppKit resources. Keeping the
/// missing-resource decision pure prevents a successful local monitor from
/// masking a failed global monitor on the next retry.
enum InputMonitorKind: Equatable {
    case globalKeyUp
    case localKeyUp
}

enum InputMonitorInstallPolicy {
    static func missing(globalInstalled: Bool, localInstalled: Bool) -> [InputMonitorKind] {
        var result: [InputMonitorKind] = []
        if !globalInstalled { result.append(.globalKeyUp) }
        if !localInstalled { result.append(.localKeyUp) }
        return result
    }
}
