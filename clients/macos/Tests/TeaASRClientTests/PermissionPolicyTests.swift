import XCTest
@testable import TeaASRClient

final class PermissionPolicyTests: XCTestCase {
    func testAuthorizedMicrophoneAndAccessibilityAreReady() {
        let state = PermissionPolicy.state(
            microphone: .authorized,
            accessibilityTrusted: true,
            autoInsert: true
        )

        XCTAssertEqual(state.microphone.authorization, .authorized)
        XCTAssertEqual(state.accessibility.authorization, .authorized)
        XCTAssertTrue(state.requiredPermissionsGranted)
        XCTAssertFalse(state.optionalPermissionsNeedAttention)
        XCTAssertTrue(state.allRequiredAndOptionalPermissionsGranted)
    }

    func testDeniedMicrophoneBlocksRequiredPermissions() {
        let state = PermissionPolicy.state(
            microphone: .denied,
            accessibilityTrusted: false,
            autoInsert: true
        )

        XCTAssertFalse(state.requiredPermissionsGranted)
        XCTAssertTrue(state.microphone.needsAttention)
        XCTAssertTrue(state.accessibility.needsAttention)
        XCTAssertFalse(state.optionalPermissionsNeedAttention)
        XCTAssertFalse(state.allRequiredAndOptionalPermissionsGranted)
    }

    func testInputMonitoringIsExplicitlyNotRequired() {
        let state = PermissionPolicy.state(
            microphone: .authorized,
            accessibilityTrusted: false,
            autoInsert: false
        )

        XCTAssertEqual(state.inputMonitoring.requirement, .notRequired)
        XCTAssertEqual(state.inputMonitoring.authorization, .notRequired)
        XCTAssertFalse(state.inputMonitoring.needsAttention)
        XCTAssertTrue(state.requiredPermissionsGranted)
        XCTAssertTrue(state.item(for: .inputMonitoring).explanation.contains("不需要"))
    }

    func testPermissionSettingsURLsAreStableAndSpecific() {
        XCTAssertEqual(
            PermissionKind.microphone.settingsURL.absoluteString,
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
        )
        XCTAssertEqual(
            PermissionKind.accessibility.settingsURL.absoluteString,
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        )
        XCTAssertEqual(
            PermissionKind.inputMonitoring.settingsURL.absoluteString,
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
        )
    }

    func testNotDeterminedMicrophoneRemainsActionableWithoutPromptingPolicy() {
        let state = PermissionPolicy.state(
            microphone: .notDetermined,
            accessibilityTrusted: true,
            autoInsert: false
        )

        XCTAssertEqual(state.microphone.authorization, .notDetermined)
        XCTAssertTrue(state.microphone.needsAttention)
        XCTAssertFalse(state.requiredPermissionsGranted)
        XCTAssertFalse(state.optionalPermissionsNeedAttention)
    }

    func testAccessibilityIsRequiredWhenAutoInsertIsEnabled() {
        let state = PermissionPolicy.state(
            microphone: .authorized,
            accessibilityTrusted: false,
            autoInsert: true
        )

        XCTAssertEqual(state.accessibility.requirement, .required)
        XCTAssertTrue(state.accessibility.needsAttention)
        XCTAssertFalse(state.requiredPermissionsGranted)
        XCTAssertFalse(state.optionalPermissionsNeedAttention)
    }

    func testAccessibilityIsOptionalWhenAutoInsertIsDisabled() {
        let state = PermissionPolicy.state(
            microphone: .authorized,
            accessibilityTrusted: false,
            autoInsert: false
        )

        XCTAssertEqual(state.accessibility.requirement, .optional)
        XCTAssertTrue(state.requiredPermissionsGranted)
        XCTAssertTrue(state.optionalPermissionsNeedAttention)
    }
}
