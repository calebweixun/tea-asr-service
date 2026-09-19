import AVFoundation
import AppKit
import ApplicationServices
import Foundation

/// Small platform seam that keeps permission policy tests completely away from
/// TCC prompts and System Settings. The production adapter is below; tests can
/// provide a fake that only returns normalized values.
protocol PermissionPlatform {
    var microphoneAuthorization: PermissionAuthorization { get }
    var accessibilityTrusted: Bool { get }

    func requestMicrophoneAccess(completion: @escaping (Bool) -> Void)
    @discardableResult
    func promptAccessibility() -> Bool
    @discardableResult
    func openSettings(for kind: PermissionKind) -> Bool
}

struct SystemPermissionPlatform: PermissionPlatform {
    var microphoneAuthorization: PermissionAuthorization {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .notDetermined:
            return .notDetermined
        case .authorized:
            return .authorized
        case .denied:
            return .denied
        case .restricted:
            return .restricted
        @unknown default:
            return .denied
        }
    }

    var accessibilityTrusted: Bool {
        // Use the non-prompting variant for every refresh. `AXIsProcessTrusted`
        // can keep returning a stale result while the user has just returned
        // from System Settings; asking ApplicationServices to evaluate the
        // current TCC record avoids treating the startup snapshot as truth.
        let options = [
            kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: false
        ]
        return AXIsProcessTrustedWithOptions(options as CFDictionary)
    }

    func requestMicrophoneAccess(completion: @escaping (Bool) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            completion(true)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async {
                    completion(granted)
                }
            }
        case .denied, .restricted:
            completion(false)
        @unknown default:
            completion(false)
        }
    }

    @discardableResult
    func promptAccessibility() -> Bool {
        let options = [
            kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true
        ]
        return AXIsProcessTrustedWithOptions(options as CFDictionary)
    }

    @discardableResult
    func openSettings(for kind: PermissionKind) -> Bool {
        NSWorkspace.shared.open(kind.settingsURL)
    }
}

/// Follow-up checks used after the app returns from System Settings. TCC can
/// publish the changed decision slightly after the activation notification, so
/// the coordinator does one immediate check and two bounded follow-ups.
enum PermissionRefreshPolicy {
    static let applicationActivationDelays: [UInt64] = [
        250_000_000,
        1_000_000_000,
    ]
}

/// Coordinates startup checks and user actions for the permission section of
/// the main window.
///
/// Instances are main-thread objects: the state and callback are deliberately
/// UI-friendly, while the only asynchronous operation (microphone TCC) is
/// marshalled back to the main queue by the production platform adapter.
@MainActor
final class PermissionCoordinator {
    private let platform: PermissionPlatform
    private var autoInsert: Bool
    private var activationRefreshTask: Task<Void, Never>?
    private var accessibilitySettingsWasOpened = false

    private(set) var state: PermissionState
    var onChange: ((PermissionState) -> Void)?

    deinit {
        activationRefreshTask?.cancel()
    }

    init(
        platform: PermissionPlatform = SystemPermissionPlatform(),
        autoInsert: Bool = true
    ) {
        self.platform = platform
        self.autoInsert = autoInsert
        self.state = PermissionPolicy.state(
            microphone: platform.microphoneAuthorization,
            accessibilityTrusted: platform.accessibilityTrusted,
            autoInsert: autoInsert
        )
    }

    /// Keep the startup workflow in sync with the user's auto-insert setting.
    /// Accessibility is only required when the app is expected to paste into
    /// another app automatically.
    func updateAutoInsertRequirement(_ enabled: Bool) {
        guard autoInsert != enabled else { return }
        autoInsert = enabled
        refresh()
    }

    /// Re-read both permissions. Call this when the main window becomes active
    /// again because the user may have changed a TCC switch in System Settings.
    func refresh() {
        state = PermissionPolicy.state(
            microphone: platform.microphoneAuthorization,
            accessibilityTrusted: platform.accessibilityTrusted,
            autoInsert: autoInsert
        )
        if state.accessibility.requirement != .required || state.accessibility.isSatisfied {
            // A successful re-check is the only authoritative signal that the
            // current process can use Accessibility. Do not keep a stale
            // restart warning after TCC has accepted the current app identity.
            accessibilitySettingsWasOpened = false
        }
        onChange?(state)
    }

    /// Refresh immediately when the app becomes active, then re-check after
    /// TCC has had time to publish a System Settings change. The current state
    /// remains authoritative throughout: if macOS still reports false, the UI
    /// must continue to show the permission as missing.
    func refreshAfterApplicationActivation() {
        activationRefreshTask?.cancel()
        refresh()
        activationRefreshTask = Task { @MainActor [weak self] in
            for delay in PermissionRefreshPolicy.applicationActivationDelays {
                do {
                    try await Task.sleep(nanoseconds: delay)
                } catch {
                    return
                }
                guard !Task.isCancelled else { return }
                self?.refresh()
            }
        }
    }

    /// Returns true once after the user has returned from the Accessibility
    /// pane while the current process is still untrusted. This is deliberately
    /// not a guessed "granted" state: callers should explain that a full app
    /// restart/re-adding the current build may be required, rather than hiding
    /// the missing permission.
    func consumeAccessibilityRestartHint() -> Bool {
        guard state.accessibility.requirement == .required,
              accessibilitySettingsWasOpened,
              !state.accessibility.isSatisfied
        else {
            return false
        }
        accessibilitySettingsWasOpened = false
        return true
    }

    /// Request the native microphone prompt when possible. Denied/restricted
    /// states are intentionally not sent through the prompt API; the UI should
    /// guide the user to System Settings instead.
    func requestMicrophoneAccess() {
        platform.requestMicrophoneAccess { [weak self] _ in
            Task { @MainActor in
                self?.refresh()
            }
        }
    }

    /// Ask macOS to show its Accessibility consent prompt. The user still has
    /// to enable TEA ASR in System Settings, so refresh immediately and again
    /// when the window becomes active.
    func promptAccessibility() {
        _ = platform.promptAccessibility()
        refresh()
    }

    /// Open the exact System Settings privacy pane for a permission item.
    /// Input Monitoring is safe to open for explanatory UI, but the current
    /// state marks it notRequired and no action button is advertised.
    @discardableResult
    func openSettings(for kind: PermissionKind) -> Bool {
        if kind == .accessibility {
            accessibilitySettingsWasOpened = true
        }
        return platform.openSettings(for: kind)
    }

    func performPrimaryAction(for kind: PermissionKind) {
        switch kind {
        case .microphone:
            if state.microphone.authorization == .notDetermined {
                requestMicrophoneAccess()
            } else {
                _ = openSettings(for: kind)
            }
        case .accessibility:
            promptAccessibility()
            _ = openSettings(for: kind)
        case .inputMonitoring:
            // This app does not use event taps. Keep the method exhaustive so
            // future permission rows can delegate to one action entry point.
            break
        }
    }
}
