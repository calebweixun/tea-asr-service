import XCTest
@testable import TeaASRClient

final class AppLaunchPresentationTests: XCTestCase {
    func testLaunchShowsOverviewWhenRequiredPermissionsAreGranted() {
        XCTAssertEqual(
            MainWindowLaunchPolicy.section(requiredPermissionsGranted: true),
            .overview
        )
    }

    /// The permission checklist is a group inside Diagnostics now, so that is
    /// where a launch with a missing required permission must land.
    func testLaunchShowsDiagnosticsWhenRequiredPermissionsAreMissing() {
        XCTAssertEqual(
            MainWindowLaunchPolicy.section(requiredPermissionsGranted: false),
            .diagnostics
        )
    }
}
