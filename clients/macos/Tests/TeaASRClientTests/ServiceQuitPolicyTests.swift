import XCTest
@testable import TeaASRClient

/// "能讓程式結束後自動停止服務嗎?" — yes, but only for the process this app
/// launched and still holds a `ManagedProcess` handle to. A service started
/// by a LaunchAgent, from a terminal, or by the menu bar's fire-and-forget
/// `ServiceControl.start(executable:)` (which keeps no handle) must survive
/// the app's exit untouched, because nothing here ever matches a process by
/// name or by port.
final class ServiceQuitPolicyTests: XCTestCase {
    func testOnlyAManagedRunningProcessIsStoppedAndOnlyWhenTheUserAskedForIt() {
        XCTAssertEqual(
            ServiceQuitPolicy.action(
                stopOnQuit: true, hasManagedProcess: true, managedProcessIsRunning: true
            ),
            .stopManagedProcess
        )
        // The setting is off: the app's own process is left running too.
        XCTAssertEqual(
            ServiceQuitPolicy.action(
                stopOnQuit: false, hasManagedProcess: true, managedProcessIsRunning: true
            ),
            .leaveRunning
        )
        // A service that is reachable but was not launched here has no
        // handle, so there is nothing this app is willing to signal.
        XCTAssertEqual(
            ServiceQuitPolicy.action(
                stopOnQuit: true, hasManagedProcess: false, managedProcessIsRunning: false
            ),
            .leaveRunning
        )
        // A handle whose process already exited is not signalled again.
        XCTAssertEqual(
            ServiceQuitPolicy.action(
                stopOnQuit: true, hasManagedProcess: true, managedProcessIsRunning: false
            ),
            .leaveRunning
        )
    }

    func testSettingDefaultsToOnAndRoundTrips() throws {
        let suiteName = "TeaASRClientTests.StopServiceOnQuit.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let settings = Settings(defaults: defaults)
        XCTAssertTrue(settings.stopServiceOnQuit, "safe to default on: it is scoped to our own process")
        settings.stopServiceOnQuit = false
        XCTAssertFalse(Settings(defaults: defaults).stopServiceOnQuit)
    }

    /// The end-to-end shape of the quit path with two real processes: one
    /// this app "launched" (wrapped in a `ManagedProcess`) and one standing
    /// in for a LaunchAgent/terminal service it never launched. Applying the
    /// policy must kill exactly the first and leave the second alone.
    func testQuitStopsOnlyTheProcessThisAppLaunched() throws {
        let managed = ManagedProcess(process: longLivedProcess(), outputCapacity: 10)
        try managed.process.run()
        let foreign = longLivedProcess()
        try foreign.run()
        defer {
            if managed.isRunning { managed.terminate() }
            if foreign.isRunning { foreign.terminate() }
        }
        XCTAssertTrue(managed.isRunning)
        XCTAssertTrue(foreign.isRunning)

        // What `applicationWillTerminate` does, with the same inputs.
        let ours = ServiceQuitPolicy.action(
            stopOnQuit: true,
            hasManagedProcess: true,
            managedProcessIsRunning: managed.isRunning
        )
        if ours == .stopManagedProcess { managed.terminate() }

        // …and what it does about the process it has no handle for.
        let theirs = ServiceQuitPolicy.action(
            stopOnQuit: true,
            hasManagedProcess: false,
            managedProcessIsRunning: false
        )
        XCTAssertEqual(theirs, .leaveRunning)

        let deadline = Date().addingTimeInterval(3.0)
        while managed.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        XCTAssertFalse(managed.isRunning, "the process this app launched must be stopped")
        XCTAssertTrue(foreign.isRunning, "a process this app never launched must be untouched")
    }

    private func longLivedProcess() -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", "sleep 30"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        return process
    }
}
