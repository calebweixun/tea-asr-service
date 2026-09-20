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
            guard shortcutIsDown else { return [] }
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
