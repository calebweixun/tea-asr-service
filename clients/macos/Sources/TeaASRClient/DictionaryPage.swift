import AppKit
import UniformTypeIdentifiers

/// The 對照表's table: adds the editing commands the rest of the app gets
/// from text fields — paste adds rows (bulk paste), copy copies the selected
/// rows as `錯字 => 正字` lines, and Delete removes the selected rows.
final class ReplacementTableView: NSTableView {
    var onPaste: (() -> Void)?
    var onCopy: (() -> Void)?
    var onDelete: (() -> Void)?

    @objc func paste(_ sender: Any?) { onPaste?() }
    @objc func copy(_ sender: Any?) { onCopy?() }
    @objc func delete(_ sender: Any?) { onDelete?() }

    override func keyDown(with event: NSEvent) {
        if let scalar = event.charactersIgnoringModifiers?.unicodeScalars.first,
           [0x7F, 0x08, UInt32(NSDeleteFunctionKey)].contains(scalar.value),
           selectedRowIndexes.count > 0 {
            onDelete?()
            return
        }
        super.keyDown(with: event)
    }
}

/// The page root. This app is a menu-bar accessory with no main menu, so the
/// standard ⌘X/⌘C/⌘V/⌘A/⌘Z key equivalents have no Edit menu to travel
/// through. A page whose whole job is editing text needs them, so they are
/// routed to the first responder here — scoped to this page only.
private final class EditingShortcutsView: NSView {
    override var isFlipped: Bool { true }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard flags.contains(.command), !flags.contains(.control), !flags.contains(.option),
              let key = event.charactersIgnoringModifiers?.lowercased()
        else { return super.performKeyEquivalent(with: event) }
        let shift = flags.contains(.shift)
        let action: Selector?
        switch key {
        case "x": action = #selector(NSText.cut(_:))
        case "c": action = #selector(NSText.copy(_:))
        case "v": action = #selector(NSText.paste(_:))
        case "a": action = #selector(NSResponder.selectAll(_:))
        case "z": action = shift ? Selector(("redo:")) : Selector(("undo:"))
        default: action = nil
        }
        if let action, NSApp.sendAction(action, to: nil, from: self) {
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

/// The 字典 page: pick a server dictionary, edit its replacement table, try
/// it on a sentence, and save it back. All state and every server operation
/// live in `DictionaryStore`; this class only renders it and asks the user
/// the questions (unsaved changes, conflicts, names) the store needs answered.
///
/// Built once, like every other section. `update()` — called on every status
/// tick — only touches labels, enabled state and visibility. The tables are
/// reloaded only when the store reports a structural change, so a cell or a
/// field being edited is never reloaded out from under the cursor.
@MainActor
final class DictionaryPage: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate, NSTokenFieldDelegate, NSSearchFieldDelegate {
    typealias Metrics = MainWindowController.Metrics

    struct ServiceState {
        let reachable: Bool?
        let features: CapabilityFeatures?
    }

    enum PageState: Equatable {
        case loading
        case unreachable(String)
        case featureDisabled
        case failed(String)
        case empty
        case ready
    }

    enum LeaveChoice {
        case save
        case discard
        case cancel
    }

    enum ImportChoice {
        case replace
        case append
        case cancel
    }

    /// Which page to show. Pure, so the decision table is unit tested:
    /// a server that says dictionaries are off wins over everything; then a
    /// failed list explains itself (unreachable is its own state); then an
    /// empty list offers the template.
    static func pageState(listState: DictionaryStore.ListState, hasDictionaries: Bool, service: ServiceState) -> PageState {
        if service.features?.contextBiasing == false {
            return .featureDisabled
        }
        switch listState {
        case .failed(let error):
            if error.isUnreachable { return .unreachable(error.localizedDescription) }
            return .failed(error.localizedDescription)
        case .loaded:
            return hasDictionaries ? .ready : .empty
        case .idle, .loading:
            if hasDictionaries { return .ready }
            if service.reachable == false {
                return .unreachable(DictionaryAPIError.unreachable("").localizedDescription)
            }
            return .loading
        }
    }

    static let contextFieldsExplanation = "目前不會送給模型：伺服器的提示詞功能（context_prompt_enabled）是關閉的，"
        + "所以這一欄只是先保留，留給之後使用。真正會改變辨識結果的是上方的對照表。"

    let store: DictionaryStore
    private(set) var view: NSView = NSView()
    private unowned let host: MainWindowController
    private let service: () -> ServiceState

    // MARK: Dialog seams (replaced by tests)

    var askLeave: (_ name: String) -> LeaveChoice = DictionaryPage.runLeaveAlert
    var askConflict: (_ name: String) -> DictionaryStore.ConflictChoice = DictionaryPage.runConflictAlert
    var askName: (_ title: String, _ message: String, _ initial: String, _ existing: [String]) -> String? = DictionaryPage.runNamePrompt
    var askDelete: (_ name: String) -> Bool = DictionaryPage.runDeleteAlert
    var askImport: () -> ImportChoice = DictionaryPage.runImportAlert
    var showMessage: (_ title: String, _ message: String) -> Void = DictionaryPage.runMessage
    var readPasteboard: () -> String? = { NSPasteboard.general.string(forType: .string) }

    // MARK: Views

    private let stateIcon = NSImageView(image: NSImage())
    private let stateTitle = NSTextField(labelWithString: "")
    private let stateDetail = NSTextField(wrappingLabelWithString: "")
    private let stateCode = NSTextField(labelWithString: "")
    private let stateRefresh = NSButton()
    private let stateTemplate = NSButton()
    private let stateNewBlank = NSButton()
    private var stateGroup = NSView()

    private let listTable = NSTableView()
    private let newButton = NSButton()
    private let duplicateButton = NSButton()
    private let renameButton = NSButton()
    private let deleteButton = NSButton()
    private let refreshButton = NSButton()
    private var listGroup = NSView()

    private let editorName = NSTextField(labelWithString: "")
    private let editorState = NSTextField(labelWithString: "")
    private let revertButton = NSButton()
    private let saveButton = NSButton()
    private let editorErrors = NSTextField(wrappingLabelWithString: "")
    private var editorBar = NSView()

    private let documentMessage = NSTextField(wrappingLabelWithString: "")
    private var documentGroup = NSView()

    private let searchField = NSSearchField()
    private let rulesTable = ReplacementTableView()
    private let addRemove = NSSegmentedControl()
    private let rulesStatus = NSTextField(labelWithString: "")
    private var rulesGroup = NSView()

    private let testField = NSTextField()
    private let testResult = NSTextField(wrappingLabelWithString: "")
    private let testApplied = NSTextField(wrappingLabelWithString: "")
    private var testGroup = NSView()

    private let domainField = NSTextField()
    private let hotwordsField = NSTokenField()
    private var contextGroup = NSView()

    /// Draft row indices in display order (after search and sort).
    private var visibleRows: [Int] = []
    /// One-off message under the table (paste/import results).
    private var rulesNotice: String?
    /// Set while the page itself changes the list selection, so the
    /// unsaved-changes guard only runs for the user's own clicks.
    private var isSyncingListSelection = false
    private var previewGeneration = 0
    private var lastKnownReachable: Bool?

    private enum Column {
        static let name = NSUserInterfaceItemIdentifier("name")
        static let hotwords = NSUserInterfaceItemIdentifier("hotwords")
        static let rules = NSUserInterfaceItemIdentifier("rules")
        static let updated = NSUserInterfaceItemIdentifier("updated")
        static let index = NSUserInterfaceItemIdentifier("index")
        static let from = NSUserInterfaceItemIdentifier("from")
        static let arrow = NSUserInterfaceItemIdentifier("arrow")
        static let to = NSUserInterfaceItemIdentifier("to")
        static let issue = NSUserInterfaceItemIdentifier("issue")
    }

    init(
        host: MainWindowController,
        client: DictionaryServing,
        endpoint: @escaping () -> DictionaryEndpoint,
        service: @escaping () -> ServiceState
    ) {
        self.host = host
        self.service = service
        self.store = DictionaryStore(client: client, endpoint: endpoint)
        super.init()
        if let limits = service().features?.contextLimits {
            store.limits = limits
        }
        build()
        store.onChange = { [weak self] change in
            self?.storeDidChange(change)
        }
        update()
    }

    // MARK: - Building

    private func build() {
        stateGroup = buildStateGroup()
        listGroup = buildListGroup()
        editorBar = buildEditorBar()
        documentGroup = buildDocumentGroup()
        rulesGroup = buildRulesGroup()
        testGroup = buildTestGroup()
        contextGroup = buildContextGroup()

        // The replacement table comes first: it is what actually changes the
        // captions. The context fields are kept for later and say so.
        let stack = host.sectionStack(views: [
            stateGroup,
            listGroup,
            editorBar,
            documentGroup,
            rulesGroup,
            testGroup,
            contextGroup,
        ])
        let root = EditingShortcutsView()
        root.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
        view = root
    }

    private func button(_ title: String, _ action: Selector, identifier: String? = nil) -> NSButton {
        button(title, action, identifier: identifier, into: NSButton())
    }

    private func button(_ title: String, _ action: Selector, identifier: String? = nil, into button: NSButton) -> NSButton {
        button.title = title
        button.target = self
        button.action = action
        button.bezelStyle = .rounded
        button.setButtonType(.momentaryPushIn)
        button.setContentHuggingPriority(.required, for: .horizontal)
        if let identifier { button.identifier = NSUserInterfaceItemIdentifier(identifier) }
        return button
    }

    private func secondaryLabel(_ field: NSTextField, size: CGFloat = 12) {
        field.font = .systemFont(ofSize: size)
        field.textColor = .secondaryLabelColor
    }

    /// The empty/error states share one quiet layout: a small symbol and a
    /// title, one paragraph of explanation, and the one or two actions that
    /// get the user out of that state.
    private func buildStateGroup() -> NSView {
        stateIcon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        stateIcon.setContentHuggingPriority(.required, for: .horizontal)
        stateTitle.font = .systemFont(ofSize: 15, weight: .semibold)
        let titleRow = NSStackView(views: [stateIcon, stateTitle])
        titleRow.orientation = .horizontal
        titleRow.alignment = .centerY
        titleRow.spacing = Metrics.tight
        secondaryLabel(stateDetail, size: 13)
        stateCode.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        stateCode.isSelectable = true
        stateCode.textColor = .labelColor
        _ = button("重新整理", #selector(refreshList(_:)), identifier: "dictionaryStateRefresh", into: stateRefresh)
        _ = button("用範本建立 church", #selector(createFromTemplate(_:)), identifier: "dictionaryTemplate", into: stateTemplate)
        _ = button("新增空白字典…", #selector(newDictionary(_:)), into: stateNewBlank)
        let actions = host.buttonRow([stateTemplate, stateNewBlank, stateRefresh])
        let stack = NSStackView(views: [titleRow, stateDetail, stateCode, actions])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = Metrics.tight
        stack.setCustomSpacing(Metrics.row, after: stateCode)
        host.stretchArrangedSubviewsToFullWidth(stack)
        return stack
    }

    private func buildListGroup() -> NSView {
        listTable.identifier = NSUserInterfaceItemIdentifier("dictionaryList")
        let columns: [(NSUserInterfaceItemIdentifier, String, CGFloat, CGFloat)] = [
            (Column.name, "名稱", 200, 120),
            (Column.hotwords, "專有詞", 70, 60),
            (Column.rules, "對照規則", 80, 60),
            (Column.updated, "最後更新", 140, 100),
        ]
        for (identifier, title, width, minWidth) in columns {
            let column = NSTableColumn(identifier: identifier)
            column.title = title
            column.width = width
            column.minWidth = minWidth
            if identifier == Column.hotwords || identifier == Column.rules {
                column.headerCell.alignment = .right
            }
            listTable.addTableColumn(column)
        }
        listTable.style = .fullWidth
        listTable.columnAutoresizingStyle = .firstColumnOnlyAutoresizingStyle
        listTable.allowsMultipleSelection = false
        listTable.allowsEmptySelection = true
        listTable.usesAlternatingRowBackgroundColors = false
        listTable.dataSource = self
        listTable.delegate = self
        let scroll = NSScrollView()
        scroll.documentView = listTable
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .bezelBorder
        scroll.heightAnchor.constraint(equalToConstant: 150).isActive = true

        _ = button("新增…", #selector(newDictionary(_:)), identifier: "dictionaryNew", into: newButton)
        _ = button("複製…", #selector(duplicateDictionary(_:)), identifier: "dictionaryDuplicate", into: duplicateButton)
        _ = button("重新命名…", #selector(renameDictionary(_:)), identifier: "dictionaryRename", into: renameButton)
        _ = button("刪除…", #selector(deleteDictionary(_:)), identifier: "dictionaryDelete", into: deleteButton)
        _ = button("重新整理", #selector(refreshList(_:)), identifier: "dictionaryRefresh", into: refreshButton)
        let actions = NSStackView()
        actions.orientation = .horizontal
        actions.spacing = Metrics.row
        actions.setViews([newButton, duplicateButton, renameButton, deleteButton], in: .leading)
        actions.setViews([refreshButton], in: .trailing)
        return host.group(title: "字典", views: [scroll, actions])
    }

    /// The dictionary being edited, whether it has unsaved changes, and the
    /// two actions that resolve that. ⌘S saves.
    private func buildEditorBar() -> NSView {
        editorName.font = .systemFont(ofSize: 13, weight: .semibold)
        editorName.lineBreakMode = .byTruncatingTail
        editorName.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        editorState.font = .systemFont(ofSize: 12)
        editorState.lineBreakMode = .byTruncatingTail
        editorState.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        _ = button("還原", #selector(revertChanges(_:)), identifier: "dictionaryRevert", into: revertButton)
        _ = button("儲存", #selector(saveChanges(_:)), identifier: "dictionarySave", into: saveButton)
        saveButton.keyEquivalent = "s"
        saveButton.keyEquivalentModifierMask = .command
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = Metrics.tight
        row.setViews([editorName, editorState], in: .leading)
        row.setViews([revertButton, saveButton], in: .trailing)
        row.setCustomSpacing(Metrics.row, after: revertButton)
        editorErrors.font = .systemFont(ofSize: 12)
        editorErrors.textColor = .systemRed
        let separator = NSBox()
        separator.boxType = .separator
        let stack = NSStackView(views: [row, separator, editorErrors])
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = Metrics.tight
        host.stretchArrangedSubviewsToFullWidth(stack)
        return stack
    }

    /// Shown instead of the editor when the selected file cannot be read.
    private func buildDocumentGroup() -> NSView {
        secondaryLabel(documentMessage, size: 13)
        documentMessage.isSelectable = true
        return host.group(views: [documentMessage])
    }

    private func buildRulesGroup() -> NSView {
        let hint = NSTextField(wrappingLabelWithString:
            "辨識結果中出現左欄的字，就換成右欄的字。只比對完全相同的字；同一處有多條符合時，較長的優先。"
            + "可以直接貼上多行「錯字 => 正字」，或從試算表複製的兩欄。")
        secondaryLabel(hint)

        searchField.placeholderString = "搜尋對照表"
        searchField.identifier = NSUserInterfaceItemIdentifier("dictionarySearch")
        searchField.delegate = self
        searchField.sendsSearchStringImmediately = true
        searchField.widthAnchor.constraint(equalToConstant: 220).isActive = true
        let paste = button("從剪貼簿貼上", #selector(pasteRules(_:)), identifier: "dictionaryPaste")
        let importButton = button("匯入…", #selector(importTOML(_:)))
        let exportButton = button("匯出…", #selector(exportTOML(_:)))
        let toolbar = NSStackView()
        toolbar.orientation = .horizontal
        toolbar.alignment = .centerY
        toolbar.spacing = Metrics.row
        toolbar.setViews([searchField], in: .leading)
        toolbar.setViews([paste, importButton, exportButton], in: .trailing)

        rulesTable.identifier = NSUserInterfaceItemIdentifier("dictionaryRules")
        let columns: [(NSUserInterfaceItemIdentifier, String, CGFloat, ReplacementRowOrdering.Key?)] = [
            (Column.index, "#", 40, .index),
            (Column.from, "聽錯的字", 180, .from),
            (Column.arrow, "", 22, nil),
            (Column.to, "正確的字", 180, .to),
            (Column.issue, "問題", 160, nil),
        ]
        for (identifier, title, width, sortKey) in columns {
            let column = NSTableColumn(identifier: identifier)
            column.title = title
            column.width = width
            if identifier == Column.index || identifier == Column.arrow {
                column.minWidth = width
                column.maxWidth = width
                column.resizingMask = []
                column.headerCell.alignment = identifier == Column.index ? .right : .center
            } else {
                column.minWidth = 90
            }
            if let sortKey {
                column.sortDescriptorPrototype = NSSortDescriptor(key: sortKey.rawValue, ascending: true)
            }
            rulesTable.addTableColumn(column)
        }
        rulesTable.style = .fullWidth
        rulesTable.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        rulesTable.allowsMultipleSelection = true
        rulesTable.usesAlternatingRowBackgroundColors = true
        rulesTable.dataSource = self
        rulesTable.delegate = self
        rulesTable.onPaste = { [weak self] in self?.pasteRules(nil) }
        rulesTable.onCopy = { [weak self] in self?.copySelectedRules() }
        rulesTable.onDelete = { [weak self] in self?.removeSelectedRules() }
        let scroll = NSScrollView()
        scroll.documentView = rulesTable
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .bezelBorder
        scroll.heightAnchor.constraint(equalToConstant: 300).isActive = true

        // The standard list-editing control: + and − under the table.
        addRemove.segmentStyle = .smallSquare
        addRemove.trackingMode = .momentary
        addRemove.segmentCount = 2
        addRemove.setImage(NSImage(systemSymbolName: "plus", accessibilityDescription: "新增一列"), forSegment: 0)
        addRemove.setImage(NSImage(systemSymbolName: "minus", accessibilityDescription: "刪除選取的列"), forSegment: 1)
        addRemove.setWidth(28, forSegment: 0)
        addRemove.setWidth(28, forSegment: 1)
        addRemove.setToolTip("新增一列", forSegment: 0)
        addRemove.setToolTip("刪除選取的列", forSegment: 1)
        addRemove.target = self
        addRemove.action = #selector(addRemoveClicked(_:))
        addRemove.identifier = NSUserInterfaceItemIdentifier("dictionaryAddRemove")
        rulesStatus.font = .systemFont(ofSize: 12)
        rulesStatus.lineBreakMode = .byTruncatingTail
        rulesStatus.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let footer = NSStackView()
        footer.orientation = .horizontal
        footer.alignment = .centerY
        footer.spacing = Metrics.row
        footer.setViews([addRemove, rulesStatus], in: .leading)

        let group = host.group(title: "對照表", views: [hint, toolbar, scroll, footer])
        if let stack = group as? NSStackView, let content = stack.arrangedSubviews.last as? NSStackView {
            // Attach the +/− bar to the table, the way Finder and System
            // Settings lists do.
            content.setCustomSpacing(0, after: scroll)
            content.setCustomSpacing(Metrics.tight, after: toolbar)
        }
        return group
    }

    private func buildTestGroup() -> NSView {
        testField.placeholderString = "輸入一句話，看看套用對照表後會變成什麼"
        testField.identifier = NSUserInterfaceItemIdentifier("dictionaryTest")
        testField.delegate = self
        testResult.font = .systemFont(ofSize: 13)
        testResult.isSelectable = true
        secondaryLabel(testApplied)
        testApplied.isSelectable = true
        let note = NSTextField(wrappingLabelWithString: "用的是畫面上尚未儲存的內容，不會影響伺服器上的字典。")
        secondaryLabel(note)
        return host.group(title: "測試", views: [
            labeledRow("句子", testField),
            labeledRow("結果", testResult),
            labeledRow("套用的規則", testApplied),
            labeledRow("", note),
        ])
    }

    private func buildContextGroup() -> NSView {
        domainField.placeholderString = "例如：教會主日聚會的講道與禱告"
        domainField.identifier = NSUserInterfaceItemIdentifier("dictionaryDomain")
        domainField.delegate = self
        hotwordsField.placeholderString = "輸入後按 Return 或逗號分隔"
        hotwordsField.identifier = NSUserInterfaceItemIdentifier("dictionaryHotwords")
        hotwordsField.delegate = self
        hotwordsField.tokenizingCharacterSet = CharacterSet(charactersIn: ",，、\n")
        hotwordsField.heightAnchor.constraint(equalToConstant: 52).isActive = true
        return host.group(title: "情境與專有詞", views: [
            labeledRow("情境說明", domainField, info: Self.contextFieldsExplanation),
            labeledRow("專有詞", hotwordsField, info: Self.contextFieldsExplanation, infoAtTop: true),
        ])
    }

    /// One labelled row on the same leading column as the rest of the app
    /// (`Metrics.labelColumn`), with the control taking the remaining width.
    private func labeledRow(_ title: String, _ control: NSView, info: String? = nil, infoAtTop: Bool = false) -> NSView {
        let key = NSTextField(labelWithString: title)
        key.alignment = .right
        key.lineBreakMode = .byTruncatingTail
        key.widthAnchor.constraint(equalToConstant: Metrics.labelColumn).isActive = true
        key.setContentHuggingPriority(.required, for: .horizontal)
        var trailing: NSView = control
        if let info {
            let controlRow = NSStackView(views: [control, InfoButton(explanation: info)])
            controlRow.orientation = .horizontal
            controlRow.alignment = infoAtTop ? .top : .centerY
            controlRow.spacing = Metrics.hair + 2
            controlRow.distribution = .fill
            trailing = controlRow
        }
        control.setContentHuggingPriority(.defaultLow, for: .horizontal)
        control.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let row = NSStackView(views: [key, trailing])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = Metrics.row
        row.distribution = .fill
        trailing.trailingAnchor.constraint(equalTo: row.trailingAnchor).isActive = true
        return row
    }

    // MARK: - Rendering

    func pageDidAppear() {
        if let limits = service().features?.contextLimits {
            store.limits = limits
        }
        if store.listState != .loading {
            store.reloadList()
        }
        update()
    }

    private func storeDidChange(_ change: DictionaryStore.Change) {
        switch change {
        case .list:
            listTable.reloadData()
            syncListSelection()
        case .document:
            domainField.stringValue = store.draft.domain
            hotwordsField.objectValue = store.draft.hotwords
            rulesNotice = nil
            reloadRules()
            clearPreview()
            schedulePreview()
        case .rows:
            reloadRules()
            schedulePreview()
        case .draft:
            reloadRuleIssues()
            schedulePreview()
        case .status:
            reloadRuleIssues()
        }
        update()
    }

    private var pageState: PageState {
        Self.pageState(listState: store.listState, hasDictionaries: !store.summaries.isEmpty, service: service())
    }

    /// Cheap, idempotent refresh of every label, enabled state and visibility
    /// on the page. Safe to call on every status tick.
    func update() {
        let service = service()
        // The service came back while the page was showing "unreachable":
        // fetch again instead of making the user press 重新整理.
        if service.reachable == true, lastKnownReachable != true,
           case .failed(let error) = store.listState, error.isUnreachable {
            store.reloadList()
        }
        lastKnownReachable = service.reachable

        let state = pageState
        let ready = state == .ready
        stateGroup.isHidden = ready
        listGroup.isHidden = !ready
        let loaded = ready && store.documentState == .loaded
        editorBar.isHidden = !ready || store.selectedName == nil
        rulesGroup.isHidden = !loaded
        testGroup.isHidden = !loaded
        contextGroup.isHidden = !loaded
        if !ready {
            renderState(state)
        }
        renderDocumentMessage(ready: ready)
        renderEditorBar(loaded: loaded)
        renderRulesStatus()

        let hasSelection = store.selectedName != nil
        let hasBaseline = store.baseline != nil
        duplicateButton.isEnabled = hasBaseline && !store.isSaving
        renameButton.isEnabled = hasBaseline && !store.isSaving
        deleteButton.isEnabled = hasSelection && !store.isSaving
        addRemove.setEnabled(store.selectedName != nil, forSegment: 0)
        addRemove.setEnabled(rulesTable.selectedRowIndexes.count > 0, forSegment: 1)
        host.window?.isDocumentEdited = store.isDirty
    }

    private func renderState(_ state: PageState) {
        let title: String
        let detail: String
        var code = ""
        var symbol = "exclamationmark.triangle.fill"
        var tint: NSColor = .systemOrange
        stateTemplate.isHidden = true
        stateNewBlank.isHidden = true
        stateRefresh.isHidden = false
        switch state {
        case .ready:
            return
        case .loading:
            title = "載入字典中…"
            detail = "正在向服務讀取字典清單。"
            symbol = "arrow.triangle.2.circlepath"
            tint = .secondaryLabelColor
            stateRefresh.isHidden = true
        case .unreachable(let message):
            title = "服務沒有執行"
            detail = "\(message)\n字典存放在服務那一端，服務啟動後才能檢視與編輯。可以到「設定」頁按「重新啟動服務」。"
            symbol = "wifi.slash"
            tint = .systemRed
        case .featureDisabled:
            title = "伺服器尚未開啟字典功能"
            detail = "在 ~/Library/Application Support/TEA ASR/config.toml 的 [service] 區塊加入下面這一行，"
                + "然後到「設定」頁按「重新啟動服務」："
            code = "context_hints_enabled = true"
        case .failed(let message):
            title = "無法讀取字典清單"
            detail = message
            tint = .systemRed
        case .empty:
            title = "還沒有字典"
            detail = "字典讓伺服器把常聽錯的字換成正確的字，例如人名與教會用語。"
                + "可以先用範本建立一個 church 字典，再把自己的對照規則加進去。"
            symbol = "character.book.closed"
            tint = .secondaryLabelColor
            stateTemplate.isHidden = false
            stateNewBlank.isHidden = false
        }
        if stateTitle.stringValue != title { stateTitle.stringValue = title }
        if stateDetail.stringValue != detail { stateDetail.stringValue = detail }
        stateCode.stringValue = code
        stateCode.isHidden = code.isEmpty
        stateIcon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title) ?? NSImage()
        stateIcon.contentTintColor = tint
    }

    private func renderDocumentMessage(ready: Bool) {
        let name = store.selectedName ?? ""
        var message: String?
        switch store.documentState {
        case .none, .loaded:
            message = nil
        case .loading:
            message = "載入「\(name)」中…"
        case .invalid(let reason):
            message = "「\(name)」的檔案格式有誤，伺服器無法讀取：\(reason)\n"
                + "請用文字編輯器修正 ~/Library/Application Support/TEA ASR/dictionaries/\(name).toml，"
                + "或刪除這個字典後重新建立。"
        case .failed(let error):
            message = "無法載入「\(name)」：\(error.localizedDescription)"
        }
        documentGroup.isHidden = !ready || message == nil
        if let message, documentMessage.stringValue != message {
            documentMessage.stringValue = message
        }
        documentMessage.textColor = {
            if case .invalid = store.documentState { return .systemRed }
            if case .failed = store.documentState { return .systemRed }
            return .secondaryLabelColor
        }()
    }

    private func renderEditorBar(loaded: Bool) {
        let name = store.selectedName ?? ""
        if editorName.stringValue != name { editorName.stringValue = name }
        let text: String
        let color: NSColor
        if store.isSaving {
            text = "儲存中…"
            color = .secondaryLabelColor
        } else if store.isDirty {
            text = "● 有尚未儲存的變更"
            color = .systemOrange
        } else if let notice = store.notice {
            text = notice
            color = .secondaryLabelColor
        } else if loaded {
            text = "最後更新 \(DictionaryPresentation.updatedText(store.selectedSummary?.updatedAt))"
            color = .secondaryLabelColor
        } else {
            text = ""
            color = .secondaryLabelColor
        }
        if editorState.stringValue != text { editorState.stringValue = text }
        editorState.textColor = color
        saveButton.isEnabled = loaded && store.isDirty && !store.isSaving
        revertButton.isEnabled = loaded && store.isDirty && !store.isSaving
        revertButton.isHidden = !loaded
        saveButton.isHidden = !loaded

        let errors = loaded ? store.validation.general + store.serverGeneralErrors : []
        let errorText = errors.joined(separator: "\n")
        if editorErrors.stringValue != errorText { editorErrors.stringValue = errorText }
        editorErrors.isHidden = errorText.isEmpty
    }

    private func renderRulesStatus() {
        let total = store.draft.rows.count
        var parts = ["共 \(total) 條"]
        if visibleRows.count != total {
            parts.append("顯示 \(visibleRows.count) 條")
        }
        let issues = store.validation.issueRowCount
        // Only the problem count is red; counts and the paste/import notice
        // are neutral information.
        let font = NSFont.systemFont(ofSize: 12)
        let text = NSMutableAttributedString(
            string: parts.joined(separator: " · "),
            attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]
        )
        if issues > 0 {
            text.append(NSAttributedString(string: " · ", attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]))
            text.append(NSAttributedString(string: "\(issues) 列需要修正", attributes: [.font: font, .foregroundColor: NSColor.systemRed]))
        }
        if let rulesNotice {
            text.append(NSAttributedString(string: "　" + rulesNotice, attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]))
        }
        if rulesStatus.attributedStringValue != text { rulesStatus.attributedStringValue = text }
    }

    private func syncListSelection() {
        isSyncingListSelection = true
        defer { isSyncingListSelection = false }
        if let name = store.selectedName, let row = store.summaries.firstIndex(where: { $0.name == name }) {
            if listTable.selectedRow != row {
                listTable.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            }
        } else {
            listTable.deselectAll(nil)
        }
    }

    private var sortKey: (ReplacementRowOrdering.Key?, Bool) {
        guard let descriptor = rulesTable.sortDescriptors.first, let key = descriptor.key else { return (nil, true) }
        return (ReplacementRowOrdering.Key(rawValue: key), descriptor.ascending)
    }

    private func recomputeVisibleRows() {
        let (key, ascending) = sortKey
        visibleRows = ReplacementRowOrdering.visibleIndices(
            rows: store.draft.rows,
            query: searchField.stringValue,
            key: key,
            ascending: ascending
        )
    }

    private func reloadRules() {
        recomputeVisibleRows()
        rulesTable.reloadData()
    }

    /// Refreshes only the 問題 column (and the counts), never the editable
    /// columns — so finishing an edit in one cell cannot disturb another.
    private func reloadRuleIssues() {
        guard rulesTable.numberOfRows > 0, let column = rulesTable.tableColumns.firstIndex(where: { $0.identifier == Column.issue }) else { return }
        rulesTable.reloadData(forRowIndexes: IndexSet(integersIn: 0..<rulesTable.numberOfRows), columnIndexes: IndexSet(integer: column))
    }

    // MARK: - Table data source and delegate

    func numberOfRows(in tableView: NSTableView) -> Int {
        tableView === listTable ? store.summaries.count : visibleRows.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard let identifier = tableColumn?.identifier else { return nil }
        if tableView === listTable {
            return listCell(identifier: identifier, row: row)
        }
        return ruleCell(identifier: identifier, row: row)
    }

    private func listCell(identifier: NSUserInterfaceItemIdentifier, row: Int) -> NSView? {
        guard store.summaries.indices.contains(row) else { return nil }
        let summary = store.summaries[row]
        let cell = textCell(in: listTable, identifier: identifier, editable: false, withIcon: identifier == Column.name)
        guard let field = cell.textField else { return cell }
        field.alignment = (identifier == Column.hotwords || identifier == Column.rules) ? .right : .left
        field.textColor = .labelColor
        cell.toolTip = summary.error.map { "檔案格式有誤：\($0)" }
        switch identifier {
        case Column.name:
            field.stringValue = summary.name
            if summary.error != nil {
                cell.imageView?.image = NSImage(systemSymbolName: "exclamationmark.triangle.fill", accessibilityDescription: "格式有誤")
                cell.imageView?.contentTintColor = .systemRed
                cell.imageView?.isHidden = false
            } else {
                cell.imageView?.isHidden = true
            }
        case Column.hotwords:
            field.stringValue = summary.hotwordsCount.map(String.init) ?? "—"
            field.textColor = .secondaryLabelColor
        case Column.rules:
            field.stringValue = summary.replacementsCount.map(String.init) ?? "—"
            field.textColor = .secondaryLabelColor
        case Column.updated:
            if summary.error != nil {
                field.stringValue = "格式有誤"
                field.textColor = .systemRed
            } else {
                field.stringValue = DictionaryPresentation.updatedText(summary.updatedAt)
                field.textColor = .secondaryLabelColor
            }
        default:
            break
        }
        return cell
    }

    private func ruleCell(identifier: NSUserInterfaceItemIdentifier, row: Int) -> NSView? {
        guard visibleRows.indices.contains(row) else { return nil }
        let index = visibleRows[row]
        guard store.draft.rows.indices.contains(index) else { return nil }
        let rule = store.draft.rows[index]
        switch identifier {
        case Column.index:
            let cell = textCell(in: rulesTable, identifier: identifier, editable: false)
            cell.textField?.stringValue = String(index + 1)
            cell.textField?.alignment = .right
            cell.textField?.textColor = .tertiaryLabelColor
            cell.textField?.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
            return cell
        case Column.from, Column.to:
            let cell = textCell(in: rulesTable, identifier: identifier, editable: true)
            cell.textField?.stringValue = identifier == Column.from ? rule.from : rule.to
            cell.textField?.placeholderString = identifier == Column.from ? "聽錯的字" : "留空表示刪除"
            return cell
        case Column.arrow:
            let cell = textCell(in: rulesTable, identifier: identifier, editable: false)
            cell.textField?.stringValue = "→"
            cell.textField?.alignment = .center
            cell.textField?.textColor = .tertiaryLabelColor
            return cell
        case Column.issue:
            let cell = textCell(in: rulesTable, identifier: identifier, editable: false, withIcon: true)
            let message = store.validation.message(for: rule.id)
            cell.textField?.stringValue = message ?? ""
            cell.textField?.textColor = .systemRed
            cell.textField?.font = .systemFont(ofSize: 12)
            cell.toolTip = message
            cell.imageView?.image = NSImage(systemSymbolName: "exclamationmark.triangle.fill", accessibilityDescription: "問題")
            cell.imageView?.contentTintColor = .systemRed
            cell.imageView?.isHidden = message == nil
            return cell
        default:
            return nil
        }
    }

    private func textCell(
        in tableView: NSTableView,
        identifier: NSUserInterfaceItemIdentifier,
        editable: Bool,
        withIcon: Bool = false
    ) -> NSTableCellView {
        if let reused = tableView.makeView(withIdentifier: identifier, owner: self) as? NSTableCellView {
            return reused
        }
        let cell = NSTableCellView()
        cell.identifier = identifier
        let field = NSTextField(labelWithString: "")
        field.lineBreakMode = .byTruncatingTail
        field.translatesAutoresizingMaskIntoConstraints = false
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        if editable {
            field.isEditable = true
            field.isSelectable = true
            field.delegate = self
        }
        cell.addSubview(field)
        cell.textField = field
        var leading = cell.leadingAnchor
        var inset: CGFloat = 2
        if withIcon {
            let icon = NSImageView()
            icon.translatesAutoresizingMaskIntoConstraints = false
            icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 11, weight: .regular)
            icon.setContentHuggingPriority(.required, for: .horizontal)
            cell.addSubview(icon)
            cell.imageView = icon
            NSLayoutConstraint.activate([
                icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
                icon.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                icon.widthAnchor.constraint(equalToConstant: 14),
            ])
            leading = icon.trailingAnchor
            inset = 4
        }
        NSLayoutConstraint.activate([
            field.leadingAnchor.constraint(equalTo: leading, constant: inset),
            field.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -2),
            field.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool {
        guard tableView === listTable, !isSyncingListSelection, store.summaries.indices.contains(row) else { return true }
        let target = store.summaries[row].name
        guard target != store.selectedName else { return true }
        return confirmLeavingIfNeeded { [weak self] in
            self?.store.open(target)
        }
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard let tableView = notification.object as? NSTableView else { return }
        if tableView === listTable {
            guard !isSyncingListSelection else { return }
            let row = listTable.selectedRow
            guard store.summaries.indices.contains(row) else { return }
            let name = store.summaries[row].name
            if name != store.selectedName {
                store.open(name)
            }
        } else {
            update()
        }
    }

    func tableView(_ tableView: NSTableView, sortDescriptorsDidChange oldDescriptors: [NSSortDescriptor]) {
        guard tableView === rulesTable else { return }
        reloadRules()
        renderRulesStatus()
    }

    // MARK: - Text editing

    func controlTextDidChange(_ notification: Notification) {
        guard let field = notification.object as? NSControl else { return }
        if field === domainField {
            store.setDomain(domainField.stringValue)
        } else if field === hotwordsField {
            store.setHotwords(currentHotwords())
        } else if field === searchField {
            reloadRules()
            renderRulesStatus()
        } else if field === testField {
            schedulePreview()
        }
    }

    func controlTextDidEndEditing(_ notification: Notification) {
        guard let field = notification.object as? NSTextField else { return }
        if field === hotwordsField {
            store.setHotwords(currentHotwords())
            return
        }
        guard field !== domainField, field !== testField, field !== searchField else { return }
        let row = rulesTable.row(for: field)
        let column = rulesTable.column(for: field)
        guard row >= 0, column >= 0, visibleRows.indices.contains(row) else { return }
        let index = visibleRows[row]
        guard store.draft.rows.indices.contains(index) else { return }
        let id = store.draft.rows[index].id
        let value = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        field.stringValue = value
        switch rulesTable.tableColumns[column].identifier {
        case Column.from: store.updateRow(id, from: value)
        case Column.to: store.updateRow(id, to: value)
        default: break
        }
    }

    private func currentHotwords() -> [String] {
        let tokens = (hotwordsField.objectValue as? [Any]) ?? []
        var seen = Set<String>()
        return tokens.compactMap { token -> String? in
            let word = String(describing: token).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !word.isEmpty, seen.insert(word).inserted else { return nil }
            return word
        }
    }

    /// Commits whatever cell or field is being edited, so ⌘S and switching
    /// dictionaries never lose the last keystrokes.
    private func commitEditing() {
        guard let window = view.window else { return }
        if let editor = window.firstResponder as? NSTextView, editor.isFieldEditor {
            window.makeFirstResponder(nil)
        }
    }

    // MARK: - Leaving with unsaved changes

    /// Returns `true` when it is fine to leave the current dictionary right
    /// now. With unsaved changes it asks: discard → `true`; save → `false`,
    /// and `proceed` runs after the save succeeds; cancel → `false`.
    @discardableResult
    func confirmLeavingIfNeeded(_ proceed: @escaping () -> Void) -> Bool {
        commitEditing()
        guard store.isDirty, let name = store.selectedName else { return true }
        switch askLeave(name) {
        case .discard:
            store.discardChanges()
            return true
        case .cancel:
            return false
        case .save:
            save(then: proceed)
            return false
        }
    }

    /// Runs `action` straight away, or after the unsaved-changes question.
    private func afterLeaving(_ action: @escaping () -> Void) {
        if confirmLeavingIfNeeded(action) {
            action()
        }
    }

    func confirmQuit() -> Bool {
        commitEditing()
        guard store.isDirty, let name = store.selectedName else { return true }
        let alert = NSAlert()
        alert.messageText = "「\(name)」有尚未儲存的變更"
        alert.informativeText = "結束 TEA ASR 會放棄這些變更。要先回去儲存嗎？"
        alert.addButton(withTitle: "回去儲存")
        alert.addButton(withTitle: "放棄變更並結束")
        if alert.runModal() == .alertFirstButtonReturn {
            host.show(section: .dictionaries)
            return false
        }
        return true
    }

    // MARK: - Actions

    @objc private func refreshList(_ sender: Any?) {
        store.reloadList()
    }

    @objc private func createFromTemplate(_ sender: Any?) {
        let name = DictionaryTemplates.churchName
        guard DictionaryNameRule.problem(with: name, existing: store.names) == nil else {
            showMessage("已經有 church 字典", "清單裡已經有名為 church 的字典。")
            return
        }
        store.create(name, content: DictionaryTemplates.church) { [weak self] result in
            if case .failure(let error) = result {
                self?.showMessage("無法建立字典", Self.createFailureText(error, name: name))
            }
        }
    }

    @objc private func newDictionary(_ sender: Any?) {
        afterLeaving { [weak self] in
            guard let self else { return }
            guard let name = self.askName(
                "新增字典",
                "名稱也是伺服器上的檔名，只能用英文字母、數字、底線與連字號，例如 church、youth_camp。OBS 外掛會用這個名稱選字典。",
                "",
                self.store.names
            ) else { return }
            self.store.create(name, content: .empty) { result in
                if case .failure(let error) = result {
                    self.showMessage("無法建立字典", Self.createFailureText(error, name: name))
                }
            }
        }
    }

    @objc private func duplicateDictionary(_ sender: Any?) {
        guard let source = store.selectedName else { return }
        afterLeaving { [weak self] in
            guard let self else { return }
            guard let name = self.askName(
                "複製「\(source)」",
                "會複製已儲存的版本。新字典的名稱：",
                DictionaryNameRule.copyName(for: source, existing: self.store.names),
                self.store.names
            ) else { return }
            self.store.duplicate(as: name) { result in
                if case .failure(let error) = result {
                    self.showMessage("無法複製字典", Self.createFailureText(error, name: name))
                }
            }
        }
    }

    @objc private func renameDictionary(_ sender: Any?) {
        guard let source = store.selectedName else { return }
        afterLeaving { [weak self] in
            guard let self else { return }
            guard let name = self.askName(
                "重新命名「\(source)」",
                "OBS 裡選了「\(source)」的場景，改名後要重新選一次。新名稱：",
                source,
                self.store.names.filter { $0 != source }
            ), name != source else { return }
            self.store.rename(to: name) { result in
                if case .failure(let error) = result {
                    if self.store.selectedName == source {
                        // The new name was never written; nothing changed.
                        self.showMessage("無法重新命名", Self.createFailureText(error, name: name))
                        return
                    }
                    self.showMessage(
                        "重新命名沒有完成",
                        "已建立「\(name)」，但刪除舊的「\(source)」時失敗：\(error.localizedDescription)\n兩個字典目前都在，可以之後再刪除舊的。"
                    )
                }
            }
        }
    }

    @objc private func deleteDictionary(_ sender: Any?) {
        guard let name = store.selectedName, askDelete(name) else { return }
        store.deleteSelected { [weak self] result in
            if case .failure(let error) = result {
                self?.showMessage("無法刪除字典", error.localizedDescription)
            }
        }
    }

    /// A create is a PUT with `"base_revision": null`, so 409 means the name
    /// was taken on the server after the list was last read.
    private static func createFailureText(_ error: DictionaryAPIError, name: String) -> String {
        if case .conflict = error {
            return "伺服器上已經有名為「\(name)」的字典（可能剛由別處建立）。請按「重新整理」後換一個名稱。"
        }
        return error.localizedDescription
    }

    @objc private func revertChanges(_ sender: Any?) {
        store.discardChanges()
    }

    @objc private func saveChanges(_ sender: Any?) {
        save(then: nil)
    }

    func save(then proceed: (() -> Void)?) {
        commitEditing()
        let validation = store.validation
        guard validation.isValid else {
            let rows = validation.issueRowCount
            var lines: [String] = []
            if rows > 0 { lines.append("對照表有 \(rows) 列需要修正（看「問題」欄）。") }
            lines.append(contentsOf: validation.general)
            showMessage("還不能儲存", lines.joined(separator: "\n"))
            revealFirstIssue()
            return
        }
        let name = store.selectedName ?? ""
        store.save(resolveConflict: { [weak self] in
            self?.askConflict(name) ?? .cancel
        }) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .saved:
                proceed?()
            case .rejected:
                let rows = self.store.serverRowErrors.count
                var message = self.store.serverGeneralErrors.joined(separator: "\n")
                if rows > 0 {
                    message = ["伺服器指出對照表有 \(rows) 列有問題，已標在「問題」欄。", message]
                        .filter { !$0.isEmpty }.joined(separator: "\n")
                }
                self.showMessage("伺服器沒有接受這份字典", message)
                self.revealFirstIssue()
            case .failed(let error):
                self.showMessage("儲存失敗", error.localizedDescription)
            case .reloaded, .cancelled, .notNeeded:
                break
            }
        }
    }

    private func revealFirstIssue() {
        let issues = store.validation.rowIssues
        guard let row = visibleRows.firstIndex(where: { issues[store.draft.rows[$0].id] != nil }) else { return }
        rulesTable.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        rulesTable.scrollRowToVisible(row)
    }

    @objc private func addRemoveClicked(_ sender: NSSegmentedControl) {
        if sender.selectedSegment == 0 {
            addRule()
        } else {
            removeSelectedRules()
        }
    }

    func addRule() {
        commitEditing()
        if !searchField.stringValue.isEmpty {
            searchField.stringValue = ""
        }
        let id = store.addRow()
        guard let index = store.draft.rows.firstIndex(where: { $0.id == id }),
              let row = visibleRows.firstIndex(of: index) else { return }
        rulesTable.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        rulesTable.scrollRowToVisible(row)
        if let column = rulesTable.tableColumns.firstIndex(where: { $0.identifier == Column.from }), rulesTable.window != nil {
            rulesTable.editColumn(column, row: row, with: nil, select: true)
        }
    }

    func removeSelectedRules() {
        commitEditing()
        let ids = Set(rulesTable.selectedRowIndexes.compactMap { row -> UUID? in
            guard visibleRows.indices.contains(row) else { return nil }
            return store.draft.rows[visibleRows[row]].id
        })
        guard !ids.isEmpty else { return }
        let first = rulesTable.selectedRowIndexes.first ?? 0
        store.removeRows(ids)
        let next = min(first, visibleRows.count - 1)
        if next >= 0 {
            rulesTable.selectRowIndexes(IndexSet(integer: next), byExtendingSelection: false)
        }
        update()
    }

    private func copySelectedRules() {
        let rules = rulesTable.selectedRowIndexes.compactMap { row -> ReplacementRule? in
            guard visibleRows.indices.contains(row) else { return nil }
            return store.draft.rows[visibleRows[row]].rule
        }
        guard !rules.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(ReplacementPasteParser.render(rules), forType: .string)
    }

    @objc func pasteRules(_ sender: Any?) {
        guard store.documentState == .loaded else { return }
        commitEditing()
        let parsed = ReplacementPasteParser.parse(readPasteboard() ?? "")
        guard !parsed.rules.isEmpty else {
            showMessage(
                "剪貼簿裡沒有可以加入的規則",
                "每行一條，寫成「錯字 => 正字」，或從試算表複製兩欄（聽錯的字、正確的字）。"
            )
            return
        }
        if !searchField.stringValue.isEmpty {
            searchField.stringValue = ""
        }
        let result = store.append(parsed.rules)
        var notice = "已加入 \(result.added.count) 條"
        if result.skipped > 0 { notice += "，\(result.skipped) 條已存在" }
        if !parsed.skippedLines.isEmpty { notice += "，\(parsed.skippedLines.count) 行看不懂而略過" }
        rulesNotice = notice + "。"
        let addedRows = IndexSet(result.added.compactMap { id in
            store.draft.rows.firstIndex(where: { $0.id == id }).flatMap { visibleRows.firstIndex(of: $0) }
        })
        if !addedRows.isEmpty {
            rulesTable.selectRowIndexes(addedRows, byExtendingSelection: false)
            rulesTable.scrollRowToVisible(addedRows.last!)
        }
        update()
    }

    @objc private func importTOML(_ sender: Any?) {
        commitEditing()
        let panel = NSOpenPanel()
        panel.title = "匯入字典（.toml）"
        panel.allowedContentTypes = [UTType(filenameExtension: "toml") ?? .plainText]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let text = try String(contentsOf: url, encoding: .utf8)
            importContent(try DictionaryTOML.parse(text), from: url.lastPathComponent)
        } catch let error as DictionaryTOML.ParseError {
            showMessage("無法匯入 \(url.lastPathComponent)", error.localizedDescription)
        } catch {
            showMessage("無法讀取 \(url.lastPathComponent)", error.localizedDescription)
        }
    }

    func importContent(_ content: DictionaryContent, from fileName: String) {
        let current = store.draft
        let isEmpty = current.rows.isEmpty && current.hotwords.isEmpty && current.domain.isEmpty
        let choice = isEmpty ? .replace : askImport()
        switch choice {
        case .cancel:
            return
        case .replace:
            store.replaceContent(content)
            domainField.stringValue = store.draft.domain
            hotwordsField.objectValue = store.draft.hotwords
            rulesNotice = "已從 \(fileName) 匯入 \(content.replacements.count) 條（尚未儲存）。"
        case .append:
            if current.domain.isEmpty, !content.domain.isEmpty {
                store.setDomain(content.domain)
                domainField.stringValue = content.domain
            }
            let words = current.hotwords + content.hotwords.filter { !current.hotwords.contains($0) }
            store.setHotwords(words)
            hotwordsField.objectValue = words
            let result = store.append(content.replacements)
            rulesNotice = "已從 \(fileName) 加入 \(result.added.count) 條（尚未儲存）。"
        }
        update()
    }

    @objc private func exportTOML(_ sender: Any?) {
        commitEditing()
        let panel = NSSavePanel()
        panel.title = "匯出字典"
        panel.nameFieldStringValue = "\(store.selectedName ?? "dictionary").toml"
        panel.allowedContentTypes = [UTType(filenameExtension: "toml") ?? .plainText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try DictionaryTOML.render(store.draft.content).write(to: url, atomically: true, encoding: .utf8)
        } catch {
            showMessage("匯出失敗", error.localizedDescription)
        }
    }

    // MARK: - Test area

    private func clearPreview() {
        testResult.stringValue = ""
        testApplied.stringValue = ""
    }

    private func schedulePreview() {
        NSObject.cancelPreviousPerformRequests(withTarget: self, selector: #selector(runPreview), object: nil)
        guard !testField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              store.documentState == .loaded
        else {
            previewGeneration += 1
            clearPreview()
            return
        }
        perform(#selector(runPreview), with: nil, afterDelay: 0.35)
    }

    @objc private func runPreview() {
        let text = testField.stringValue
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        previewGeneration += 1
        let generation = previewGeneration
        // The server validates the draft it is sent (a duplicate 聽錯的字 is a
        // 422) and caps the sentence at 2000 characters; say so up front
        // rather than showing the server's English error.
        if !store.validation.rowIssues.isEmpty {
            testResult.stringValue = ""
            testApplied.stringValue = "對照表有需要修正的列，修正後才能測試。"
            testApplied.textColor = .systemOrange
            return
        }
        if dictionaryCharacterCount(text) > 2000 {
            testResult.stringValue = ""
            testApplied.stringValue = "測試句子最多 2000 字。"
            testApplied.textColor = .systemOrange
            return
        }
        store.preview(text) { [weak self] result in
            guard let self, generation == self.previewGeneration else { return }
            switch result {
            case .success(let preview):
                self.testResult.stringValue = preview.text
                self.testResult.textColor = .labelColor
                self.testApplied.stringValue = DictionaryPresentation.appliedSummary(preview.applied)
                self.testApplied.textColor = .secondaryLabelColor
            case .failure(let error):
                self.testResult.stringValue = ""
                self.testApplied.stringValue = "無法測試：\(error.localizedDescription)"
                self.testApplied.textColor = .systemRed
            }
        }
    }

    // MARK: - Default dialogs

    private static func runLeaveAlert(_ name: String) -> LeaveChoice {
        let alert = NSAlert()
        alert.messageText = "要儲存「\(name)」的變更嗎？"
        alert.informativeText = "如果不儲存，這次的修改會遺失。"
        alert.addButton(withTitle: "儲存")
        alert.addButton(withTitle: "不儲存")
        alert.addButton(withTitle: "取消")
        switch alert.runModal() {
        case .alertFirstButtonReturn: return .save
        case .alertSecondButtonReturn: return .discard
        default: return .cancel
        }
    }

    private static func runConflictAlert(_ name: String) -> DictionaryStore.ConflictChoice {
        let alert = NSAlert()
        alert.messageText = "「\(name)」在別處被修改過"
        alert.informativeText = "你開始編輯之後，伺服器上的這個字典已經被改過（可能是另一個視窗，或有人直接改了檔案）。\n\n"
            + "覆寫：用你畫面上的內容取代伺服器上的版本；被取代的版本仍會留在歷史紀錄裡。\n"
            + "重新載入：放棄你的修改，改看伺服器上的版本。"
        alert.addButton(withTitle: "覆寫")
        alert.addButton(withTitle: "重新載入")
        alert.addButton(withTitle: "取消")
        switch alert.runModal() {
        case .alertFirstButtonReturn: return .overwrite
        case .alertSecondButtonReturn: return .reload
        default: return .cancel
        }
    }

    private static func runNamePrompt(title: String, message: String, initial: String, existing: [String]) -> String? {
        var value = initial
        var problem: String?
        while true {
            let alert = NSAlert()
            alert.messageText = title
            alert.informativeText = problem.map { "\($0)\n\n\(message)" } ?? message
            alert.addButton(withTitle: "確定")
            alert.addButton(withTitle: "取消")
            let field = NSTextField(string: value)
            field.placeholderString = "church"
            field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
            alert.accessoryView = field
            alert.window.initialFirstResponder = field
            guard alert.runModal() == .alertFirstButtonReturn else { return nil }
            value = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if let reason = DictionaryNameRule.problem(with: value, existing: existing) {
                problem = reason
                continue
            }
            return value
        }
    }

    private static func runDeleteAlert(_ name: String) -> Bool {
        let alert = NSAlert()
        alert.messageText = "要刪除字典「\(name)」嗎？"
        alert.informativeText = "刪除後 OBS 就選不到它了。伺服器會把檔案移到歷史紀錄（dictionaries/.history），需要時仍可以救回。"
        alert.addButton(withTitle: "刪除")
        alert.addButton(withTitle: "取消")
        if #available(macOS 11.0, *) {
            alert.buttons.first?.hasDestructiveAction = true
        }
        return alert.runModal() == .alertFirstButtonReturn
    }

    private static func runImportAlert() -> ImportChoice {
        let alert = NSAlert()
        alert.messageText = "要怎麼匯入？"
        alert.informativeText = "取代：用檔案內容取代目前的情境說明、專有詞與對照表。\n"
            + "加到後面：保留目前內容，只加入檔案裡新的專有詞與對照規則。\n\n匯入後仍需按「儲存」。"
        alert.addButton(withTitle: "加到後面")
        alert.addButton(withTitle: "取代")
        alert.addButton(withTitle: "取消")
        switch alert.runModal() {
        case .alertFirstButtonReturn: return .append
        case .alertSecondButtonReturn: return .replace
        default: return .cancel
        }
    }

    private static func runMessage(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.runModal()
    }
}

#if DEBUG
extension DictionaryPage {
    var debugRulesTable: NSTableView { rulesTable }
    var debugListTable: NSTableView { listTable }
    var debugSaveButton: NSButton { saveButton }
    var debugEditorState: String { editorState.stringValue }
    var debugRulesStatus: String { rulesStatus.stringValue }
    var debugStateTitle: String { stateTitle.stringValue }
    var debugTestResult: String { testResult.stringValue }
    var debugTestApplied: String { testApplied.stringValue }
    var debugVisibleRows: [Int] { visibleRows }
    var debugIsEditorVisible: Bool { !rulesGroup.isHidden }

    /// Types `text` into the test field and runs the preview immediately,
    /// without the typing debounce.
    func debugRunPreview(_ text: String) {
        testField.stringValue = text
        NSObject.cancelPreviousPerformRequests(withTarget: self, selector: #selector(runPreview), object: nil)
        runPreview()
    }

    func debugSearch(_ query: String) {
        searchField.stringValue = query
        reloadRules()
        renderRulesStatus()
    }

    func debugSelectDictionary(row: Int) {
        if tableView(listTable, shouldSelectRow: row) {
            listTable.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        }
    }
}
#endif
