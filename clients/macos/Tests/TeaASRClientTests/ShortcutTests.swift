import XCTest
@testable import TeaASRClient

final class ShortcutTests: XCTestCase {
    func testShortcutPersistsStableKeyCodeAndModifiers() throws {
        let suiteName = "TeaASRClientTests.Shortcut.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }

        let settings = Settings(defaults: defaults)
        let shortcut = try GlobalShortcut(keyCode: 14, modifiers: [.control, .shift])
        settings.shortcut = shortcut
        settings.interactionMode = .pushToTalk
        settings.startStopFeedback = true

        let reloaded = Settings(defaults: defaults)
        XCTAssertEqual(reloaded.shortcut, shortcut)
        XCTAssertEqual(reloaded.interactionMode, .pushToTalk)
        XCTAssertTrue(reloaded.startStopFeedback)
        XCTAssertEqual(reloaded.shortcut.displayString, "⌃⇧E")
    }

    func testInvalidShortcutRequiresModifierAndValidKeyCode() {
        XCTAssertThrowsError(try GlobalShortcut(keyCode: 2, modifiers: [])) { error in
            XCTAssertEqual(error as? GlobalShortcut.ValidationError, .modifierRequired)
        }
        XCTAssertThrowsError(try GlobalShortcut(keyCode: 128, modifiers: [.command])) { error in
            XCTAssertEqual(error as? GlobalShortcut.ValidationError, .keyCodeOutOfRange)
        }
        XCTAssertThrowsError(try GlobalShortcut(keyCode: 12, modifiers: [.command])) { error in
            XCTAssertEqual(error as? GlobalShortcut.ValidationError, .reservedCombination)
        }
    }

    func testDefaultIsStableAndDoesNotAppearAsFixedUIText() {
        XCTAssertEqual(GlobalShortcut.default.keyCode, 2)
        XCTAssertEqual(GlobalShortcut.default.displayString, "⌥⌘D")
        XCTAssertEqual(DictationInteractionMode.toggle.title, "切換模式（按一下開始／再按一下停止）")
    }

    func testLegacyOrCorruptDefaultsFallBackToSafeDefault() throws {
        let suiteName = "TeaASRClientTests.Shortcut.Corrupt.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        defaults.set(999, forKey: "dictationShortcutKeyCode")
        defaults.set(0, forKey: "dictationShortcutModifiers")
        XCTAssertEqual(Settings(defaults: defaults).shortcut, .default)
    }
}
