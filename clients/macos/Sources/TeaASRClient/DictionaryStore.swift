import Foundation

/// Everything the dictionary page knows, and every operation it can perform,
/// without any AppKit. The page renders this; tests drive it directly with a
/// synchronous fake `DictionaryServing`.
///
/// The one rule this type exists to keep: the server copy (`baseline` +
/// `revision`) and the user's working copy (`draft`) are separate, and the
/// draft is only ever replaced by an explicit choice — loading another
/// dictionary after the page asked, "還原", "重新載入" on a conflict, or a
/// successful save.
@MainActor
final class DictionaryStore {
    enum ListState: Equatable {
        case idle
        case loading
        case loaded
        case failed(DictionaryAPIError)
    }

    enum DocumentState: Equatable {
        case none
        case loading
        case loaded
        /// The file exists but the server cannot parse it (GET → 422).
        case invalid(String)
        case failed(DictionaryAPIError)
    }

    /// What changed, so the page can do the least work: a cell edit must not
    /// reload the table under the user's cursor.
    enum Change: Equatable {
        /// The dictionary list (or its loading/failure state).
        case list
        /// A different document (or a reloaded one) replaced the draft.
        case document
        /// Rows were added, removed or replaced.
        case rows
        /// A field or cell value changed in place.
        case draft
        /// Saving flag, notice or server errors.
        case status
    }

    enum ConflictChoice {
        case reload
        case overwrite
        case cancel
    }

    enum SaveOutcome: Equatable {
        case saved
        case notNeeded
        /// Local validation or a server 422 refused the content; the row and
        /// general errors say why.
        case rejected
        /// A 409 was resolved by throwing the draft away for the server copy.
        case reloaded
        case cancelled
        case failed(DictionaryAPIError)
    }

    private let client: DictionaryServing
    private let endpoint: () -> DictionaryEndpoint

    var limits: DictionaryLimits = .standard
    var onChange: ((Change) -> Void)?

    private(set) var summaries: [DictionarySummary] = []
    private(set) var listState: ListState = .idle
    private(set) var selectedName: String?
    private(set) var documentState: DocumentState = .none
    private(set) var baseline: DictionaryContent?
    private(set) var revision: DictionaryRevision?
    private(set) var draft = DictionaryDraft(content: .empty)
    private(set) var serverRowErrors: [UUID: String] = [:]
    private(set) var serverGeneralErrors: [String] = []
    private(set) var isSaving = false
    /// The last save's confirmation; cleared by the next edit.
    private(set) var notice: String?

    init(client: DictionaryServing, endpoint: @escaping () -> DictionaryEndpoint) {
        self.client = client
        self.endpoint = endpoint
    }

    var isDirty: Bool { draft.isDirty(comparedTo: baseline) }

    var validation: DictionaryValidation {
        DictionaryValidator.validate(draft, limits: limits, serverRowErrors: serverRowErrors)
    }

    var names: [String] { summaries.map(\.name) }

    var selectedSummary: DictionarySummary? {
        summaries.first { $0.name == selectedName }
    }

    // MARK: - List and selection

    /// Re-reads the list. Never touches the draft: if the selected dictionary
    /// is still listed it stays selected; if nothing was selected (first
    /// load, or after a delete) the first dictionary is opened.
    func reloadList(completion: (() -> Void)? = nil) {
        listState = .loading
        onChange?(.list)
        client.list(endpoint: endpoint()) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let items):
                self.summaries = items.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
                self.listState = .loaded
                if let selected = self.selectedName, !self.names.contains(selected), !self.isDirty {
                    self.clearSelection()
                }
                self.onChange?(.list)
                if self.selectedName == nil, let first = self.summaries.first {
                    self.open(first.name)
                }
            case .failure(let error):
                self.listState = .failed(error)
                self.onChange?(.list)
            }
            completion?()
        }
    }

    /// Opens `name`, replacing the draft. The page asks about unsaved
    /// changes before calling this; the store does not second-guess it.
    func open(_ name: String, completion: (() -> Void)? = nil) {
        selectedName = name
        documentState = .loading
        notice = nil
        onChange?(.list)
        client.get(name: name, endpoint: endpoint()) { [weak self] result in
            guard let self, self.selectedName == name else { return }
            switch result {
            case .success(let document):
                self.load(document)
            case .failure(.invalid(let message, let fields)):
                self.baseline = nil
                self.revision = nil
                self.draft = DictionaryDraft(content: .empty)
                let detail = fields.isEmpty ? message : fields.map(\.message).joined(separator: "\n")
                self.documentState = .invalid(detail.isEmpty ? "格式無法辨識。" : detail)
                self.onChange?(.document)
            case .failure(let error):
                self.baseline = nil
                self.revision = nil
                self.draft = DictionaryDraft(content: .empty)
                self.documentState = .failed(error)
                self.onChange?(.document)
            }
            completion?()
        }
    }

    private func clearSelection() {
        selectedName = nil
        documentState = .none
        baseline = nil
        revision = nil
        draft = DictionaryDraft(content: .empty)
        serverRowErrors = [:]
        serverGeneralErrors = []
        notice = nil
    }

    private func load(_ document: DictionaryDocument) {
        selectedName = document.name
        baseline = document.content
        revision = document.revision
        draft = DictionaryDraft(content: document.content)
        serverRowErrors = [:]
        serverGeneralErrors = []
        documentState = .loaded
        onChange?(.document)
    }

    // MARK: - Draft edits

    func discardChanges() {
        guard let baseline else { return }
        draft = DictionaryDraft(content: baseline)
        serverRowErrors = [:]
        serverGeneralErrors = []
        onChange?(.document)
    }

    func setDomain(_ domain: String) {
        guard draft.domain != domain else { return }
        draft.domain = domain
        edited(.draft)
    }

    func setHotwords(_ hotwords: [String]) {
        guard draft.hotwords != hotwords else { return }
        draft.hotwords = hotwords
        edited(.draft)
    }

    func updateRow(_ id: UUID, from: String? = nil, to: String? = nil) {
        guard let index = draft.rows.firstIndex(where: { $0.id == id }) else { return }
        var row = draft.rows[index]
        if let from { row.from = from }
        if let to { row.to = to }
        guard row != draft.rows[index] else { return }
        draft.rows[index] = row
        serverRowErrors[id] = nil
        edited(.draft)
    }

    @discardableResult
    func addRow() -> UUID {
        let row = ReplacementRow(from: "", to: "")
        draft.rows.append(row)
        edited(.rows)
        return row.id
    }

    func removeRows(_ ids: Set<UUID>) {
        guard !ids.isEmpty else { return }
        draft.rows.removeAll { ids.contains($0.id) }
        for id in ids { serverRowErrors[id] = nil }
        edited(.rows)
    }

    @discardableResult
    func append(_ rules: [ReplacementRule]) -> (added: [UUID], skipped: Int) {
        let result = draft.append(rules)
        if !result.added.isEmpty { edited(.rows) }
        return result
    }

    /// Import with "取代": everything on screen is replaced by the file's
    /// content, which then still has to be saved like any other edit.
    func replaceContent(_ content: DictionaryContent) {
        draft = DictionaryDraft(content: content)
        serverRowErrors = [:]
        edited(.rows)
    }

    private func edited(_ change: Change) {
        notice = nil
        serverGeneralErrors = []
        onChange?(change)
    }

    // MARK: - Saving

    /// Saves the draft with `base_revision`. On 409 `resolveConflict` decides:
    /// `.reload` replaces the draft with the server copy, `.overwrite` re-reads
    /// the current revision and saves the draft over it, `.cancel` leaves
    /// everything as it is so the user can export or copy their edits first.
    func save(
        resolveConflict: @escaping () -> ConflictChoice,
        completion: @escaping (SaveOutcome) -> Void
    ) {
        guard let name = selectedName, baseline != nil, !isSaving else {
            completion(.notNeeded)
            return
        }
        guard isDirty else {
            completion(.notNeeded)
            return
        }
        guard validation.isValid else {
            completion(.rejected)
            return
        }
        isSaving = true
        onChange?(.status)
        put(name: name, condition: revision.map { .ifRevision($0) } ?? .createOnly, resolveConflict: resolveConflict) { [weak self] outcome in
            guard let self else { return }
            self.isSaving = false
            self.onChange?(.status)
            completion(outcome)
        }
    }

    private func put(
        name: String,
        condition: DictionaryWriteCondition,
        resolveConflict: @escaping () -> ConflictChoice,
        completion: @escaping (SaveOutcome) -> Void
    ) {
        let sent = draft
        client.put(name: name, content: sent.content, condition: condition, endpoint: endpoint()) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let document):
                self.baseline = document.content
                self.revision = document.revision
                self.serverRowErrors = [:]
                self.serverGeneralErrors = []
                if self.draft.content != document.content {
                    // The server normalised something; show what it stored.
                    self.draft = DictionaryDraft(content: document.content)
                    self.onChange?(.document)
                }
                self.notice = DictionaryPresentation.savedNotice
                self.onChange?(.status)
                self.reloadList()
                completion(.saved)
            case .failure(.conflict(let currentRevision)):
                switch resolveConflict() {
                case .cancel:
                    completion(.cancelled)
                case .reload:
                    self.client.get(name: name, endpoint: self.endpoint()) { result in
                        switch result {
                        case .success(let document):
                            self.load(document)
                            self.reloadList()
                            completion(.reloaded)
                        case .failure(let error):
                            completion(.failed(error))
                        }
                    }
                case .overwrite:
                    // The draft stays; only the revision it is based on moves
                    // forward, which is what "overwrite" means. The 409 already
                    // says what the current revision is.
                    if let currentRevision {
                        self.revision = currentRevision
                        self.put(name: name, condition: .ifRevision(currentRevision), resolveConflict: resolveConflict, completion: completion)
                        return
                    }
                    // No revision in the 409: the file is gone (or the server
                    // did not say). Re-read to tell the two apart.
                    self.client.get(name: name, endpoint: self.endpoint()) { result in
                        switch result {
                        case .success(let current):
                            self.revision = current.revision
                            self.put(
                                name: name,
                                condition: current.revision.map { .ifRevision($0) } ?? .createOnly,
                                resolveConflict: resolveConflict,
                                completion: completion
                            )
                        case .failure(.notFound):
                            // Deleted elsewhere: overwriting means re-creating.
                            self.put(name: name, condition: .createOnly, resolveConflict: resolveConflict, completion: completion)
                        case .failure(let error):
                            completion(.failed(error))
                        }
                    }
                }
            case .failure(.invalid(let message, let fields)):
                self.applyServerErrors(message: message, fields: fields, rows: sent.rows)
                completion(.rejected)
            case .failure(let error):
                completion(.failed(error))
            }
        }
    }

    /// Attaches a 422's row-level errors to the rows that were sent (by
    /// position at the time of sending, then by stable id from then on), and
    /// keeps the rest as general messages.
    private func applyServerErrors(message: String, fields: [DictionaryFieldError], rows: [ReplacementRow]) {
        var rowErrors: [UUID: String] = [:]
        var general: [String] = []
        for field in fields {
            if field.isReplacementRow, let index = field.index, rows.indices.contains(index) {
                let id = rows[index].id
                rowErrors[id] = [rowErrors[id], field.localizedMessage].compactMap { $0 }.joined(separator: "；")
            } else {
                general.append(Self.describe(field))
            }
        }
        if fields.isEmpty {
            general.append(message.isEmpty ? "伺服器拒絕了這份字典內容。" : message)
        }
        serverRowErrors = rowErrors
        serverGeneralErrors = general
        onChange?(.status)
    }

    private static func describe(_ field: DictionaryFieldError) -> String {
        let label: String
        switch field.field.split(separator: ".").first.map(String.init) ?? field.field {
        case "domain": label = "情境說明"
        case "hotwords": label = "專有詞"
        case "replacements": label = "對照表"
        case "name": label = "名稱"
        case "file": label = "檔案"
        case "": label = ""
        default: label = field.field
        }
        var prefix = label
        if let index = field.index { prefix += "第 \(index + 1) 項" }
        return prefix.isEmpty ? field.localizedMessage : "\(prefix)：\(field.localizedMessage)"
    }

    // MARK: - Create, duplicate, rename, delete

    /// Creates `name` (a PUT with `"base_revision": null`, so an existing
    /// dictionary of that name is never overwritten — that answers 409) and
    /// opens it.
    func create(
        _ name: String,
        content: DictionaryContent,
        completion: @escaping (Result<Void, DictionaryAPIError>) -> Void
    ) {
        client.put(name: name, content: content, condition: .createOnly, endpoint: endpoint()) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let document):
                self.load(document)
                self.reloadList()
                completion(.success(()))
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    /// Copies the saved version of the selected dictionary.
    func duplicate(as newName: String, completion: @escaping (Result<Void, DictionaryAPIError>) -> Void) {
        guard let baseline else { return }
        create(newName, content: baseline, completion: completion)
    }

    /// There is no rename route: the saved content is written under the new
    /// name first, and only then is the old name deleted, so a failure at
    /// either step never loses the dictionary.
    func rename(to newName: String, completion: @escaping (Result<Void, DictionaryAPIError>) -> Void) {
        guard let oldName = selectedName, let baseline else { return }
        client.put(name: newName, content: baseline, condition: .createOnly, endpoint: endpoint()) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let document):
                self.client.delete(name: oldName, endpoint: self.endpoint()) { deleteResult in
                    self.load(document)
                    self.reloadList()
                    switch deleteResult {
                    case .success:
                        completion(.success(()))
                    case .failure(let error):
                        completion(.failure(error))
                    }
                }
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    func deleteSelected(completion: @escaping (Result<Void, DictionaryAPIError>) -> Void) {
        guard let name = selectedName else { return }
        client.delete(name: name, endpoint: endpoint()) { [weak self] result in
            guard let self else { return }
            if case .success = result {
                self.clearSelection()
                self.onChange?(.document)
                self.reloadList()
            }
            completion(result)
        }
    }

    // MARK: - Preview

    /// Runs `text` through the selected dictionary using the unsaved draft.
    func preview(_ text: String, completion: @escaping (Result<DictionaryPreviewResult, DictionaryAPIError>) -> Void) {
        guard let name = selectedName, documentState == .loaded else { return }
        client.preview(name: name, text: text, draft: draft.content, endpoint: endpoint(), completion: completion)
    }
}
