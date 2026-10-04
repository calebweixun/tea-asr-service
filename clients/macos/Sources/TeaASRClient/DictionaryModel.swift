import Foundation

// MARK: - Wire types
//
// These mirror the `/v1/dictionaries` HTTP contract. Like `ServerStatus`,
// they are plain Decodable structs rather than dictionaries, and they decode
// leniently where the contract leaves a type open (revision, updated_at):
// an unexpected-but-harmless shape must not make the whole page unusable.

/// The server's revision token: the SHA-256 hex digest of the file's bytes.
/// The client never interprets it; it only echoes it back as `base_revision`.
typealias DictionaryRevision = String

/// `updated_at`: the file's mtime as a UTC ISO 8601 string. The raw text is
/// kept so an unparseable value is still shown rather than dropped.
struct DictionaryTimestamp: Decodable, Equatable {
    let raw: String
    let date: Date?

    init(raw: String, date: Date?) {
        self.raw = raw
        self.date = date
    }

    init(from decoder: Decoder) throws {
        let text = try decoder.singleValueContainer().decode(String.self)
        self.init(raw: text, date: Self.parse(text))
    }

    static func parse(_ text: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: text) { return date }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: text) { return date }
        // Python's `isoformat()` writes microseconds, which ISO8601DateFormatter
        // rejects; drop them (sub-second precision is never displayed).
        if let dot = text.firstIndex(of: "."),
           let zone = text[dot...].firstIndex(where: { $0 == "+" || $0 == "-" || $0 == "Z" }) {
            return plain.date(from: String(text[..<dot]) + String(text[zone...]))
        }
        return nil
    }
}

/// One row of `GET /v1/dictionaries`. An invalid file arrives as
/// `{name, error}` only, which is why every other field is optional.
struct DictionarySummary: Decodable, Equatable {
    let name: String
    let domain: String?
    let hotwordsCount: Int?
    let replacementsCount: Int?
    let revision: DictionaryRevision?
    let updatedAt: DictionaryTimestamp?
    let error: String?

    init(
        name: String,
        domain: String? = nil,
        hotwordsCount: Int? = nil,
        replacementsCount: Int? = nil,
        revision: DictionaryRevision? = nil,
        updatedAt: DictionaryTimestamp? = nil,
        error: String? = nil
    ) {
        self.name = name
        self.domain = domain
        self.hotwordsCount = hotwordsCount
        self.replacementsCount = replacementsCount
        self.revision = revision
        self.updatedAt = updatedAt
        self.error = error
    }

    enum CodingKeys: String, CodingKey {
        case name
        case domain
        case hotwordsCount = "hotwords_count"
        case replacementsCount = "replacements_count"
        case revision
        case updatedAt = "updated_at"
        case error
    }
}

/// One `{from, to}` replacement exactly as it travels on the wire.
struct ReplacementRule: Codable, Equatable, Hashable {
    var from: String
    var to: String
}

/// The editable part of a dictionary: what a PUT sends and what dirty-state
/// compares. Name and revision live outside it on purpose — renaming is a
/// separate operation, and a revision bump alone is not an edit.
struct DictionaryContent: Equatable {
    var domain: String
    var hotwords: [String]
    var replacements: [ReplacementRule]

    static let empty = DictionaryContent(domain: "", hotwords: [], replacements: [])
}

/// `GET /v1/dictionaries/{name}` (and the body of a successful PUT).
struct DictionaryDocument: Decodable, Equatable {
    let name: String
    let content: DictionaryContent
    let revision: DictionaryRevision?
    let updatedAt: DictionaryTimestamp?

    init(name: String, content: DictionaryContent, revision: DictionaryRevision?, updatedAt: DictionaryTimestamp? = nil) {
        self.name = name
        self.content = content
        self.revision = revision
        self.updatedAt = updatedAt
    }

    enum CodingKeys: String, CodingKey {
        case name
        case domain
        case hotwords
        case replacements
        case revision
        case updatedAt = "updated_at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        content = DictionaryContent(
            domain: try container.decodeIfPresent(String.self, forKey: .domain) ?? "",
            hotwords: try container.decodeIfPresent([String].self, forKey: .hotwords) ?? [],
            replacements: try container.decodeIfPresent([ReplacementRule].self, forKey: .replacements) ?? []
        )
        revision = try container.decodeIfPresent(DictionaryRevision.self, forKey: .revision)
        updatedAt = try container.decodeIfPresent(DictionaryTimestamp.self, forKey: .updatedAt)
    }
}

/// When a PUT may write. The server checks `base_revision` only when the key
/// is present, and compares it with the current file's revision (`null` when
/// the file does not exist), which gives three distinct behaviours.
enum DictionaryWriteCondition: Equatable {
    /// `"base_revision": null` — create; 409 if the name already exists.
    case createOnly
    /// `"base_revision": "<rev>"` — replace only if nothing changed since.
    case ifRevision(DictionaryRevision)
}

/// `PUT /v1/dictionaries/{name}`. The client never omits `base_revision`:
/// omitting it overwrites unconditionally, which this editor never wants.
struct DictionaryPutBody: Encodable, Equatable {
    let domain: String
    let hotwords: [String]
    let replacements: [ReplacementRule]
    let condition: DictionaryWriteCondition

    init(content: DictionaryContent, condition: DictionaryWriteCondition) {
        domain = content.domain
        hotwords = content.hotwords
        replacements = content.replacements
        self.condition = condition
    }

    enum CodingKeys: String, CodingKey {
        case domain
        case hotwords
        case replacements
        case baseRevision = "base_revision"
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(domain, forKey: .domain)
        try container.encode(hotwords, forKey: .hotwords)
        try container.encode(replacements, forKey: .replacements)
        switch condition {
        case .createOnly: try container.encodeNil(forKey: .baseRevision)
        case .ifRevision(let revision): try container.encode(revision, forKey: .baseRevision)
        }
    }
}

/// `POST /v1/dictionaries/{name}/preview`. `dictionary` is the unsaved draft,
/// so the test area reflects what is on screen, not what was last saved.
struct DictionaryPreviewBody: Encodable, Equatable {
    struct Draft: Encodable, Equatable {
        let domain: String
        let hotwords: [String]
        let replacements: [ReplacementRule]
    }

    let text: String
    let dictionary: Draft?

    init(text: String, draft: DictionaryContent?) {
        self.text = text
        dictionary = draft.map { Draft(domain: $0.domain, hotwords: $0.hotwords, replacements: $0.replacements) }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(text, forKey: .text)
        try container.encodeIfPresent(dictionary, forKey: .dictionary)
    }

    enum CodingKeys: String, CodingKey {
        case text
        case dictionary
    }
}

struct DictionaryPreviewResult: Decodable, Equatable {
    struct Applied: Decodable, Equatable {
        let from: String
        let to: String
        let count: Int
    }

    let text: String
    let applied: [Applied]
}

/// One entry of a 422 `invalid` response's `error.details`. `field` is a
/// dotted path such as `replacements.from`, `hotwords`, `name` or `file`;
/// `index` is the list position for hotwords/replacements, otherwise null.
struct DictionaryFieldError: Decodable, Equatable {
    let field: String
    let index: Int?
    let message: String

    /// Whether this error points at one replacement row.
    var isReplacementRow: Bool {
        (field == "replacements" || field.hasPrefix("replacements.")) && index != nil
    }

    /// The server's messages are English; the one a user can hit from this
    /// editor's table (a duplicate 聽錯的字) is shown in the same words the
    /// local check uses, so the 問題 column never says the same thing twice.
    var localizedMessage: String {
        let prefix = "duplicate source; first used at index "
        if message.hasPrefix(prefix), let first = Int(message.dropFirst(prefix.count)) {
            return ReplacementRowIssue.duplicateFrom(firstRow: first + 1).message
        }
        return message
    }
}

/// The server's documented input limits (`features.context_limits`). The
/// defaults are the server's own defaults, used until capabilities arrive.
struct DictionaryLimits: Decodable, Equatable {
    var maxDomainChars = 300
    var maxHotwords = 200
    var maxHotwordChars = 32
    var maxReplacements = 500
    var maxReplacementChars = 32

    static let standard = DictionaryLimits()

    init() {}

    enum CodingKeys: String, CodingKey {
        case maxDomainChars = "max_domain_chars"
        case maxHotwords = "max_hotwords"
        case maxHotwordChars = "max_hotword_chars"
        case maxReplacements = "max_replacements"
        case maxReplacementChars = "max_replacement_chars"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let fallback = DictionaryLimits()
        maxDomainChars = try container.decodeIfPresent(Int.self, forKey: .maxDomainChars) ?? fallback.maxDomainChars
        maxHotwords = try container.decodeIfPresent(Int.self, forKey: .maxHotwords) ?? fallback.maxHotwords
        maxHotwordChars = try container.decodeIfPresent(Int.self, forKey: .maxHotwordChars) ?? fallback.maxHotwordChars
        maxReplacements = try container.decodeIfPresent(Int.self, forKey: .maxReplacements) ?? fallback.maxReplacements
        maxReplacementChars = try container.decodeIfPresent(Int.self, forKey: .maxReplacementChars) ?? fallback.maxReplacementChars
    }
}

// MARK: - Draft and validation

/// Counts characters the way the server does: Python's `len()` counts code
/// points, so a limit of 32 means 32 Unicode scalars, not 32 graphemes.
func dictionaryCharacterCount(_ text: String) -> Int {
    text.unicodeScalars.count
}

/// A replacement row on screen. `id` is stable across sorting, filtering and
/// edits, so a validation message or a server 422 stays attached to the row
/// it was about even after the view order changes.
struct ReplacementRow: Equatable {
    let id: UUID
    var from: String
    var to: String

    init(id: UUID = UUID(), from: String, to: String) {
        self.id = id
        self.from = from
        self.to = to
    }

    var rule: ReplacementRule { ReplacementRule(from: from, to: to) }
}

/// The editor's working copy of one dictionary.
struct DictionaryDraft: Equatable {
    var domain: String
    var hotwords: [String]
    var rows: [ReplacementRow]

    init(content: DictionaryContent) {
        domain = content.domain
        hotwords = content.hotwords
        rows = content.replacements.map { ReplacementRow(from: $0.from, to: $0.to) }
    }

    /// What would be sent on save. Dirty state is `content != baseline`, so
    /// row identities (which change on every load) never count as an edit,
    /// and undoing a change by hand makes the editor clean again.
    var content: DictionaryContent {
        DictionaryContent(domain: domain, hotwords: hotwords, replacements: rows.map(\.rule))
    }

    func isDirty(comparedTo baseline: DictionaryContent?) -> Bool {
        guard let baseline else { return false }
        return content != baseline
    }

    /// Appends rules, skipping any `from → to` pair already present (pasting
    /// the same list twice should not double it). A rule whose `from` exists
    /// with a different `to` is still added, so validation can flag it as a
    /// duplicate for the user to resolve rather than silently picking one.
    @discardableResult
    mutating func append(_ rules: [ReplacementRule]) -> (added: [UUID], skipped: Int) {
        var existing = Set(rows.map(\.rule))
        var added: [UUID] = []
        var skipped = 0
        for rule in rules {
            guard !existing.contains(rule) else {
                skipped += 1
                continue
            }
            existing.insert(rule)
            let row = ReplacementRow(from: rule.from, to: rule.to)
            rows.append(row)
            added.append(row.id)
        }
        return (added, skipped)
    }
}

/// What is wrong with one replacement row, worded for the 「問題」 column.
enum ReplacementRowIssue: Equatable {
    case fromEmpty
    case fromTooLong(limit: Int)
    case toTooLong(limit: Int)
    case duplicateFrom(firstRow: Int)
    case server(String)

    var message: String {
        switch self {
        case .fromEmpty: return "聽錯的字不可空白"
        case .fromTooLong(let limit): return "聽錯的字超過 \(limit) 字"
        case .toTooLong(let limit): return "正確的字超過 \(limit) 字"
        case .duplicateFrom(let firstRow): return "與第 \(firstRow) 列重複"
        case .server(let message): return message
        }
    }
}

struct DictionaryValidation: Equatable {
    var rowIssues: [UUID: [ReplacementRowIssue]] = [:]
    /// Problems outside the replacement table (domain, hotwords, counts).
    var general: [String] = []

    var isValid: Bool { rowIssues.isEmpty && general.isEmpty }
    var issueRowCount: Int { rowIssues.count }

    func message(for id: UUID) -> String? {
        guard let issues = rowIssues[id], !issues.isEmpty else { return nil }
        return issues.map(\.message).joined(separator: "；")
    }
}

enum DictionaryValidator {
    /// Local checks mirroring the server's own rules, so most mistakes are
    /// caught while typing instead of on save. `serverRowErrors` are the
    /// row-level messages from the last 422, keyed by row id; they are shown
    /// alongside (not instead of) the local checks until that row is edited.
    static func validate(
        _ draft: DictionaryDraft,
        limits: DictionaryLimits = .standard,
        serverRowErrors: [UUID: String] = [:]
    ) -> DictionaryValidation {
        var result = DictionaryValidation()
        if dictionaryCharacterCount(draft.domain) > limits.maxDomainChars {
            result.general.append("情境說明超過 \(limits.maxDomainChars) 字。")
        }
        if draft.hotwords.count > limits.maxHotwords {
            result.general.append("專有詞最多 \(limits.maxHotwords) 個（目前 \(draft.hotwords.count) 個）。")
        }
        let longHotwords = draft.hotwords.filter { dictionaryCharacterCount($0) > limits.maxHotwordChars }
        if !longHotwords.isEmpty {
            result.general.append("專有詞每個最多 \(limits.maxHotwordChars) 字：\(longHotwords.joined(separator: "、"))")
        }
        if draft.rows.count > limits.maxReplacements {
            result.general.append("對照表最多 \(limits.maxReplacements) 條（目前 \(draft.rows.count) 條）。")
        }

        var firstRowForFrom: [String: Int] = [:]
        for (index, row) in draft.rows.enumerated() {
            var issues: [ReplacementRowIssue] = []
            if row.from.isEmpty {
                issues.append(.fromEmpty)
            } else if dictionaryCharacterCount(row.from) > limits.maxReplacementChars {
                issues.append(.fromTooLong(limit: limits.maxReplacementChars))
            }
            if dictionaryCharacterCount(row.to) > limits.maxReplacementChars {
                issues.append(.toTooLong(limit: limits.maxReplacementChars))
            }
            if !row.from.isEmpty {
                if let first = firstRowForFrom[row.from] {
                    issues.append(.duplicateFrom(firstRow: first + 1))
                } else {
                    firstRowForFrom[row.from] = index
                }
            }
            if let server = serverRowErrors[row.id], !issues.map(\.message).contains(server) {
                issues.append(.server(server))
            }
            if !issues.isEmpty {
                result.rowIssues[row.id] = issues
            }
        }
        return result
    }
}

/// Dictionary names become file names on the server, so they follow the
/// server's own rule exactly: `^[A-Za-z0-9_-]{1,32}$`.
enum DictionaryNameRule {
    static let maxLength = 32

    /// `nil` when `name` is acceptable; otherwise the reason, in zh-TW.
    static func problem(with name: String, existing: [String]) -> String? {
        if name.isEmpty {
            return "請輸入名稱。"
        }
        if name.count > maxLength {
            return "名稱最多 \(maxLength) 個字元。"
        }
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        if name.unicodeScalars.contains(where: { !allowed.contains($0) }) {
            return "名稱只能用英文字母、數字、底線（_）與連字號（-），因為它也是伺服器上的檔名。"
        }
        if existing.contains(name) {
            return "已經有名為「\(name)」的字典。"
        }
        return nil
    }

    /// A free name for a copy: `church-copy`, `church-copy2`, … trimmed so the
    /// result still fits the length limit.
    static func copyName(for name: String, existing: [String]) -> String {
        for attempt in 1...99 {
            let suffix = attempt == 1 ? "-copy" : "-copy\(attempt)"
            let base = String(name.prefix(maxLength - suffix.count))
            let candidate = base + suffix
            if !existing.contains(candidate) { return candidate }
        }
        return ""
    }
}

// MARK: - Bulk paste

/// Turns pasted text into replacement rules. One rule per line, written as
/// `錯字 => 正字`, `錯字 → 正字`, `錯字 -> 正字`, or two tab-separated cells
/// (which is what copying two columns out of Numbers/Excel produces). Blank
/// lines and `#` comments are ignored; any other line that does not split
/// into exactly a non-empty "from" and a "to" is reported back, never guessed.
enum ReplacementPasteParser {
    static let separators = ["=>", "⇒", "→", "->", "\t"]

    struct Result: Equatable {
        var rules: [ReplacementRule] = []
        var skippedLines: [String] = []
    }

    static func parse(_ text: String) -> Result {
        var result = Result()
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
        // Tabs are kept: they are a separator, and a spreadsheet row with an
        // empty 正確的字 cell ends in one.
        var spaces = CharacterSet.whitespaces
        spaces.remove(charactersIn: "\t")
        for rawLine in lines {
            let line = rawLine.trimmingCharacters(in: spaces)
            if line.trimmingCharacters(in: .whitespaces).isEmpty || line.hasPrefix("#") { continue }
            guard let rule = parseLine(line) else {
                result.skippedLines.append(line)
                continue
            }
            result.rules.append(rule)
        }
        return result
    }

    private static func parseLine(_ line: String) -> ReplacementRule? {
        for separator in separators {
            let parts = line.components(separatedBy: separator)
            guard parts.count >= 2 else { continue }
            // A tab-separated row from a spreadsheet may carry trailing empty
            // cells; anything else with more than one separator is ambiguous.
            let meaningful = separator == "\t"
                ? Array(parts.prefix(2)) + parts.dropFirst(2).filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
                : parts
            guard meaningful.count == 2 else { return nil }
            let from = meaningful[0].trimmingCharacters(in: .whitespaces)
            let to = meaningful[1].trimmingCharacters(in: .whitespaces)
            guard !from.isEmpty else { return nil }
            return ReplacementRule(from: from, to: to)
        }
        return nil
    }

    /// The inverse, used for copying selected rows back out.
    static func render(_ rules: [ReplacementRule]) -> String {
        rules.map { "\($0.from) => \($0.to)" }.joined(separator: "\n")
    }
}

// MARK: - TOML import / export

/// Reads and writes the server's dictionary file format. This is not a
/// general TOML parser: it accepts exactly what the server accepts —
/// `domain`, `hotwords`, and `replacements` either as `[[replacements]]`
/// tables or as an inline array — and rejects anything else with the line it
/// stopped at, the same strictness the server applies (`extra="forbid"`).
enum DictionaryTOML {
    struct ParseError: LocalizedError, Equatable {
        let line: Int
        let reason: String

        var errorDescription: String? { "第 \(line) 行：\(reason)" }
    }

    static func render(_ content: DictionaryContent) -> String {
        var output = "domain = \(quote(content.domain))\n"
        if content.hotwords.isEmpty {
            output += "hotwords = []\n"
        } else {
            output += "hotwords = [\n"
            for word in content.hotwords {
                output += "  \(quote(word)),\n"
            }
            output += "]\n"
        }
        for rule in content.replacements {
            output += "\n[[replacements]]\nfrom = \(quote(rule.from))\nto = \(quote(rule.to))\n"
        }
        return output
    }

    static func quote(_ value: String) -> String {
        var output = "\""
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\"": output += "\\\""
            case "\\": output += "\\\\"
            case "\n": output += "\\n"
            case "\t": output += "\\t"
            case "\r": output += "\\r"
            default:
                if scalar.value < 0x20 || scalar.value == 0x7F {
                    output += String(format: "\\u%04X", scalar.value)
                } else {
                    output.unicodeScalars.append(scalar)
                }
            }
        }
        return output + "\""
    }

    static func parse(_ text: String) throws -> DictionaryContent {
        var parser = Parser(text: text)
        return try parser.parseDocument()
    }

    private enum Value {
        case string(String)
        case array([Value])
        case table([(String, Value)])
    }

    private struct Parser {
        let scalars: [Unicode.Scalar]
        var position = 0
        var line = 1

        init(text: String) {
            scalars = Array(text.unicodeScalars)
        }

        mutating func parseDocument() throws -> DictionaryContent {
            var domain: String?
            var hotwords: [String]?
            var replacements: [ReplacementRule] = []
            var sawInlineReplacements = false
            var currentTable: [(String, Value)]?
            var currentTableLine = 0

            func closeTable() throws {
                guard let table = currentTable else { return }
                replacements.append(try Self.rule(from: table, line: currentTableLine))
                currentTable = nil
            }

            while true {
                skipWhitespaceAndComments(newlines: true)
                guard let scalar = peek() else { break }
                if scalar == "[" {
                    let headerLine = line
                    if peek(offset: 1) == "[" {
                        advance(); advance()
                        skipInlineWhitespace()
                        let name = try parseKey()
                        skipInlineWhitespace()
                        guard consume("]"), consume("]") else { throw error("區塊標題沒有用 ]] 結束") }
                        guard name == "replacements" else { throw error("不支援的區塊 [[\(name)]]") }
                        guard !sawInlineReplacements else {
                            throw error("replacements 不能同時用兩種寫法")
                        }
                        try closeTable()
                        currentTable = []
                        currentTableLine = headerLine
                    } else {
                        throw error("不支援的區塊（只接受 [[replacements]]）")
                    }
                    try expectEndOfLine()
                    continue
                }

                let keyLine = line
                let key = try parseKey()
                skipInlineWhitespace()
                guard consume("=") else { throw error("「\(key)」後面缺少 =") }
                skipInlineWhitespace()
                let value = try parseValue()
                try expectEndOfLine()

                if currentTable != nil {
                    guard key == "from" || key == "to" else {
                        throw ParseError(line: keyLine, reason: "[[replacements]] 只能有 from 與 to，不能有「\(key)」")
                    }
                    if currentTable!.contains(where: { $0.0 == key }) {
                        throw ParseError(line: keyLine, reason: "「\(key)」重複")
                    }
                    currentTable!.append((key, value))
                    continue
                }
                switch key {
                case "domain":
                    guard domain == nil else { throw ParseError(line: keyLine, reason: "domain 重複") }
                    guard case .string(let text) = value else {
                        throw ParseError(line: keyLine, reason: "domain 必須是字串")
                    }
                    domain = text
                case "hotwords":
                    guard hotwords == nil else { throw ParseError(line: keyLine, reason: "hotwords 重複") }
                    guard case .array(let items) = value else {
                        throw ParseError(line: keyLine, reason: "hotwords 必須是字串陣列")
                    }
                    hotwords = try items.map {
                        guard case .string(let word) = $0 else {
                            throw ParseError(line: keyLine, reason: "hotwords 必須是字串陣列")
                        }
                        return word
                    }
                case "replacements":
                    guard !sawInlineReplacements, replacements.isEmpty else {
                        throw ParseError(line: keyLine, reason: "replacements 重複")
                    }
                    guard case .array(let items) = value else {
                        throw ParseError(line: keyLine, reason: "replacements 必須是 {from, to} 的陣列")
                    }
                    replacements = try items.map {
                        guard case .table(let pairs) = $0 else {
                            throw ParseError(line: keyLine, reason: "replacements 必須是 {from, to} 的陣列")
                        }
                        return try Self.rule(from: pairs, line: keyLine)
                    }
                    sawInlineReplacements = true
                default:
                    throw ParseError(line: keyLine, reason: "不認得的欄位「\(key)」（只接受 domain、hotwords、replacements）")
                }
            }
            try closeTable()
            return DictionaryContent(domain: domain ?? "", hotwords: hotwords ?? [], replacements: replacements)
        }

        private static func rule(from pairs: [(String, Value)], line: Int) throws -> ReplacementRule {
            var from: String?
            var to: String?
            for (key, value) in pairs {
                guard case .string(let text) = value else {
                    throw ParseError(line: line, reason: "\(key) 必須是字串")
                }
                switch key {
                case "from": from = text
                case "to": to = text
                default: throw ParseError(line: line, reason: "replacement 只能有 from 與 to，不能有「\(key)」")
                }
            }
            guard let from else { throw ParseError(line: line, reason: "replacement 缺少 from") }
            guard let to else { throw ParseError(line: line, reason: "replacement 缺少 to") }
            return ReplacementRule(from: from, to: to)
        }

        // MARK: Values

        private mutating func parseValue() throws -> Value {
            guard let scalar = peek() else { throw error("缺少值") }
            switch scalar {
            case "\"":
                return .string(try parseBasicString())
            case "'":
                return .string(try parseLiteralString())
            case "[":
                advance()
                var items: [Value] = []
                while true {
                    skipWhitespaceAndComments(newlines: true)
                    if consume("]") { return .array(items) }
                    items.append(try parseValue())
                    skipWhitespaceAndComments(newlines: true)
                    if consume(",") { continue }
                    if consume("]") { return .array(items) }
                    throw error("陣列元素之間缺少逗號")
                }
            case "{":
                advance()
                var pairs: [(String, Value)] = []
                skipInlineWhitespace()
                if consume("}") { return .table(pairs) }
                while true {
                    skipInlineWhitespace()
                    let key = try parseKey()
                    skipInlineWhitespace()
                    guard consume("=") else { throw error("「\(key)」後面缺少 =") }
                    skipInlineWhitespace()
                    pairs.append((key, try parseValue()))
                    skipInlineWhitespace()
                    if consume(",") { continue }
                    if consume("}") { return .table(pairs) }
                    throw error("行內表格沒有用 } 結束")
                }
            default:
                throw error("只接受字串、陣列與 {from, to}")
            }
        }

        private mutating func parseKey() throws -> String {
            guard let scalar = peek() else { throw error("缺少欄位名稱") }
            if scalar == "\"" { return try parseBasicString() }
            if scalar == "'" { return try parseLiteralString() }
            var key = ""
            while let next = peek(), Self.isBareKey(next) {
                key.unicodeScalars.append(next)
                advance()
            }
            guard !key.isEmpty else { throw error("無法辨識的內容") }
            return key
        }

        private static func isBareKey(_ scalar: Unicode.Scalar) -> Bool {
            switch scalar {
            case "A"..."Z", "a"..."z", "0"..."9", "_", "-": return true
            default: return false
            }
        }

        private mutating func parseBasicString() throws -> String {
            advance()
            let multiline = peek() == "\"" && peek(offset: 1) == "\""
            if multiline {
                advance(); advance()
                if peek() == "\n" { advance(); line += 1 }
            }
            var text = ""
            while true {
                guard let scalar = peek() else { throw error("字串沒有結束") }
                if scalar == "\"" {
                    if !multiline {
                        advance()
                        return text
                    }
                    if peek(offset: 1) == "\"" && peek(offset: 2) == "\"" {
                        advance(); advance(); advance()
                        return text
                    }
                }
                if scalar == "\n" {
                    guard multiline else { throw error("字串不能跨行") }
                    line += 1
                }
                advance()
                if scalar == "\\" {
                    guard let escaped = peek() else { throw error("字串沒有結束") }
                    advance()
                    switch escaped {
                    case "\"": text += "\""
                    case "\\": text += "\\"
                    case "n": text += "\n"
                    case "t": text += "\t"
                    case "r": text += "\r"
                    case "b": text += "\u{08}"
                    case "f": text += "\u{0C}"
                    case "u", "U":
                        let length = escaped == "u" ? 4 : 8
                        var hex = ""
                        for _ in 0..<length {
                            guard let digit = peek() else { throw error("\\u 跳脫字元不完整") }
                            hex.unicodeScalars.append(digit)
                            advance()
                        }
                        guard let value = UInt32(hex, radix: 16), let decoded = Unicode.Scalar(value) else {
                            throw error("\\u 跳脫字元無效")
                        }
                        text.unicodeScalars.append(decoded)
                    default:
                        throw error("不支援的跳脫字元 \\\(escaped)")
                    }
                    continue
                }
                text.unicodeScalars.append(scalar)
            }
        }

        private mutating func parseLiteralString() throws -> String {
            advance()
            var text = ""
            while let scalar = peek() {
                advance()
                if scalar == "'" { return text }
                if scalar == "\n" { throw error("字串不能跨行") }
                text.unicodeScalars.append(scalar)
            }
            throw error("字串沒有結束")
        }

        // MARK: Scanning

        private func peek(offset: Int = 0) -> Unicode.Scalar? {
            let index = position + offset
            return index < scalars.count ? scalars[index] : nil
        }

        private mutating func advance() {
            position += 1
        }

        private mutating func consume(_ scalar: Unicode.Scalar) -> Bool {
            guard peek() == scalar else { return false }
            advance()
            return true
        }

        private mutating func skipInlineWhitespace() {
            while let scalar = peek(), scalar == " " || scalar == "\t" || scalar == "\r" {
                advance()
            }
        }

        private mutating func skipWhitespaceAndComments(newlines: Bool) {
            while let scalar = peek() {
                if scalar == " " || scalar == "\t" || scalar == "\r" || scalar == "\u{FEFF}" {
                    advance()
                } else if scalar == "\n" && newlines {
                    line += 1
                    advance()
                } else if scalar == "#" {
                    while let next = peek(), next != "\n" { advance() }
                } else {
                    return
                }
            }
        }

        private mutating func expectEndOfLine() throws {
            skipInlineWhitespace()
            if peek() == "#" {
                while let next = peek(), next != "\n" { advance() }
            }
            guard let scalar = peek() else { return }
            guard scalar == "\n" else { throw error("同一行有多餘的內容") }
        }

        private func error(_ reason: String) -> ParseError {
            ParseError(line: line, reason: reason)
        }
    }
}

// MARK: - Template and presentation

enum DictionaryTemplates {
    /// The starter offered on an empty list. Deliberately generic: a few
    /// common church terms so the page has something to show, never anyone's
    /// name — the real names belong to the user's own dictionary.
    static let churchName = "church"
    static let church = DictionaryContent(
        domain: "教會主日聚會：講道、禱告、詩歌與報告事項",
        hotwords: ["主日", "以馬內利", "哈利路亞", "聖靈", "禱告", "敬拜", "讀經"],
        replacements: [
            ReplacementRule(from: "以碼內利", to: "以馬內利"),
            ReplacementRule(from: "哈利陸亞", to: "哈利路亞"),
            ReplacementRule(from: "聖令", to: "聖靈"),
        ]
    )
}

enum DictionaryPresentation {
    static func updatedText(_ timestamp: DictionaryTimestamp?, now: Date = Date()) -> String {
        guard let timestamp else { return "—" }
        guard let date = timestamp.date else { return timestamp.raw }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_Hant_TW")
        formatter.dateFormat = Calendar.current.isDate(date, equalTo: now, toGranularity: .year)
            ? "M/d HH:mm"
            : "yyyy/M/d HH:mm"
        return formatter.string(from: date)
    }

    static func appliedSummary(_ applied: [DictionaryPreviewResult.Applied]) -> String {
        guard !applied.isEmpty else { return "沒有套用任何規則。" }
        return applied.map { item in
            let target = item.to.isEmpty ? "（刪除）" : item.to
            return item.count > 1 ? "\(item.from) → \(target) ×\(item.count)" : "\(item.from) → \(target)"
        }.joined(separator: "　")
    }

    static let savedNotice = "已儲存；OBS 下次連線（按『套用連線設定』或重開 OBS）時生效"
}

/// Which draft rows the 對照表 shows, and in what order. Sorting and
/// filtering only change the view: the draft keeps the order it will be
/// saved (and applied) in, so clicking a column header is never an edit.
enum ReplacementRowOrdering {
    enum Key: String {
        case index
        case from
        case to
    }

    static func visibleIndices(
        rows: [ReplacementRow],
        query: String,
        key: Key?,
        ascending: Bool
    ) -> [Int] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        var indices = Array(rows.indices)
        if !needle.isEmpty {
            indices = indices.filter {
                rows[$0].from.localizedCaseInsensitiveContains(needle)
                    || rows[$0].to.localizedCaseInsensitiveContains(needle)
            }
        }
        guard let key, key != .index || !ascending else { return indices }
        func value(_ index: Int) -> String {
            key == .from ? rows[index].from : rows[index].to
        }
        return indices.sorted { lhs, rhs in
            if key == .index { return lhs > rhs }
            let order = value(lhs).localizedStandardCompare(value(rhs))
            if order == .orderedSame { return lhs < rhs }
            return ascending ? order == .orderedAscending : order == .orderedDescending
        }
    }
}
