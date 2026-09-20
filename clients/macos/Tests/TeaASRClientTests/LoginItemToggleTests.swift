import XCTest
@testable import TeaASRClient

/// `LoginItemToggle` backs the menu bar's "登入時自動開啟程式" item — whether
/// *this app* opens at login via `SMAppService.mainApp`. It is exercised
/// here against a fake `LoginItemService` rather than the real registry:
/// registering/unregistering a login item for real would mutate the test
/// machine's actual System Settings state, which no test in this suite is
/// allowed to do.
final class LoginItemToggleTests: XCTestCase {
    final class FakeLoginItemService: LoginItemService {
        var isRegistered: Bool
        var registerCallCount = 0
        var unregisterCallCount = 0
        var errorToThrow: Error?

        init(isRegistered: Bool) {
            self.isRegistered = isRegistered
        }

        struct Failure: Error {}

        func register() throws {
            registerCallCount += 1
            if let errorToThrow { throw errorToThrow }
            isRegistered = true
        }

        func unregister() throws {
            unregisterCallCount += 1
            if let errorToThrow { throw errorToThrow }
            isRegistered = false
        }
    }

    func testTogglingWhileUnregisteredRegistersIt() {
        let service = FakeLoginItemService(isRegistered: false)
        let result = LoginItemToggle.toggle(service)

        guard case .success = result else {
            XCTFail("expected success")
            return
        }
        XCTAssertTrue(service.isRegistered)
        XCTAssertEqual(service.registerCallCount, 1)
        XCTAssertEqual(service.unregisterCallCount, 0)
    }

    func testTogglingWhileRegisteredUnregistersIt() {
        let service = FakeLoginItemService(isRegistered: true)
        let result = LoginItemToggle.toggle(service)

        guard case .success = result else {
            XCTFail("expected success")
            return
        }
        XCTAssertFalse(service.isRegistered)
        XCTAssertEqual(service.unregisterCallCount, 1)
        XCTAssertEqual(service.registerCallCount, 0)
    }

    /// Toggling twice must always end up back where it started, regardless
    /// of which direction the first toggle went.
    func testTogglingTwiceRoundTrips() {
        let service = FakeLoginItemService(isRegistered: false)
        _ = LoginItemToggle.toggle(service)
        _ = LoginItemToggle.toggle(service)
        XCTAssertFalse(service.isRegistered)
    }

    /// A failure to register/unregister (e.g. the user removed the app from
    /// Login Items in System Settings mid-session) must be surfaced, not
    /// silently swallowed, and must not have flipped `isRegistered` under
    /// the hood since the fake only flips it on success.
    func testFailurePropagatesTheErrorAndLeavesStateUnchanged() {
        let service = FakeLoginItemService(isRegistered: false)
        service.errorToThrow = FakeLoginItemService.Failure()

        let result = LoginItemToggle.toggle(service)

        guard case .failure = result else {
            XCTFail("expected failure")
            return
        }
        XCTAssertFalse(service.isRegistered, "a failed register() must not have silently taken effect")
    }
}
