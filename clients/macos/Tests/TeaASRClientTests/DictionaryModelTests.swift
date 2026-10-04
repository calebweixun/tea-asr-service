import XCTest
@testable import TeaASRClient

/// The AppKit-free half of the 字典 page: decoding every documented
/// `/v1/dictionaries` response, the bulk-paste parser, TOML import/export,
/// local validation, name rules, and draft dirty-state.
final class DictionaryModelTests: XCTestCase {
    // MARK: - Decoding

    func testListDecodesValidAndInvalidEntries() throws {
        let json = """
        [
          {"name":"church","domain":"主日","hotwords_count":12,"replacements_count":116,"error":null,"revision":"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08","updated_at":"2026-10-01T01:30:00.123456+00:00"},
          {"name":"broken","domain":null,"hotwords_count":null,"replacements_count":null,"error":"invalid TOML or dictionary fields","revision":null,"updated_at":null}
        ]
        """
        let list = try JSONDecoder().decode([DictionarySummary].self, from: Data(json.utf8))
        XCTAssertEqual(list.count, 2)
        XCTAssertEqual(list[0].name, "church")
        XCTAssertEqual(list[0].hotwordsCount, 12)
        XCTAssertEqual(list[0].replacementsCount, 116)
        XCTAssertEqual(list[0].revision, "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")
        XCTAssertNotNil(list[0].updatedAt?.date)
        XCTAssertNil(list[0].error)
        XCTAssertEqual(list[1].error, "invalid TOML or dictionary fields")
        XCTAssertNil(list[1].replacementsCount)
        XCTAssertNil(list[1].revision)
    }

    func testServerTimestampsParseWithAndWithoutMicroseconds() {
        // Python's isoformat(): microseconds only when non-zero.
        let micro = DictionaryTimestamp.parse("2026-10-04T15:22:53.669347+00:00")
        let whole = DictionaryTimestamp.parse("2026-10-04T15:22:53+00:00")
        let microSeconds = try? XCTUnwrap(micro).timeIntervalSince1970
        let wholeSeconds = try? XCTUnwrap(whole).timeIntervalSince1970
        XCTAssertEqual(microSeconds ?? 0, wholeSeconds ?? -1, accuracy: 1)
        XCTAssertEqual(DictionaryPresentation.updatedText(DictionaryTimestamp(raw: "garbage", date: nil)), "garbage")
    }

    func testDocumentDecodesFullShape() throws {
        let json = """
        {"name":"church","domain":"主日","hotwords":["以馬內利"],
         "replacements":[{"from":"甲錯","to":"甲對"},{"from":"乙錯","to":""}],
         "revision":"abc123","updated_at":"2026-10-01T01:02:03.456789+00:00"}
        """
        let document = try JSONDecoder().decode(DictionaryDocument.self, from: Data(json.utf8))
        XCTAssertEqual(document.name, "church")
        XCTAssertEqual(document.content.domain, "主日")
        XCTAssertEqual(document.content.hotwords, ["以馬內利"])
        XCTAssertEqual(document.content.replacements, [
            ReplacementRule(from: "甲錯", to: "甲對"),
            ReplacementRule(from: "乙錯", to: ""),
        ])
        XCTAssertEqual(document.revision, "abc123")
        XCTAssertNotNil(document.updatedAt?.date, "fractional-second ISO 8601 must parse")
    }

    func testPreviewResultDecodes() throws {
        let json = #"{"text":"今天由乙對帶領","applied":[{"from":"乙錯","to":"乙對","count":2}]}"#
        let result = try JSONDecoder().decode(DictionaryPreviewResult.self, from: Data(json.utf8))
        XCTAssertEqual(result.text, "今天由乙對帶領")
        XCTAssertEqual(result.applied, [.init(from: "乙錯", to: "乙對", count: 2)])
        XCTAssertEqual(DictionaryPresentation.appliedSummary(result.applied), "乙錯 → 乙對 ×2")
        XCTAssertEqual(DictionaryPresentation.appliedSummary([]), "沒有套用任何規則。")
    }

    func testContextLimitsDecodeFromCapabilities() throws {
        let json = """
        {"native_audio_streaming":true,"partial_transcripts":true,"word_timestamps":false,"translation":false,
         "diarization":false,"hotwords":false,"context_biasing":true,"durable_sessions":false,
         "durable_revisable":false,"batch_jobs":false,
         "context_limits":{"max_domain_chars":300,"max_hotwords":200,"max_hotword_chars":32,"max_replacements":500,"max_replacement_chars":24}}
        """
        let features = try JSONDecoder().decode(CapabilityFeatures.self, from: Data(json.utf8))
        XCTAssertTrue(features.contextBiasing)
        XCTAssertEqual(features.contextLimits?.maxReplacementChars, 24)
        XCTAssertEqual(features.contextLimits?.maxReplacements, 500)
    }

    func testCapabilitiesWithoutContextLimitsStillDecode() throws {
        let json = """
        {"native_audio_streaming":true,"partial_transcripts":true,"word_timestamps":false,"translation":false,
         "diarization":false,"hotwords":false,"context_biasing":false,"durable_sessions":false,
         "durable_revisable":false,"batch_jobs":false}
        """
        let features = try JSONDecoder().decode(CapabilityFeatures.self, from: Data(json.utf8))
        XCTAssertFalse(features.contextBiasing)
        XCTAssertNil(features.contextLimits)
    }

    // MARK: - Encoding

    func testPutBodyAlwaysSendsBaseRevisionNullMeaningCreateOnly() throws {
        // Omitting base_revision overwrites unconditionally on the real
        // server; `null` makes it 409 when the name already exists.
        let body = DictionaryPutBody(content: .empty, condition: .createOnly)
        let json = String(data: try JSONEncoder().encode(body), encoding: .utf8) ?? ""
        XCTAssertTrue(json.contains("\"base_revision\":null"), json)
        let object = try JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]
        XCTAssertEqual(object?["hotwords"] as? [String], [])
        XCTAssertEqual(object?["domain"] as? String, "")
    }

    func testPutBodyEchoesTheRevision() throws {
        let content = DictionaryContent(domain: "d", hotwords: ["w"], replacements: [ReplacementRule(from: "a", to: "b")])
        let object = try JSONSerialization.jsonObject(
            with: JSONEncoder().encode(DictionaryPutBody(content: content, condition: .ifRevision("abc")))
        ) as? [String: Any]
        XCTAssertEqual(object?["base_revision"] as? String, "abc")
        let rules = object?["replacements"] as? [[String: String]]
        XCTAssertEqual(rules?.first?["from"], "a")
        XCTAssertEqual(rules?.first?["to"], "b")
    }

    func testPreviewBodyCarriesTheDraft() throws {
        let draft = DictionaryContent(domain: "", hotwords: [], replacements: [ReplacementRule(from: "x", to: "y")])
        let object = try JSONSerialization.jsonObject(
            with: JSONEncoder().encode(DictionaryPreviewBody(text: "xx", draft: draft))
        ) as? [String: Any]
        XCTAssertEqual(object?["text"] as? String, "xx")
        let dictionary = object?["dictionary"] as? [String: Any]
        XCTAssertEqual((dictionary?["replacements"] as? [[String: String]])?.first?["to"], "y")
    }

    // MARK: - Error envelope

    func testErrorEnvelopeDecodesTheReal422Shape() {
        let json = """
        {"error":{"code":"invalid","message":"Dictionary rules contain duplicate replacement sources.","retryable":false,"request_id":null,
          "details":[{"field":"replacements.from","index":1,"message":"duplicate source; first used at index 0"},
                     {"field":"name","index":null,"message":"must match ^[A-Za-z0-9_-]{1,32}$"}]}}
        """
        let envelope = DictionaryClient.ErrorEnvelope.decode(Data(json.utf8))
        XCTAssertEqual(envelope?.code, "invalid")
        XCTAssertEqual(envelope?.fields.count, 2)
        XCTAssertTrue(envelope?.fields[0].isReplacementRow ?? false)
        XCTAssertEqual(envelope?.fields[0].localizedMessage, "與第 1 列重複", "server indices are 0-based, rows are 1-based")
        XCTAssertFalse(envelope?.fields[1].isReplacementRow ?? true)
        XCTAssertNil(envelope?.currentRevision)
    }

    func testErrorEnvelopeDecodesTheReal409Shape() {
        let json = #"{"error":{"code":"conflict","message":"Dictionary changed since it was read.","retryable":false,"request_id":null,"current_revision":"beef"}}"#
        XCTAssertEqual(DictionaryClient.ErrorEnvelope.decode(Data(json.utf8))?.currentRevision, "beef")
        let gone = #"{"error":{"code":"conflict","message":"x","retryable":false,"current_revision":null}}"#
        XCTAssertNil(DictionaryClient.ErrorEnvelope.decode(Data(gone.utf8))?.currentRevision)
        XCTAssertNil(DictionaryClient.ErrorEnvelope.decode(Data("not json".utf8)))
    }

    func testServerDuplicateMessageDoesNotRepeatTheLocalOne() {
        var draft = DictionaryDraft(content: .empty)
        draft.rows = [ReplacementRow(from: "a", to: "b"), ReplacementRow(from: "a", to: "c")]
        let server = DictionaryFieldError(field: "replacements.from", index: 1, message: "duplicate source; first used at index 0")
        let validation = DictionaryValidator.validate(draft, serverRowErrors: [draft.rows[1].id: server.localizedMessage])
        XCTAssertEqual(validation.rowIssues[draft.rows[1].id], [.duplicateFrom(firstRow: 1)])
    }

    // MARK: - Paste parser

    func testPasteParserAcceptsEverySupportedSeparator() {
        let text = """
        甲錯 => 甲對
        乙錯→乙對
        丙錯 -> 丙對
        丁錯\t丁對
        戊錯 ⇒ 戊對
        """
        let result = ReplacementPasteParser.parse(text)
        XCTAssertEqual(result.rules, [
            ReplacementRule(from: "甲錯", to: "甲對"),
            ReplacementRule(from: "乙錯", to: "乙對"),
            ReplacementRule(from: "丙錯", to: "丙對"),
            ReplacementRule(from: "丁錯", to: "丁對"),
            ReplacementRule(from: "戊錯", to: "戊對"),
        ])
        XCTAssertTrue(result.skippedLines.isEmpty)
    }

    func testPasteParserSkipsBlankAndCommentLinesAndReportsUnreadableOnes() {
        let text = "\r\n# 註解\n甲錯 => 甲對\r\n只有一個字\n => 沒有錯字\na => b => c\n\n"
        let result = ReplacementPasteParser.parse(text)
        XCTAssertEqual(result.rules, [ReplacementRule(from: "甲錯", to: "甲對")])
        XCTAssertEqual(result.skippedLines, ["只有一個字", "=> 沒有錯字", "a => b => c"])
    }

    func testPasteParserAcceptsSpreadsheetRowsWithTrailingEmptyCells() {
        let result = ReplacementPasteParser.parse("甲錯\t甲對\t\t\n乙錯\t\t")
        XCTAssertEqual(result.rules, [
            ReplacementRule(from: "甲錯", to: "甲對"),
            ReplacementRule(from: "乙錯", to: ""),
        ])
    }

    func testPasteRenderRoundTrips() {
        let rules = [ReplacementRule(from: "甲錯", to: "甲對"), ReplacementRule(from: "乙錯", to: "乙對")]
        XCTAssertEqual(ReplacementPasteParser.parse(ReplacementPasteParser.render(rules)).rules, rules)
    }

    // MARK: - Draft dirty state

    func testDraftIsCleanUntilContentActuallyChanges() {
        let baseline = DictionaryContent(domain: "d", hotwords: ["w"], replacements: [ReplacementRule(from: "a", to: "b")])
        var draft = DictionaryDraft(content: baseline)
        XCTAssertFalse(draft.isDirty(comparedTo: baseline))

        draft.rows[0].to = "c"
        XCTAssertTrue(draft.isDirty(comparedTo: baseline))
        draft.rows[0].to = "b"
        XCTAssertFalse(draft.isDirty(comparedTo: baseline), "undoing an edit by hand makes the draft clean again")

        draft.hotwords.append("x")
        XCTAssertTrue(draft.isDirty(comparedTo: baseline))
        draft.hotwords.removeLast()
        draft.domain = "e"
        XCTAssertTrue(draft.isDirty(comparedTo: baseline))
    }

    func testReorderingRowsIsAnEditBecauseOrderIsApplicationOrder() {
        let baseline = DictionaryContent(domain: "", hotwords: [], replacements: [
            ReplacementRule(from: "a", to: "b"), ReplacementRule(from: "c", to: "d"),
        ])
        var draft = DictionaryDraft(content: baseline)
        draft.rows.reverse()
        XCTAssertTrue(draft.isDirty(comparedTo: baseline))
    }

    func testDraftWithoutBaselineIsNeverDirty() {
        XCTAssertFalse(DictionaryDraft(content: .empty).isDirty(comparedTo: nil))
    }

    func testAppendSkipsExactDuplicatesButKeepsConflictingOnes() {
        var draft = DictionaryDraft(content: DictionaryContent(domain: "", hotwords: [], replacements: [ReplacementRule(from: "a", to: "b")]))
        let result = draft.append([
            ReplacementRule(from: "a", to: "b"),
            ReplacementRule(from: "a", to: "c"),
            ReplacementRule(from: "x", to: "y"),
            ReplacementRule(from: "x", to: "y"),
        ])
        XCTAssertEqual(result.added.count, 2)
        XCTAssertEqual(result.skipped, 2)
        XCTAssertEqual(draft.rows.map(\.rule), [
            ReplacementRule(from: "a", to: "b"),
            ReplacementRule(from: "a", to: "c"),
            ReplacementRule(from: "x", to: "y"),
        ])
    }

    // MARK: - Validation

    func testValidationFlagsEmptyTooLongAndDuplicateRows() {
        let long = String(repeating: "字", count: 33)
        var draft = DictionaryDraft(content: .empty)
        draft.rows = [
            ReplacementRow(from: "甲錯", to: "甲對"),
            ReplacementRow(from: "", to: "孤兒"),
            ReplacementRow(from: long, to: "x"),
            ReplacementRow(from: "y", to: long),
            ReplacementRow(from: "甲錯", to: "別的"),
            ReplacementRow(from: "刪掉", to: ""),
        ]
        let validation = DictionaryValidator.validate(draft)
        XCTAssertNil(validation.rowIssues[draft.rows[0].id], "the first occurrence is not the duplicate")
        XCTAssertEqual(validation.rowIssues[draft.rows[1].id], [.fromEmpty])
        XCTAssertEqual(validation.rowIssues[draft.rows[2].id], [.fromTooLong(limit: 32)])
        XCTAssertEqual(validation.rowIssues[draft.rows[3].id], [.toTooLong(limit: 32)])
        XCTAssertEqual(validation.rowIssues[draft.rows[4].id], [.duplicateFrom(firstRow: 1)])
        XCTAssertNil(validation.rowIssues[draft.rows[5].id], "an empty 正確的字 means delete, which the server allows")
        XCTAssertEqual(validation.issueRowCount, 4)
        XCTAssertFalse(validation.isValid)
        XCTAssertEqual(validation.message(for: draft.rows[4].id), "與第 1 列重複")
    }

    func testValidationCountsCodePointsLikeTheServer() {
        var draft = DictionaryDraft(content: .empty)
        // 32 CJK characters is exactly at the limit; an emoji with a skin
        // tone is one grapheme but two code points, as Python counts it.
        draft.rows = [
            ReplacementRow(from: String(repeating: "字", count: 32), to: "ok"),
            ReplacementRow(from: String(repeating: "字", count: 31) + "👍🏽", to: "ok"),
        ]
        let validation = DictionaryValidator.validate(draft)
        XCTAssertNil(validation.rowIssues[draft.rows[0].id])
        XCTAssertEqual(validation.rowIssues[draft.rows[1].id], [.fromTooLong(limit: 32)])
    }

    func testValidationReportsGeneralLimitsFromServerCapabilities() {
        var limits = DictionaryLimits()
        limits.maxHotwords = 2
        limits.maxReplacements = 1
        limits.maxDomainChars = 3
        var draft = DictionaryDraft(content: .empty)
        draft.domain = "四個字啊"
        draft.hotwords = ["a", "b", "c"]
        draft.rows = [ReplacementRow(from: "a", to: "b"), ReplacementRow(from: "c", to: "d")]
        let validation = DictionaryValidator.validate(draft, limits: limits)
        XCTAssertEqual(validation.general.count, 3)
        XCTAssertTrue(validation.rowIssues.isEmpty)
        XCTAssertFalse(validation.isValid)
    }

    func testServerRowErrorsAttachToTheirRow() {
        var draft = DictionaryDraft(content: .empty)
        draft.rows = [ReplacementRow(from: "a", to: "b"), ReplacementRow(from: "c", to: "d")]
        let validation = DictionaryValidator.validate(draft, serverRowErrors: [draft.rows[1].id: "伺服器說不行"])
        XCTAssertEqual(validation.rowIssues[draft.rows[1].id], [.server("伺服器說不行")])
        XCTAssertNil(validation.rowIssues[draft.rows[0].id])
    }

    // MARK: - Names

    func testNameRuleMatchesTheServerPattern() {
        XCTAssertNil(DictionaryNameRule.problem(with: "church", existing: []))
        XCTAssertNil(DictionaryNameRule.problem(with: "Youth_camp-2", existing: []))
        XCTAssertNil(DictionaryNameRule.problem(with: String(repeating: "a", count: 32), existing: []))
        XCTAssertNotNil(DictionaryNameRule.problem(with: "", existing: []))
        XCTAssertNotNil(DictionaryNameRule.problem(with: String(repeating: "a", count: 33), existing: []))
        XCTAssertNotNil(DictionaryNameRule.problem(with: "教會", existing: []))
        XCTAssertNotNil(DictionaryNameRule.problem(with: "has space", existing: []))
        XCTAssertNotNil(DictionaryNameRule.problem(with: "../etc", existing: []))
        XCTAssertNotNil(DictionaryNameRule.problem(with: "church", existing: ["church"]))
    }

    func testCopyNameIsFreeAndFitsTheLimit() {
        XCTAssertEqual(DictionaryNameRule.copyName(for: "church", existing: ["church"]), "church-copy")
        XCTAssertEqual(DictionaryNameRule.copyName(for: "church", existing: ["church", "church-copy"]), "church-copy2")
        let long = String(repeating: "a", count: 32)
        let copy = DictionaryNameRule.copyName(for: long, existing: [long])
        XCTAssertNil(DictionaryNameRule.problem(with: copy, existing: [long]))
    }

    // MARK: - TOML

    func testTOMLRoundTripsAwkwardStrings() throws {
        let content = DictionaryContent(
            domain: "講道 \"引號\" 與 \\ 反斜線\n換行",
            hotwords: ["以馬內利", "tab\there"],
            replacements: [ReplacementRule(from: "甲錯", to: "甲對"), ReplacementRule(from: "刪掉", to: "")]
        )
        XCTAssertEqual(try DictionaryTOML.parse(DictionaryTOML.render(content)), content)
    }

    func testTOMLParsesTheHandWrittenServerFormat() throws {
        let text = """
        # 主日字典
        domain = "教會主日"
        hotwords = [
          "以馬內利",  # 註解
          '哈利路亞',
        ]

        [[replacements]]
        from = "甲錯"
        to = "甲對"

        [[replacements]]
        "from" = '乙錯'
        to = "\\u4E59對"
        """
        let content = try DictionaryTOML.parse(text)
        XCTAssertEqual(content.domain, "教會主日")
        XCTAssertEqual(content.hotwords, ["以馬內利", "哈利路亞"])
        XCTAssertEqual(content.replacements, [
            ReplacementRule(from: "甲錯", to: "甲對"),
            ReplacementRule(from: "乙錯", to: "乙對"),
        ])
    }

    func testTOMLParsesInlineReplacementArray() throws {
        let text = #"""
        domain = ""
        hotwords = []
        replacements = [
          { from = "a", to = "b" },
          {from="c",to="d"},
        ]
        """#
        XCTAssertEqual(try DictionaryTOML.parse(text).replacements, [
            ReplacementRule(from: "a", to: "b"), ReplacementRule(from: "c", to: "d"),
        ])
    }

    func testTOMLRejectsUnknownFieldsWithTheLineNumber() {
        XCTAssertThrowsError(try DictionaryTOML.parse("domain = \"x\"\nprompt = \"y\"\n")) { error in
            XCTAssertEqual((error as? DictionaryTOML.ParseError)?.line, 2)
        }
        XCTAssertThrowsError(try DictionaryTOML.parse("[[replacements]]\nfrom = \"a\"\n")) { error in
            XCTAssertTrue(error.localizedDescription.contains("to"))
        }
        XCTAssertThrowsError(try DictionaryTOML.parse("[service]\n"))
        XCTAssertThrowsError(try DictionaryTOML.parse("domain = \"unterminated\n"))
        XCTAssertThrowsError(try DictionaryTOML.parse("hotwords = [1, 2]\n"))
    }

    // MARK: - Ordering

    func testOrderingFiltersAndSortsWithoutTouchingTheDraft() {
        let rows = [
            ReplacementRow(from: "b", to: "乙"),
            ReplacementRow(from: "a", to: "甲"),
            ReplacementRow(from: "c", to: "a-target"),
        ]
        XCTAssertEqual(ReplacementRowOrdering.visibleIndices(rows: rows, query: "", key: nil, ascending: true), [0, 1, 2])
        XCTAssertEqual(ReplacementRowOrdering.visibleIndices(rows: rows, query: "", key: .from, ascending: true), [1, 0, 2])
        XCTAssertEqual(ReplacementRowOrdering.visibleIndices(rows: rows, query: "", key: .from, ascending: false), [2, 0, 1])
        XCTAssertEqual(ReplacementRowOrdering.visibleIndices(rows: rows, query: "", key: .index, ascending: false), [2, 1, 0])
        XCTAssertEqual(ReplacementRowOrdering.visibleIndices(rows: rows, query: "a", key: nil, ascending: true), [1, 2],
                       "search matches either column")
        XCTAssertEqual(ReplacementRowOrdering.visibleIndices(rows: rows, query: "乙", key: nil, ascending: true), [0])
    }
}
