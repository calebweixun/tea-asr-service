import Foundation

/// The interaction state machine receives only semantic events.  AppKit,
/// Carbon, sleep notifications, and tests can all feed it the same events,
/// which prevents a repeated keyDown or a late keyUp from toggling a second
/// session.
enum DictationInteractionEvent: Equatable {
    case shortcutDown(isRepeat: Bool)
    case shortcutUp
    case sessionEnded
    case cancelled
    case focusLost
    case systemSleep
    case permissionLost
}

enum DictationInteractionCommand: Equatable {
    case start
    case stop
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
            switch mode {
            case .toggle:
                active.toggle()
                return [active ? .start : .stop]
            case .pushToTalk:
                guard !shortcutIsDown else { return [] }
                shortcutIsDown = true
                guard !active else { return [] }
                active = true
                return [.start]
            }

        case .shortcutUp:
            guard mode == .pushToTalk, shortcutIsDown else { return [] }
            shortcutIsDown = false
            guard active else { return [] }
            active = false
            return [.stop]

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
    case failed
}

enum InteractionFeedbackPolicy {
    static func shouldPlay(
        enabled: Bool,
        event: InteractionFeedbackEvent
    ) -> Bool {
        guard enabled else { return false }
        switch event {
        case .started, .stopped:
            return true
        case .failed:
            // Errors are already presented in the management window.  Avoid
            // turning a transient network/device failure into a surprise beep.
            return false
        }
    }
}
