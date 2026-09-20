import Foundation

/// The one place that knows what macOS currently *calls* a privacy pane.
///
/// macOS renamed the Accessibility privacy category to
/// 「裝置控制和資料取用」 (Device Control and Data Access). The underlying
/// TCC service, the API (`AXIsProcessTrusted`) and the System Settings
/// anchor are unchanged — only the user-facing name moved — so this is
/// purely a string concern and belongs nowhere near the policy model.
///
/// The decision is made from the *runtime* OS version on purpose. A compile
/// time `if #available` would bake whichever SDK happened to build the app
/// into the binary, so a build made on an older SDK would keep showing the
/// old name on a new OS (and vice versa). Users read the name off System
/// Settings, which is the running OS, not the SDK.
enum SystemPermissionNaming {
    /// First macOS major version that uses the new name. macOS 27 is
    /// confirmed on-device; 26 is assumed to have introduced it (see the
    /// report's unverified list) — if that turns out to be wrong, this one
    /// constant is the only thing that has to move.
    static let firstDeviceControlNamingMajorVersion = 26

    static var currentMajorVersion: Int {
        ProcessInfo.processInfo.operatingSystemVersion.majorVersion
    }

    /// Pure form, so tests can pin a version instead of the host's.
    static func accessibilityTitle(majorVersion: Int) -> String {
        majorVersion >= firstDeviceControlNamingMajorVersion
            ? "裝置控制和資料取用"
            : "輔助使用"
    }

    /// What the current OS calls the Accessibility privacy category.
    static var accessibilityTitle: String {
        accessibilityTitle(majorVersion: currentMajorVersion)
    }

    /// The System Settings path to quote in prose, e.g. in an alert.
    static var accessibilitySettingsPath: String {
        "系統設定 → 隱私權與安全性 → \(accessibilityTitle)"
    }
}

/// Permissions that can affect the macOS client.
///
/// Input Monitoring is intentionally part of the model even though TEA ASR
/// does not install an event tap. Keeping it explicit lets the main window
/// explain that the permission is not required instead of presenting a
/// misleading incomplete checklist.
enum PermissionKind: String, CaseIterable, Identifiable {
    case microphone
    case accessibility
    case inputMonitoring

    var id: String { rawValue }

    var title: String {
        switch self {
        case .microphone:
            return "麥克風"
        case .accessibility:
            return SystemPermissionNaming.accessibilityTitle
        case .inputMonitoring:
            return "輸入監控"
        }
    }

    var requirement: PermissionRequirement {
        switch self {
        case .microphone:
            return .required
        case .accessibility:
            // The app can still copy final text to the clipboard without this
            // permission. It is needed only for automatic insertion.
            return .optional
        case .inputMonitoring:
            return .notRequired
        }
    }

    var explanation: String {
        switch self {
        case .microphone:
            return "聽寫與會議記錄需要讀取麥克風。"
        case .accessibility:
            return "只有自動貼上到前景 app 時需要；剪貼簿模式不需要。"
        case .inputMonitoring:
            return "按住說話模式需要接收其他 app 的按鍵放開事件。"
        }
    }

    /// The System Settings pane associated with this permission.
    ///
    /// These URLs are intentionally kept in the pure model so a UI can render
    /// an action without duplicating platform strings. Opening the URL remains
    /// a coordinator/platform concern and is never performed by policy tests.
    var settingsURL: URL {
        let privacy: String
        switch self {
        case .microphone:
            privacy = "Privacy_Microphone"
        case .accessibility:
            privacy = "Privacy_Accessibility"
        case .inputMonitoring:
            privacy = "Privacy_ListenEvent"
        }
        return URL(
            string: "x-apple.systempreferences:com.apple.preference.security?\(privacy)"
        )!
    }

    var actionTitle: String? {
        switch self {
        case .microphone:
            return "允許麥克風"
        case .accessibility:
            return "開啟\(SystemPermissionNaming.accessibilityTitle)設定"
        case .inputMonitoring:
            return nil
        }
    }
}

enum PermissionRequirement: Equatable {
    case required
    case optional
    case notRequired
}

/// Normalised authorization values used by both the platform adapter and the
/// UI model. The coordinator maps AVFoundation/ApplicationServices values into
/// this small platform-independent set.
enum PermissionAuthorization: Equatable {
    case notDetermined
    case authorized
    case denied
    case restricted
    case notRequired
}

struct PermissionItemState: Equatable, Identifiable {
    let kind: PermissionKind
    let requirement: PermissionRequirement
    let authorization: PermissionAuthorization

    var id: PermissionKind { kind }
    var title: String { kind.title }
    var explanation: String {
        if kind == .inputMonitoring, requirement == .notRequired {
            return "目前使用切換模式，不需要輸入監控權限。"
        }
        return kind.explanation
    }
    var settingsURL: URL { kind.settingsURL }
    var actionTitle: String? {
        if kind == .inputMonitoring, requirement == .required {
            return "開啟輸入監控設定"
        }
        return kind.actionTitle
    }

    var isSatisfied: Bool {
        authorization == .authorized || authorization == .notRequired
    }

    var needsAttention: Bool {
        switch requirement {
        case .required, .optional:
            return !isSatisfied
        case .notRequired:
            return false
        }
    }
}

/// Single source of truth for the permission section of the future main
/// window. It is a value type on purpose: a view can bind to one snapshot and
/// compare it safely without knowing about AVFoundation or ApplicationServices.
struct PermissionState: Equatable {
    let microphone: PermissionItemState
    let accessibility: PermissionItemState
    let inputMonitoring: PermissionItemState

    var items: [PermissionItemState] {
        [microphone, accessibility, inputMonitoring]
    }

    var requiredPermissionsGranted: Bool {
        items
            .filter { $0.requirement == .required }
            .allSatisfy(\.isSatisfied)
    }

    var optionalPermissionsNeedAttention: Bool {
        items.contains { $0.requirement == .optional && $0.needsAttention }
    }

    var allRequiredAndOptionalPermissionsGranted: Bool {
        items
            .filter { $0.requirement != .notRequired }
            .allSatisfy(\.isSatisfied)
    }

    func item(for kind: PermissionKind) -> PermissionItemState {
        switch kind {
        case .microphone:
            return microphone
        case .accessibility:
            return accessibility
        case .inputMonitoring:
            return inputMonitoring
        }
    }

    static func make(
        microphone: PermissionAuthorization,
        accessibilityTrusted: Bool,
        autoInsert: Bool = true,
        inputMonitoringAuthorized: Bool = false,
        requiresInputMonitoring: Bool = false
    ) -> PermissionState {
        PermissionState(
            microphone: PermissionItemState(
                kind: .microphone,
                requirement: PermissionKind.microphone.requirement,
                authorization: microphone
            ),
            accessibility: PermissionItemState(
                kind: .accessibility,
                // Automatic insertion is the normal workflow (and is enabled
                // by default), so Accessibility becomes a startup requirement
                // whenever that workflow is enabled. Clipboard-only mode can
                // still run without it.
                requirement: autoInsert ? .required : .optional,
                authorization: accessibilityTrusted ? .authorized : .denied
            ),
            inputMonitoring: PermissionItemState(
                kind: .inputMonitoring,
                requirement: requiresInputMonitoring ? .required : .notRequired,
                authorization: requiresInputMonitoring
                    ? (inputMonitoringAuthorized ? .authorized : .denied)
                    : .notRequired
            )
        )
    }
}

/// Pure policy entry point used by tests and future UI code. It contains no
/// calls to TCC, System Settings, or user-facing prompts.
enum PermissionPolicy {
    static func state(
        microphone: PermissionAuthorization,
        accessibilityTrusted: Bool,
        autoInsert: Bool = true,
        inputMonitoringAuthorized: Bool = false,
        requiresInputMonitoring: Bool = false
    ) -> PermissionState {
        PermissionState.make(
            microphone: microphone,
            accessibilityTrusted: accessibilityTrusted,
            autoInsert: autoInsert,
            inputMonitoringAuthorized: inputMonitoringAuthorized,
            requiresInputMonitoring: requiresInputMonitoring
        )
    }
}
