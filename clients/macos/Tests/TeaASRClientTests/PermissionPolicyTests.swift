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

    func testInputMonitoringBecomesRequiredForPushToTalk() {
        let missing = PermissionPolicy.state(
            microphone: .authorized,
            accessibilityTrusted: true,
            autoInsert: false,
            inputMonitoringAuthorized: false,
            requiresInputMonitoring: true
        )
        XCTAssertEqual(missing.inputMonitoring.requirement, .required)
        XCTAssertFalse(missing.requiredPermissionsGranted)
        XCTAssertEqual(missing.inputMonitoring.authorization, .denied)
        XCTAssertNotNil(missing.inputMonitoring.actionTitle)

        let granted = PermissionPolicy.state(
            microphone: .authorized,
            accessibilityTrusted: true,
            autoInsert: false,
            inputMonitoringAuthorized: true,
            requiresInputMonitoring: true
        )
        XCTAssertTrue(granted.requiredPermissionsGranted)
        XCTAssertTrue(granted.inputMonitoring.isSatisfied)
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

    func testActivationRefreshPolicyUsesImmediateAndBoundedFollowUps() {
        XCTAssertEqual(
            PermissionRefreshPolicy.applicationActivationDelays,
            [250_000_000, 1_000_000_000]
        )
    }

    @MainActor
    func testCoordinatorRechecksAccessibilityAfterApplicationActivation() {
        let platform = FakePermissionPlatform(
            microphoneAuthorization: .authorized,
            accessibilityTrusted: false
        )
        let coordinator = PermissionCoordinator(platform: platform)
        XCTAssertFalse(coordinator.state.accessibility.isSatisfied)

        platform.accessibilityTrusted = true
        coordinator.refreshAfterApplicationActivation()

        XCTAssertTrue(coordinator.state.accessibility.isSatisfied)
        XCTAssertFalse(coordinator.consumeAccessibilityRestartHint())
    }

    @MainActor
    func testCoordinatorOnlySuggestsRestartAfterAnUntrustedSettingsReturn() {
        let platform = FakePermissionPlatform(
            microphoneAuthorization: .authorized,
            accessibilityTrusted: false
        )
        let coordinator = PermissionCoordinator(platform: platform)

        XCTAssertTrue(coordinator.openSettings(for: .accessibility))
        coordinator.refreshAfterApplicationActivation()

        XCTAssertTrue(coordinator.consumeAccessibilityRestartHint())
        XCTAssertFalse(coordinator.consumeAccessibilityRestartHint())
    }
}

private final class FakePermissionPlatform: PermissionPlatform {
    var microphoneAuthorization: PermissionAuthorization
    var accessibilityTrusted: Bool
    var inputMonitoringAuthorized: Bool = false

    init(
        microphoneAuthorization: PermissionAuthorization,
        accessibilityTrusted: Bool
    ) {
        self.microphoneAuthorization = microphoneAuthorization
        self.accessibilityTrusted = accessibilityTrusted
    }

    func requestMicrophoneAccess(completion: @escaping (Bool) -> Void) {
        completion(microphoneAuthorization == .authorized)
    }

    func promptAccessibility() -> Bool {
        accessibilityTrusted
    }

    func promptInputMonitoring() -> Bool {
        inputMonitoringAuthorized
    }

    func openSettings(for kind: PermissionKind) -> Bool {
        true
    }
}
