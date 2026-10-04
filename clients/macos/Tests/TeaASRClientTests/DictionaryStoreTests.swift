import XCTest
@testable import TeaASRClient

/// The dictionary editor's logic without AppKit: list loading and selection,
/// dirty state, saving with `base_revision`, every 409 resolution, 422 row
/// mapping, and create/duplicate/rename/delete — all against a synchronous
/// in-memory fake server.
@MainActor
final class DictionaryStoreTests: XCTestCase {
    func testFirstListLoadOpensTheFirstDictionary() {
        let server = FakeDictionaryServer(dictionaries: ["youth": sample(rules: 1), "church": sample(rules: 2)])
        let store = makeStore(server)

        store.reloadList()

        XCTAssertEqual(store.listState, .loaded)
        XCTAssertEqual(store.names, ["church", "youth"], "the list is sorted by name")
        XCTAssertEqual(store.selectedName, "church")
        XCTAssertEqual(store.documentState, .loaded)
        XCTAssertEqual(store.draft.rows.count, 2)
        XCTAssertFalse(store.isDirty)
    }

    func testEditingMakesTheStoreDirtyAndRevertCleansIt() {
        let store = loadedStore()
        let id = store.draft.rows[0].id
        store.updateRow(id, to: "改過")
        XCTAssertTrue(store.isDirty)
        store.discardChanges()
        XCTAssertFalse(store.isDirty)
        XCTAssertEqual(store.draft.rows[0].to, "對0")
    }

    func testSaveSendsBaseRevisionAndShowsTheOBSNotice() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        let before = store.revision
        store.updateRow(store.draft.rows[0].id, to: "新")

        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { XCTFail("no conflict expected"); return .cancel }) { outcome = $0 }

        XCTAssertEqual(outcome, .saved)
        XCTAssertEqual(server.puts.last?.condition, before.map { .ifRevision($0) })
        XCTAssertNotEqual(store.revision, before, "the store must adopt the new revision for the next save")
        XCTAssertFalse(store.isDirty)
        XCTAssertEqual(store.notice, DictionaryPresentation.savedNotice)
        XCTAssertEqual(server.dictionaries["church"]?.content.replacements.first?.to, "新")
    }

    func testSaveIsANoOpWhenNothingChanged() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { .cancel }) { outcome = $0 }
        XCTAssertEqual(outcome, .notNeeded)
        XCTAssertTrue(server.puts.isEmpty)
    }

    func testLocallyInvalidDraftIsNeverSent() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        store.addRow() // empty 聽錯的字
        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { .cancel }) { outcome = $0 }
        XCTAssertEqual(outcome, .rejected)
        XCTAssertTrue(server.puts.isEmpty)
    }

    // MARK: - 409

    func testConflictReloadReplacesTheDraftWithTheServerCopy() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        store.updateRow(store.draft.rows[0].id, to: "我的修改")
        server.editElsewhere("church", to: "別人的修改")

        var asked = 0
        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { asked += 1; return .reload }) { outcome = $0 }

        XCTAssertEqual(asked, 1)
        XCTAssertEqual(outcome, .reloaded)
        XCTAssertEqual(store.draft.rows[0].to, "別人的修改")
        XCTAssertEqual(store.revision, server.dictionaries["church"]?.revision)
        XCTAssertFalse(store.isDirty)
    }

    func testConflictOverwriteRereadsTheRevisionAndSavesTheDraft() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        let stale = store.revision
        store.updateRow(store.draft.rows[0].id, to: "我的修改")
        server.editElsewhere("church", to: "別人的修改")
        let current = server.dictionaries["church"]?.revision

        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { .overwrite }) { outcome = $0 }

        XCTAssertEqual(outcome, .saved)
        XCTAssertEqual(server.puts.map(\.condition), [.ifRevision(stale!), .ifRevision(current!)],
                       "first attempt with the stale revision, retry with the 409's current_revision")
        XCTAssertEqual(server.log.filter { $0 == "get church" }.count, 1,
                       "current_revision makes the extra GET unnecessary")
        XCTAssertEqual(server.dictionaries["church"]?.content.replacements.first?.to, "我的修改")
        XCTAssertFalse(store.isDirty)
    }

    func testConflictWithoutCurrentRevisionFallsBackToAGet() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        server.omitCurrentRevision = true
        let store = makeStore(server)
        store.reloadList()
        store.updateRow(store.draft.rows[0].id, to: "我的修改")
        server.editElsewhere("church", to: "別人的修改")

        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { .overwrite }) { outcome = $0 }

        XCTAssertEqual(outcome, .saved)
        XCTAssertEqual(server.log.filter { $0 == "get church" }.count, 2, "re-read to learn the current revision")
        XCTAssertEqual(server.dictionaries["church"]?.content.replacements.first?.to, "我的修改")
    }

    func testOverwriteAfterDeletionElsewhereRecreates() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        store.updateRow(store.draft.rows[0].id, to: "我的修改")
        server.dictionaries["church"] = nil

        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { .overwrite }) { outcome = $0 }

        XCTAssertEqual(outcome, .saved)
        XCTAssertEqual(server.puts.last?.condition, .createOnly)
        XCTAssertNotNil(server.dictionaries["church"])
    }

    func testCreateNeverOverwritesAnExistingName() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        var result: Result<Void, DictionaryAPIError>?
        store.create("church", content: .empty) { result = $0 }
        guard case .failure(.conflict) = result else { return XCTFail("expected 409, got \(String(describing: result))") }
        XCTAssertEqual(server.dictionaries["church"]?.content.replacements.count, 1)
    }

    func testConflictCancelKeepsTheDraftAndTheOldRevision() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        let stale = store.revision
        store.updateRow(store.draft.rows[0].id, to: "我的修改")
        server.editElsewhere("church", to: "別人的修改")

        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { .cancel }) { outcome = $0 }

        XCTAssertEqual(outcome, .cancelled)
        XCTAssertTrue(store.isDirty)
        XCTAssertEqual(store.draft.rows[0].to, "我的修改")
        XCTAssertEqual(store.revision, stale)
        XCTAssertFalse(store.isSaving)
    }

    // MARK: - 422

    func testServerRowErrorsLandOnTheRowsThatWereSent() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 3)])
        let store = makeStore(server)
        store.reloadList()
        store.updateRow(store.draft.rows[0].id, to: "改")
        server.nextPutError = .invalid(message: "invalid", fields: [
            DictionaryFieldError(field: "replacements.to", index: 2, message: "伺服器：這列不行"),
            DictionaryFieldError(field: "hotwords", index: 0, message: "太長"),
        ])
        let flagged = store.draft.rows[2].id

        var outcome: DictionaryStore.SaveOutcome?
        store.save(resolveConflict: { .cancel }) { outcome = $0 }

        XCTAssertEqual(outcome, .rejected)
        XCTAssertEqual(store.validation.message(for: flagged), "伺服器：這列不行")
        XCTAssertEqual(store.serverGeneralErrors, ["專有詞第 1 項：太長"])
        XCTAssertTrue(store.isDirty)

        store.updateRow(flagged, from: "修好了")
        XCTAssertNil(store.validation.message(for: flagged), "editing a row clears the server's complaint about it")
    }

    // MARK: - Create, duplicate, rename, delete

    func testCreateOpensTheNewDictionary() {
        let server = FakeDictionaryServer(dictionaries: [:])
        let store = makeStore(server)
        store.reloadList()
        XCTAssertNil(store.selectedName)

        var result: Result<Void, DictionaryAPIError>?
        store.create("church", content: DictionaryTemplates.church) { result = $0 }

        XCTAssertNoThrow(try result?.get())
        XCTAssertEqual(server.puts.last?.condition, .createOnly, "creating sends base_revision: null")
        XCTAssertEqual(store.selectedName, "church")
        XCTAssertEqual(store.names, ["church"])
        XCTAssertEqual(store.draft.content, DictionaryTemplates.church)
    }

    func testDuplicateCopiesTheSavedVersion() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 2)])
        let store = makeStore(server)
        store.reloadList()
        store.updateRow(store.draft.rows[0].id, to: "未儲存")
        store.discardChanges()
        store.duplicate(as: "church-copy") { _ in }
        XCTAssertEqual(server.dictionaries["church-copy"]?.content, server.dictionaries["church"]?.content)
        XCTAssertEqual(store.selectedName, "church-copy")
    }

    func testRenameWritesTheNewNameBeforeDeletingTheOld() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 2)])
        let store = makeStore(server)
        store.reloadList()

        var result: Result<Void, DictionaryAPIError>?
        store.rename(to: "sunday") { result = $0 }

        XCTAssertNoThrow(try result?.get())
        XCTAssertEqual(server.log, ["list", "get church", "put sunday", "delete church", "list"])
        XCTAssertEqual(store.names, ["sunday"])
        XCTAssertEqual(store.selectedName, "sunday")
    }

    func testDeleteClearsTheEditorAndOpensWhatIsLeft() {
        let server = FakeDictionaryServer(dictionaries: ["church": sample(rules: 1), "youth": sample(rules: 1)])
        let store = makeStore(server)
        store.reloadList()
        store.deleteSelected { _ in }
        XCTAssertEqual(store.names, ["youth"])
        XCTAssertEqual(store.selectedName, "youth")
    }

    func testInvalidFileIsShownAsInvalidNotAsAnEmptyDictionary() {
        let server = FakeDictionaryServer(dictionaries: [:])
        server.invalid = ["broken": "invalid TOML or dictionary fields"]
        let store = makeStore(server)
        store.reloadList()
        XCTAssertEqual(store.summaries.first?.error, "invalid TOML or dictionary fields")
        XCTAssertEqual(store.documentState, .invalid("invalid TOML or dictionary fields"))
        XCTAssertNil(store.baseline)
        XCTAssertFalse(store.isDirty)
    }

    func testListFailureKeepsTheReasonForThePage() {
        let server = FakeDictionaryServer(dictionaries: [:])
        server.listError = .unreachable("Connection refused")
        let store = makeStore(server)
        store.reloadList()
        XCTAssertEqual(store.listState, .failed(.unreachable("Connection refused")))
    }

    // MARK: - Helpers

    private func makeStore(_ server: FakeDictionaryServer) -> DictionaryStore {
        DictionaryStore(client: server, endpoint: { DictionaryEndpoint(host: "127.0.0.1", port: 8452, token: "t") })
    }

    private func loadedStore() -> DictionaryStore {
        let store = makeStore(FakeDictionaryServer(dictionaries: ["church": sample(rules: 2)]))
        store.reloadList()
        return store
    }

    private func sample(rules: Int) -> DictionaryContent {
        DictionaryContent(
            domain: "主日",
            hotwords: ["禱告"],
            replacements: (0..<rules).map { ReplacementRule(from: "錯\($0)", to: "對\($0)") }
        )
    }
}

/// An in-memory `/v1/dictionaries` that answers synchronously and enforces
/// `base_revision` the way the real server does (409 when it is stale).
final class FakeDictionaryServer: DictionaryServing {
    struct Stored {
        var content: DictionaryContent
        var revision: DictionaryRevision
    }

    struct Put {
        let name: String
        let content: DictionaryContent
        let condition: DictionaryWriteCondition
    }

    var dictionaries: [String: Stored] = [:]
    var invalid: [String: String] = [:]
    var listError: DictionaryAPIError?
    var nextPutError: DictionaryAPIError?
    /// Answer 409 without `current_revision`, to exercise the GET fallback.
    var omitCurrentRevision = false
    var previewResult: Result<DictionaryPreviewResult, DictionaryAPIError>?
    private(set) var puts: [Put] = []
    private(set) var previews: [(text: String, draft: DictionaryContent?)] = []
    private(set) var log: [String] = []
    private var counter = 0

    init(dictionaries: [String: DictionaryContent]) {
        for (name, content) in dictionaries {
            self.dictionaries[name] = Stored(content: content, revision: nextRevision())
        }
    }

    private func nextRevision() -> DictionaryRevision {
        counter += 1
        return "r\(counter)"
    }

    func editElsewhere(_ name: String, to value: String) {
        guard var stored = dictionaries[name] else { return }
        stored.content.replacements[0].to = value
        stored.revision = nextRevision()
        dictionaries[name] = stored
    }

    private func document(_ name: String) -> DictionaryDocument? {
        dictionaries[name].map { DictionaryDocument(name: name, content: $0.content, revision: $0.revision) }
    }

    func list(endpoint: DictionaryEndpoint, completion: @escaping (Result<[DictionarySummary], DictionaryAPIError>) -> Void) {
        log.append("list")
        if let listError { return completion(.failure(listError)) }
        var items = dictionaries.map { name, stored in
            DictionarySummary(
                name: name,
                domain: stored.content.domain,
                hotwordsCount: stored.content.hotwords.count,
                replacementsCount: stored.content.replacements.count,
                revision: stored.revision
            )
        }
        items += invalid.map { DictionarySummary(name: $0.key, error: $0.value) }
        completion(.success(items))
    }

    func get(name: String, endpoint: DictionaryEndpoint, completion: @escaping (Result<DictionaryDocument, DictionaryAPIError>) -> Void) {
        log.append("get \(name)")
        if let reason = invalid[name] { return completion(.failure(.invalid(message: reason, fields: []))) }
        guard let document = document(name) else { return completion(.failure(.notFound)) }
        completion(.success(document))
    }

    func put(
        name: String,
        content: DictionaryContent,
        condition: DictionaryWriteCondition,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<DictionaryDocument, DictionaryAPIError>) -> Void
    ) {
        log.append("put \(name)")
        puts.append(Put(name: name, content: content, condition: condition))
        if let error = nextPutError {
            nextPutError = nil
            return completion(.failure(error))
        }
        // The real server: base_revision must equal the current revision,
        // which is null when the file does not exist.
        let current = dictionaries[name]?.revision
        let expected: DictionaryRevision?
        switch condition {
        case .createOnly: expected = nil
        case .ifRevision(let revision): expected = revision
        }
        guard current == expected else {
            return completion(.failure(.conflict(currentRevision: omitCurrentRevision ? nil : current)))
        }
        dictionaries[name] = Stored(content: content, revision: nextRevision())
        completion(.success(document(name)!))
    }

    func delete(name: String, endpoint: DictionaryEndpoint, completion: @escaping (Result<Void, DictionaryAPIError>) -> Void) {
        log.append("delete \(name)")
        guard dictionaries.removeValue(forKey: name) != nil || invalid.removeValue(forKey: name) != nil else {
            return completion(.failure(.notFound))
        }
        completion(.success(()))
    }

    func preview(
        name: String,
        text: String,
        draft: DictionaryContent?,
        endpoint: DictionaryEndpoint,
        completion: @escaping (Result<DictionaryPreviewResult, DictionaryAPIError>) -> Void
    ) {
        previews.append((text, draft))
        if let previewResult { return completion(previewResult) }
        var output = text
        var applied: [DictionaryPreviewResult.Applied] = []
        for rule in draft?.replacements ?? [] {
            let count = output.components(separatedBy: rule.from).count - 1
            guard count > 0 else { continue }
            output = output.replacingOccurrences(of: rule.from, with: rule.to)
            applied.append(.init(from: rule.from, to: rule.to, count: count))
        }
        completion(.success(DictionaryPreviewResult(text: output, applied: applied)))
    }
}
