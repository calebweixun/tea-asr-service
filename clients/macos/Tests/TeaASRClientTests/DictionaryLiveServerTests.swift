import XCTest
@testable import TeaASRClient

/// End-to-end check of the 字典 page against a REAL tea-asr server: the
/// production `DictionaryPage` → `DictionaryStore` → `DictionaryClient` →
/// HTTP path, with only the modal dialogs answered by the test. Skipped
/// unless pointed at a disposable server:
///
///     TEA_ASR_LIVE_DICTIONARY_PORT=8452 \
///     TEA_ASR_LIVE_DICTIONARY_DIR=<isolated support>/dictionaries \
///     CFFIXED_USER_HOME=<isolated home with the same token> \
///     swift test --filter DictionaryLiveServerTests
///
/// It creates, edits, saves, conflicts, renames and deletes its own
/// dictionary, and checks the server's files (canonical TOML, `.history/`)
/// after each step. Never run it against the port the real service uses.
@MainActor
final class DictionaryLiveServerTests: XCTestCase {
    private var port = 0
    private var directory = URL(fileURLWithPath: "/nonexistent")
    private let dictName = "live_check"
    private let renamed = "live_check_renamed"

    override func setUpWithError() throws {
        let environment = ProcessInfo.processInfo.environment
        guard let rawPort = environment["TEA_ASR_LIVE_DICTIONARY_PORT"], let port = Int(rawPort),
              let rawDirectory = environment["TEA_ASR_LIVE_DICTIONARY_DIR"]
        else {
            throw XCTSkip("set TEA_ASR_LIVE_DICTIONARY_PORT and TEA_ASR_LIVE_DICTIONARY_DIR to run against a live server")
        }
        guard port != 8327 else { throw XCTSkip("refusing to touch the real service port") }
        self.port = port
        directory = URL(fileURLWithPath: rawDirectory)
        // Start from a clean slate for this test's own names only (the
        // directory belongs to a disposable server).
        for leftover in [dictName, renamed] {
            try? FileManager.default.removeItem(at: file(leftover))
            history(leftover).forEach { try? FileManager.default.removeItem(at: $0) }
        }
    }

    func testFullEditingRoundTripAgainstTheRealServer() throws {
        let controller = makeController()
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        let store = page.store
        var messages: [String] = []
        page.showMessage = { title, body in messages.append("\(title)：\(body)") }
        try waitUntil("list loads") { store.listState == .loaded }
        XCTAssertTrue(store.names.contains("church"))
        XCTAssertEqual(store.summaries.first { $0.name == "old_notes" }?.error, "invalid TOML or dictionary fields")
        XCTAssertNotNil(store.summaries.first { $0.name == "church" }?.updatedAt?.date, "real updated_at must parse")

        // 1. Create (base_revision: null) through the page's 新增… button.
        page.askLeave = { _ in .discard }
        page.askName = { [dictName] _, _, _, existing in
            XCTAssertFalse(existing.contains(dictName))
            return dictName
        }
        try press("新增…", in: page.view)
        try waitUntil("new dictionary opens") { store.selectedName == self.dictName && store.documentState == .loaded }
        XCTAssertTrue(FileManager.default.fileExists(atPath: file(dictName).path), "server wrote the new file")
        XCTAssertEqual(history(dictName).count, 0, "a create has nothing to back up")
        let created = store.revision

        // 1b. Creating the same name again is a 409, never an overwrite.
        var duplicateCreate: Result<Void, DictionaryAPIError>?
        store.create(dictName, content: DictionaryTemplates.church) { duplicateCreate = $0 }
        try waitUntil("duplicate create answers") { duplicateCreate != nil }
        guard case .failure(.conflict) = duplicateCreate else { return XCTFail("expected 409, got \(String(describing: duplicateCreate))") }
        XCTAssertEqual(store.revision, created)

        // 2. Edit rows (bulk paste) and the context fields.
        page.readPasteboard = { "李明哲執事 => 李銘哲執事\n恩點堂\t恩典堂\n嗯 => " }
        page.pasteRules(nil)
        store.setDomain("測試用聚會")
        store.setHotwords(["李銘哲"])
        XCTAssertTrue(store.isDirty)
        XCTAssertEqual(store.draft.rows.count, 3)

        // 3. Preview uses the unsaved draft (server applies longest match first).
        page.debugRunPreview("嗯，今天由李明哲執事在恩點堂分享")
        try waitUntil("preview answers") { !page.debugTestResult.isEmpty || page.debugTestApplied.hasPrefix("無法") }
        XCTAssertEqual(page.debugTestResult, "，今天由李銘哲執事在恩典堂分享")
        XCTAssertTrue(page.debugTestApplied.contains("李明哲執事 → 李銘哲執事"), page.debugTestApplied)

        // 4. Save with base_revision; canonical TOML lands on disk.
        page.save(then: nil)
        try waitUntil("save finishes") { !store.isSaving && !store.isDirty }
        XCTAssertEqual(page.debugEditorState, DictionaryPresentation.savedNotice)
        XCTAssertNotEqual(store.revision, created)
        let saved = try String(contentsOf: file(dictName), encoding: .utf8)
        XCTAssertEqual(try DictionaryTOML.parse(saved), store.draft.content, "the canonical TOML on disk is what the app shows")
        XCTAssertTrue(saved.contains("[[replacements]]"))
        XCTAssertEqual(history(dictName).count, 1, "the previous version was copied to .history")
        XCTAssertEqual(store.revision, sha256Hex(of: file(dictName)), "revision is the file's SHA-256")

        // 5. Reload: what the server returns equals what was saved.
        let savedContent = store.draft.content
        var reopened = false
        store.open(dictName) { reopened = true }
        try waitUntil("reopen") { reopened }
        XCTAssertEqual(store.draft.content, savedContent)
        XCTAssertFalse(store.isDirty)

        // 6a. 409 → overwrite: the file changes behind the app's back.
        try writeBehindTheAppsBack(to: "外部修改一")
        store.updateRow(store.draft.rows[0].id, to: "App 的修改")
        var conflicts = 0
        page.askConflict = { _ in conflicts += 1; return .overwrite }
        page.save(then: nil)
        try waitUntil("overwrite finishes") { !store.isSaving && !store.isDirty }
        XCTAssertEqual(conflicts, 1)
        XCTAssertEqual(try DictionaryTOML.parse(String(contentsOf: file(dictName), encoding: .utf8)).replacements.first?.to, "App 的修改")
        XCTAssertTrue(history(dictName).contains { (try? String(contentsOf: $0, encoding: .utf8))?.contains("外部修改一") ?? false },
                      "the overwritten outside edit is kept in .history")

        // 6b. 409 → reload: the outside edit wins and the draft is replaced.
        try writeBehindTheAppsBack(to: "外部修改二")
        store.updateRow(store.draft.rows[0].id, to: "會被放棄")
        page.askConflict = { _ in .reload }
        page.save(then: nil)
        try waitUntil("reload finishes") { !store.isSaving && !store.isDirty }
        XCTAssertEqual(store.draft.rows.first?.to, "外部修改二")

        // 7. 422 duplicate: the page refuses before sending; the server
        // refuses too when asked directly, with a row-addressed detail.
        let before = try String(contentsOf: file(dictName), encoding: .utf8)
        page.readPasteboard = { "李明哲執事 => 別的寫法" }
        page.pasteRules(nil)
        messages.removeAll()
        page.save(then: nil)
        XCTAssertEqual(messages.first?.hasPrefix("還不能儲存"), true)
        XCTAssertEqual(try String(contentsOf: file(dictName), encoding: .utf8), before, "nothing was written")
        var direct: Result<DictionaryDocument, DictionaryAPIError>?
        DictionaryClient().put(name: dictName, content: store.draft.content, condition: .ifRevision(store.revision ?? ""), endpoint: endpoint()) { direct = $0 }
        try waitUntil("direct 422") { direct != nil }
        guard case .failure(.invalid(_, let fields)) = direct else { return XCTFail("expected 422, got \(String(describing: direct))") }
        XCTAssertEqual(fields.first?.field, "replacements.from")
        XCTAssertEqual(fields.first?.index, store.draft.rows.count - 1)
        XCTAssertEqual(fields.first?.localizedMessage, "與第 1 列重複")
        store.discardChanges()

        // 8. Rename: new name written first, old one moved into .history.
        page.askName = { [renamed] _, _, _, _ in renamed }
        try press("重新命名…", in: page.view)
        try waitUntil("rename finishes") { store.selectedName == self.renamed && store.names.contains(self.renamed) }
        XCTAssertFalse(FileManager.default.fileExists(atPath: file(dictName).path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: file(renamed).path))
        XCTAssertFalse(store.names.contains(dictName))

        // 9. Delete: soft delete into .history, list moves on.
        page.askDelete = { _ in true }
        try press("刪除…", in: page.view)
        try waitUntil("delete finishes") { !store.names.contains(self.renamed) && store.listState == .loaded }
        XCTAssertFalse(FileManager.default.fileExists(atPath: file(renamed).path))
        XCTAssertEqual(history(renamed).count, 1, "the deleted dictionary can be recovered from .history")
        XCTAssertNotEqual(store.selectedName, renamed)
        XCTAssertTrue(messages.isEmpty || messages == ["還不能儲存：對照表有 1 列需要修正（看「問題」欄）。"], "\(messages)")
    }

    // MARK: - Helpers

    private func endpoint() -> DictionaryEndpoint {
        DictionaryEndpoint(host: "127.0.0.1", port: port, token: try? Settings(defaults: .standard).token())
    }

    private func makeController() -> MainWindowController {
        let settings = Settings(defaults: UserDefaults(suiteName: "DictionaryLiveServerTests-\(UUID().uuidString)")!)
        settings.host = "127.0.0.1"
        settings.port = port
        let permissions = PermissionCoordinator(platform: LiveTestPlatform(), autoInsert: true, requiresInputMonitoring: false)
        return MainWindowController(settings: settings, appState: AppState(), permissions: permissions, logsClient: FakeLogsClient())
    }

    private func file(_ dictionary: String) -> URL {
        directory.appendingPathComponent("\(dictionary).toml")
    }

    private func history(_ dictionary: String) -> [URL] {
        let entries = (try? FileManager.default.contentsOfDirectory(
            at: directory.appendingPathComponent(".history"), includingPropertiesForKeys: nil
        )) ?? []
        return entries.filter {
            let base = $0.deletingPathExtension().lastPathComponent
            // `<name>-<UTC stamp>`; the stamp starts with a digit, which keeps
            // `live_check` from matching `live_check_renamed-…`.
            return base.hasPrefix(dictionary + "-") && base.dropFirst(dictionary.count + 1).first?.isNumber == true
        }
    }

    private func writeBehindTheAppsBack(to value: String) throws {
        var content = try DictionaryTOML.parse(String(contentsOf: file(dictName), encoding: .utf8))
        content.replacements[0].to = value
        try DictionaryTOML.render(content).write(to: file(dictName), atomically: true, encoding: .utf8)
    }

    private func sha256Hex(of url: URL) -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/shasum")
        process.arguments = ["-a", "256", url.path]
        let pipe = Pipe()
        process.standardOutput = pipe
        try? process.run()
        process.waitUntilExit()
        let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return String(output.prefix(64))
    }

    private func press(_ title: String, in root: NSView) throws {
        func find(_ view: NSView) -> NSButton? {
            if let button = view as? NSButton, button.title == title { return button }
            for subview in view.subviews { if let found = find(subview) { return found } }
            return nil
        }
        let button = try XCTUnwrap(find(root), "no button 「\(title)」")
        XCTAssertTrue(button.isEnabled, "「\(title)」 is disabled")
        _ = button.target?.perform(button.action, with: button)
    }

    private func waitUntil(_ what: String, timeout: TimeInterval = 5, _ condition: () -> Bool) throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else {
                XCTFail("timed out waiting for: \(what)")
                throw XCTSkip("stopping after timeout")
            }
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
    }
}

private struct LiveTestPlatform: PermissionPlatform {
    var microphoneAuthorization: PermissionAuthorization { .authorized }
    var accessibilityTrusted: Bool { true }
    var inputMonitoringAuthorized: Bool { true }
    func requestMicrophoneAccess(completion: @escaping (Bool) -> Void) { completion(true) }
    @discardableResult
    func promptAccessibility() -> Bool { true }
    @discardableResult
    func promptInputMonitoring() -> Bool { true }
    @discardableResult
    func openSettings(for kind: PermissionKind) -> Bool { true }
}
