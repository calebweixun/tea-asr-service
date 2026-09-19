import XCTest
@testable import TeaASRClient

final class AppLaunchPresentationTests: XCTestCase {
    func testLaunchShowsOverviewWhenRequiredPermissionsAreGranted() {
        XCTAssertEqual(
            MainWindowLaunchPolicy.section(requiredPermissionsGranted: true),
            .overview
        )
    }

    func testLaunchShowsPermissionsWhenRequiredPermissionsAreMissing() {
        XCTAssertEqual(
            MainWindowLaunchPolicy.section(requiredPermissionsGranted: false),
            .permissions
        )
    }
}
