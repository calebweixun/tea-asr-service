import XCTest
@testable import TeaASRClient

/// The 字典 page inside `MainWindowController`, with a fake server and
/// canned dialog answers: which state it shows, that it is built once, that
/// edits drive the Save button, that leaving with unsaved changes asks, bulk
/// paste, and the test area.
@MainActor
final class DictionaryPageTests: XCTestCase {
    // MARK: - Page state decision table

    func testPageStateDecisionTable() {
        let on = features(contextBiasing: true)
        let off = features(contextBiasing: false)
        typealias Page = DictionaryPage
        XCTAssertEqual(Page.pageState(listState: .loaded, hasDictionaries: true, service: .init(reachable: true, features: off)), .featureDisabled,
                       "a server that says dictionaries are off wins")
        guard case .unreachable = Page.pageState(listState: .failed(.unreachable("x")), hasDictionaries: false, service: .init(reachable: false, features: nil)) else {
            return XCTFail("expected unreachable")
        }
        guard case .unreachable = Page.pageState(listState: .idle, hasDictionaries: false, service: .init(reachable: false, features: nil)) else {
            return XCTFail("expected unreachable before the first fetch when the probe already failed")
        }
        guard case .failed = Page.pageState(listState: .failed(.unauthorized), hasDictionaries: false, service: .init(reachable: true, features: on)) else {
            return XCTFail("expected failed")
        }
        XCTAssertEqual(Page.pageState(listState: .loaded, hasDictionaries: false, service: .init(reachable: true, features: on)), .empty)
        XCTAssertEqual(Page.pageState(listState: .loaded, hasDictionaries: true, service: .init(reachable: true, features: on)), .ready)
        XCTAssertEqual(Page.pageState(listState: .loading, hasDictionaries: false, service: .init(reachable: nil, features: nil)), .loading)
    }

    // MARK: - Integration

    func testOpeningThePageListsAndOpensTheFirstDictionary() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)

        XCTAssertEqual(page.debugListTable.numberOfRows, 1)
        XCTAssertEqual(page.debugListTable.selectedRow, 0)
        XCTAssertTrue(page.debugIsEditorVisible)
        XCTAssertEqual(page.debugRulesTable.numberOfRows, 3)
        XCTAssertFalse(page.debugSaveButton.isEnabled, "nothing to save yet")
        XCTAssertTrue(page.debugRulesStatus.contains("共 3 條"))
    }

    func testPageIsBuiltOnceAndRefetchesOnReturn() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let first = controller.debugMountedSectionView
        controller.show(section: .overview)
        controller.show(section: .dictionaries)
        XCTAssertTrue(controller.debugMountedSectionView === first)
        XCTAssertEqual(server.log.filter { $0 == "list" }.count, 2)
    }

    func testStatusTicksDoNotReloadTheRulesTable() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        page.store.updateRow(page.store.draft.rows[0].id, to: "改")
        let before = server.log.count
        controller.refresh()
        controller.setStatus("tick")
        XCTAssertEqual(server.log.count, before, "a status tick must not hit the server")
        XCTAssertTrue(page.store.isDirty, "and must not throw away the draft")
    }

    func testEditingEnablesSaveAndSavingShowsTheOBSNotice() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)

        page.store.updateRow(page.store.draft.rows[0].id, to: "改過")
        XCTAssertTrue(page.debugSaveButton.isEnabled)
        XCTAssertTrue(page.debugEditorState.contains("尚未儲存"))
        XCTAssertTrue(controller.window?.isDocumentEdited ?? false, "the close button shows the unsaved dot")

        page.save(then: nil)

        XCTAssertFalse(page.debugSaveButton.isEnabled)
        XCTAssertEqual(page.debugEditorState, DictionaryPresentation.savedNotice)
        XCTAssertFalse(controller.window?.isDocumentEdited ?? true)
    }

    func testSwitchingDictionariesWithUnsavedChangesAsks() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(), "youth": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        page.store.updateRow(page.store.draft.rows[0].id, to: "改過")

        var asked: [String] = []
        page.askLeave = { name in asked.append(name); return .cancel }
        page.debugSelectDictionary(row: 1)
        XCTAssertEqual(asked, ["church"])
        XCTAssertEqual(page.store.selectedName, "church", "cancel stays put")
        XCTAssertTrue(page.store.isDirty)

        page.askLeave = { _ in .save }
        page.debugSelectDictionary(row: 1)
        XCTAssertEqual(server.dictionaries["church"]?.content.replacements.first?.to, "改過", "save runs first")
        XCTAssertEqual(page.store.selectedName, "youth", "then the switch happens")
        XCTAssertEqual(page.debugListTable.selectedRow, 1)
    }

    func testSwitchingAndDiscardingDropsTheEdits() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(), "youth": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        page.store.updateRow(page.store.draft.rows[0].id, to: "改過")
        page.askLeave = { _ in .discard }
        page.debugSelectDictionary(row: 1)
        XCTAssertEqual(page.store.selectedName, "youth")
        XCTAssertEqual(server.dictionaries["church"]?.content.replacements.first?.to, "對0", "discard never writes")
    }

    func testClosingTheWindowWithUnsavedChangesAsks() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        let window = try XCTUnwrap(controller.window)

        XCTAssertTrue(controller.windowShouldClose(window), "nothing to lose, close freely")

        page.store.updateRow(page.store.draft.rows[0].id, to: "改過")
        var asked = 0
        page.askLeave = { _ in asked += 1; return .cancel }
        XCTAssertFalse(controller.windowShouldClose(window))
        XCTAssertEqual(asked, 1)
        XCTAssertTrue(controller.hasUnsavedDictionaryChanges)
    }

    func testPastingLinesAddsRowsAndReportsSkippedOnes() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        page.readPasteboard = { "新錯 => 新對\n錯0 => 對0\n看不懂的一行\n表格錯\t表格對" }

        page.pasteRules(nil)

        XCTAssertEqual(page.store.draft.rows.count, 5)
        XCTAssertEqual(page.debugRulesTable.numberOfRows, 5)
        XCTAssertTrue(page.store.isDirty)
        XCTAssertTrue(page.debugRulesStatus.contains("已加入 2 條"))
        XCTAssertTrue(page.debugRulesStatus.contains("1 條已存在"))
        XCTAssertTrue(page.debugRulesStatus.contains("1 行看不懂"))
    }

    func testDuplicateFromIsFlaggedAndBlocksSaving() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        var messages: [String] = []
        page.showMessage = { title, _ in messages.append(title) }
        page.readPasteboard = { "錯0 => 別的" }
        page.pasteRules(nil)

        XCTAssertTrue(page.debugRulesStatus.contains("1 列需要修正"))
        page.save(then: nil)
        XCTAssertEqual(messages, ["還不能儲存"])
        XCTAssertTrue(server.puts.isEmpty)
    }

    func testSearchFiltersTheVisibleRowsOnly() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        page.debugSearch("錯1")
        XCTAssertEqual(page.debugRulesTable.numberOfRows, 1)
        XCTAssertEqual(page.store.draft.rows.count, 3)
        XCTAssertTrue(page.debugRulesStatus.contains("顯示 1 條"))
    }

    func testTestAreaUsesTheUnsavedDraft() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        page.store.updateRow(page.store.draft.rows[1].id, to: "草稿")

        page.debugRunPreview("今天錯1來了")

        XCTAssertEqual(page.debugTestResult, "今天草稿來了")
        XCTAssertEqual(page.debugTestApplied, "錯1 → 草稿")
        XCTAssertEqual(server.previews.last?.draft?.replacements[1].to, "草稿")
    }

    func testEmptyListOffersTheChurchTemplate() throws {
        let server = FakeDictionaryServer(dictionaries: [:])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        XCTAssertEqual(page.debugStateTitle, "還沒有字典")
        XCTAssertFalse(page.debugIsEditorVisible)

        let template = try XCTUnwrap(findButton(titled: "用範本建立 church", in: page.view))
        _ = template.target?.perform(template.action, with: template)

        XCTAssertEqual(server.dictionaries["church"]?.content, DictionaryTemplates.church)
        XCTAssertTrue(page.debugIsEditorVisible)
    }

    func testCapabilityOffExplainsHowToEnableIt() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let appState = AppState()
        appState.updateService(.success(snapshot(contextBiasing: false)))
        let controller = makeController(server, appState: appState)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        XCTAssertEqual(page.debugStateTitle, "伺服器尚未開啟字典功能")
        XCTAssertTrue(controller.debugLabelTexts().contains("context_hints_enabled = true"))
        XCTAssertFalse(page.debugIsEditorVisible)
    }

    func testUnreachableServiceSaysSo() throws {
        let server = FakeDictionaryServer(dictionaries: [:])
        server.listError = .unreachable("Connection refused")
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        XCTAssertEqual(page.debugStateTitle, "服務沒有執行")
        XCTAssertTrue(controller.debugLabelTexts().contains(where: { $0.contains("Connection refused") }))
    }

    func testDeleteAsksFirst() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(), "youth": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        var asked: [String] = []
        page.askDelete = { asked.append($0); return false }
        let delete = try XCTUnwrap(findButton(titled: "刪除…", in: page.view))
        _ = delete.target?.perform(delete.action, with: delete)
        XCTAssertEqual(asked, ["church"])
        XCTAssertNotNil(server.dictionaries["church"], "declining keeps the dictionary")

        page.askDelete = { _ in true }
        _ = delete.target?.perform(delete.action, with: delete)
        XCTAssertNil(server.dictionaries["church"])
        XCTAssertEqual(page.store.selectedName, "youth")
    }

    func testNewDictionaryUsesTheValidatedName() throws {
        let server = FakeDictionaryServer(dictionaries: ["church": sample()])
        let controller = makeController(server)
        controller.show(section: .dictionaries)
        let page = try XCTUnwrap(controller.debugDictionaryPage)
        var existingSeen: [String] = []
        page.askName = { _, _, _, existing in existingSeen = existing; return "youth_camp" }
        let new = try XCTUnwrap(findButton(titled: "新增…", in: page.view))
        _ = new.target?.perform(new.action, with: new)
        XCTAssertEqual(existingSeen, ["church"], "the prompt validates against existing names")
        XCTAssertEqual(server.dictionaries["youth_camp"]?.content, .empty)
        XCTAssertEqual(page.store.selectedName, "youth_camp")
    }

    // MARK: - Helpers

    private func makeController(_ server: FakeDictionaryServer, appState: AppState = AppState()) -> MainWindowController {
        let settings = Settings(defaults: UserDefaults(suiteName: "DictionaryPageTests-\(UUID().uuidString)")!)
        let permissions = PermissionCoordinator(
            platform: DictionaryPageTestPlatform(),
            autoInsert: true,
            requiresInputMonitoring: false
        )
        return MainWindowController(
            settings: settings,
            appState: appState,
            permissions: permissions,
            logsClient: FakeLogsClient(),
            dictionaryClient: server
        )
    }

    private func sample() -> DictionaryContent {
        DictionaryContent(
            domain: "主日",
            hotwords: ["禱告"],
            replacements: (0..<3).map { ReplacementRule(from: "錯\($0)", to: "對\($0)") }
        )
    }

    private func features(contextBiasing: Bool) -> CapabilityFeatures {
        CapabilityFeatures(
            nativeAudioStreaming: true, partialTranscripts: true, wordTimestamps: false, translation: false,
            diarization: false, hotwords: false, contextBiasing: contextBiasing, durableSessions: false,
            durableRevisable: false, batchJobs: false
        )
    }

    private func snapshot(contextBiasing: Bool) -> ServiceSnapshot {
        ServiceSnapshot(
            healthzOK: true,
            readyzOK: true,
            readyState: "ready",
            status: ServerStatus(
                modelState: "ready", model: "m", modelRevision: "r", workerGeneration: 1, workerLoadMs: nil,
                lastError: nil, idleS: 0, activeSessions: 0,
                queue: QueueStatus(waitingTasks: 0, waitingSamples: 0, maxWaitingTasks: 1, maxWaitingSamples: 1)
            ),
            capabilities: Capabilities(
                protocolVersion: "1",
                audio: CapabilityAudio(sampleRate: 16_000, channels: 1, format: "s16le"),
                profiles: [],
                features: features(contextBiasing: contextBiasing),
                limits: CapabilityLimits(maxFramePCMBytes: 1, maxUtteranceMs: 1, maxContinuousSessions: 1, maxTotalConnections: 1)
            )
        )
    }

    private func findButton(titled title: String, in root: NSView) -> NSButton? {
        if let button = root as? NSButton, button.title == title { return button }
        for subview in root.subviews {
            if let found = findButton(titled: title, in: subview) { return found }
        }
        return nil
    }
}

private struct DictionaryPageTestPlatform: PermissionPlatform {
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
