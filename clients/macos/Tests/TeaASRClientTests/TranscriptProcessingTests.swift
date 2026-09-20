import XCTest
@testable import TeaASRClient

final class TranscriptProcessingTests: XCTestCase {
    func testDefaultProcessorIsStrictNoOp() {
        let raw = "  你好，世界\n\t🙂\u{E000}  "

        let result = TranscriptProcessor().process(rawTranscript: raw)

        XCTAssertEqual(result.rawTranscript, raw)
        XCTAssertEqual(result.cleanedText, raw)
        XCTAssertEqual(result.pasteText, raw)
        XCTAssertTrue(result.appliedSteps.isEmpty)
    }

    func testRulesRunInDeclaredOrderAcrossCleaningAndPasteStages() {
        let processor = TranscriptProcessor(
            cleaningRules: [
                ReplacementRule(identifier: "replace-a", from: "A", to: "B"),
                ReplacementRule(identifier: "replace-b", from: "B", to: "C"),
            ],
            pasteRules: [
                ReplacementRule(identifier: "append-mark", from: "C", to: "C!"),
            ]
        )

        let result = processor.process(rawTranscript: "A")

        XCTAssertEqual(result.rawTranscript, "A")
        XCTAssertEqual(result.cleanedText, "C")
        XCTAssertEqual(result.pasteText, "C!")
        XCTAssertEqual(result.appliedSteps, ["replace-a", "replace-b", "append-mark"])
    }

    func testEmptyTextRemainsEmptyAndDoesNotInventSteps() {
        let processor = TranscriptProcessor(
            cleaningRules: [ReplacementRule(identifier: "unused", from: "x", to: "y")]
        )

        let result = processor.process(rawTranscript: "")

        XCTAssertEqual(result.rawTranscript, "")
        XCTAssertEqual(result.cleanedText, "")
        XCTAssertEqual(result.pasteText, "")
        XCTAssertTrue(result.appliedSteps.isEmpty)
    }

    func testUnicodeAndCJKSurviveDeterministicProcessing() {
        let processor = TranscriptProcessor(
            cleaningRules: [
                ReplacementRule(identifier: "normalize-comma", from: "，", to: ","),
                ReplacementRule(identifier: "normalize-question", from: "？", to: "?"),
            ]
        )

        let result = processor.process(rawTranscript: "你好，世界？カフェ🙂")

        XCTAssertEqual(result.cleanedText, "你好,世界?カフェ🙂")
        XCTAssertEqual(result.pasteText, result.cleanedText)
        XCTAssertEqual(result.appliedSteps, ["normalize-comma", "normalize-question"])
    }

    func testFinalMetadataIsPreservedWhileTextIsProcessed() {
        let transcript = Wire.Transcript(
            segmentId: "seg-7",
            segmentIndex: 7,
            revision: 3,
            startSample: 16_000,
            endSample: 32_000,
            timestampQuality: "segment",
            text: "A",
            warnings: ["diagnostic"]
        )
        let timestamp = Date(timeIntervalSince1970: 1_234)
        let processor = TranscriptProcessor(
            cleaningRules: [ReplacementRule(identifier: "replace", from: "A", to: "B")]
        )

        let result = processor.process(final: transcript, timestamp: timestamp)

        XCTAssertEqual(result.metadata.segmentID, "seg-7")
        XCTAssertEqual(result.metadata.segmentIndex, 7)
        XCTAssertEqual(result.metadata.revision, 3)
        XCTAssertEqual(result.metadata.startSample, 16_000)
        XCTAssertEqual(result.metadata.endSample, 32_000)
        XCTAssertEqual(result.metadata.timestampQuality, "segment")
        XCTAssertEqual(result.metadata.timestamp, timestamp)
        XCTAssertEqual(result.metadata.warnings, ["diagnostic"])
        XCTAssertEqual(result.rawTranscript, "A")
        XCTAssertEqual(result.cleanedText, "B")
        XCTAssertEqual(result.pasteText, "B")
    }

    func testPartialEventBypassesProcessing() {
        let processor = TranscriptEventProcessor(
            processor: TranscriptProcessor(
                cleaningRules: [ReplacementRule(identifier: "should-not-run", from: "A", to: "B")]
            )
        )
        let partial = Wire.Transcript(
            segmentId: "seg-partial",
            segmentIndex: 0,
            revision: 1,
            startSample: 0,
            endSample: 1_600,
            timestampQuality: "segment",
            text: "A",
            warnings: nil
        )

        let result = processor.process(.partial(partial), timestamp: Date())

        XCTAssertNil(result)
    }

    func testTimestampQualityPreservesPresentValue() throws {
        let data = Data(
            #"{"segment_id":"seg-present","segment_index":1,"revision":2,"start_sample":0,"end_sample":1600,"timestamp_quality":"segment","text":"你好"}"#.utf8
        )

        let transcript = try JSONDecoder().decode(Wire.Transcript.self, from: data)

        XCTAssertEqual(transcript.timestampQuality, "segment")
    }

    func testTimestampQualityDefaultsWhenFieldIsAbsent() throws {
        let data = Data(
            #"{"segment_id":"seg-legacy","segment_index":1,"revision":2,"start_sample":0,"end_sample":1600,"text":"你好"}"#.utf8
        )

        let transcript = try JSONDecoder().decode(Wire.Transcript.self, from: data)

        XCTAssertEqual(transcript.timestampQuality, "segment")
    }

    func testPresentationAndInsertionPolicyStaySeparate() {
        let timestamp = Date(timeIntervalSince1970: 1_234)
        let transcript = Wire.Transcript(
            segmentId: "seg-policy",
            segmentIndex: 1,
            revision: 1,
            startSample: 0,
            endSample: 1_600,
            timestampQuality: "segment",
            text: "A"
        )
        let processed = TranscriptProcessor(
            cleaningRules: [ReplacementRule(identifier: "clean", from: "A", to: "B")],
            pasteRules: [ReplacementRule(identifier: "paste", from: "B", to: "C")]
        ).process(final: transcript, timestamp: timestamp)

        XCTAssertEqual(TranscriptOutputPolicy.presentationText(from: processed), "B")
        XCTAssertEqual(TranscriptOutputPolicy.insertionText(from: processed), "C")
    }

    // MARK: - Trailing punctuation strip

    func testStripTrailingPunctuationRemovesIdeographicFullStop() {
        let rule = TrailingPeriodStripRule()

        XCTAssertEqual(rule.apply(to: "今天天氣很好。"), "今天天氣很好")
    }

    func testStripTrailingPunctuationRemovesAsciiPeriod() {
        let rule = TrailingPeriodStripRule()

        XCTAssertEqual(rule.apply(to: "The weather is nice."), "The weather is nice")
    }

    func testStripTrailingPunctuationRemovesFullWidthLatinPeriod() {
        let rule = TrailingPeriodStripRule()

        // U+FF0E FULLWIDTH FULL STOP, distinct from U+3002 IDEOGRAPHIC FULL STOP.
        XCTAssertEqual(rule.apply(to: "今天天氣很好\u{FF0E}"), "今天天氣很好")
    }

    func testStripTrailingPunctuationLeavesQuestionAndExclamationMarksAlone() {
        let rule = TrailingPeriodStripRule()

        // The user's report was specifically about the trailing full stop;
        // "？"/"！" carry intonation this rule deliberately does not touch.
        XCTAssertEqual(rule.apply(to: "今天天氣好嗎？"), "今天天氣好嗎？")
        XCTAssertEqual(rule.apply(to: "太棒了！"), "太棒了！")
    }

    func testStripTrailingPunctuationLeavesInteriorPunctuationAlone() {
        let rule = TrailingPeriodStripRule()

        XCTAssertEqual(
            rule.apply(to: "先說結論。再說原因。最後補充。"),
            "先說結論。再說原因。最後補充"
        )
    }

    func testStripTrailingPunctuationDoesNotMisfireOnDecimalNumbers() {
        let rule = TrailingPeriodStripRule()

        XCTAssertEqual(rule.apply(to: "圓周率大約是 3.14"), "圓周率大約是 3.14")
        // A bare trailing digit-period, not only one followed by more digits.
        XCTAssertEqual(rule.apply(to: "版本 5."), "版本 5.")
    }

    func testStripTrailingPunctuationDoesNotMisfireOnAbbreviations() {
        let rule = TrailingPeriodStripRule()

        XCTAssertEqual(rule.apply(to: "他任職於 Acme Inc."), "他任職於 Acme Inc.")
        XCTAssertEqual(rule.apply(to: "請找 Dr."), "請找 Dr.")
    }

    func testStripTrailingPunctuationHandlesEmptyAndWhitespaceOnlyStrings() {
        let rule = TrailingPeriodStripRule()

        XCTAssertEqual(rule.apply(to: ""), "")
        XCTAssertEqual(rule.apply(to: "   "), "   ")
        XCTAssertEqual(rule.apply(to: "\n\t"), "\n\t")
    }

    func testStripTrailingPunctuationHandlesPunctuationOnlyStrings() {
        let rule = TrailingPeriodStripRule()

        XCTAssertEqual(rule.apply(to: "。"), "")
        XCTAssertEqual(rule.apply(to: "."), "")
        XCTAssertEqual(rule.apply(to: "？"), "？")
    }

    func testStripTrailingPunctuationIgnoresTrailingWhitespaceAfterTheMark() {
        let rule = TrailingPeriodStripRule()

        XCTAssertEqual(rule.apply(to: "今天天氣很好。  "), "今天天氣很好  ")
    }

    func testStripTrailingPunctuationDisabledByDefaultLeavesProcessorAStrictNoOp() {
        let raw = "今天天氣很好。"

        let result = TranscriptProcessor(
            isTrailingPunctuationStripEnabled: { false }
        ).process(rawTranscript: raw)

        XCTAssertEqual(result.rawTranscript, raw)
        XCTAssertEqual(result.cleanedText, raw)
        XCTAssertEqual(result.pasteText, raw)
        XCTAssertTrue(result.appliedSteps.isEmpty)
    }

    func testStripTrailingPunctuationEnabledAffectsCleanedAndPasteTextButNotRawTranscript() {
        let raw = "今天天氣很好。"

        let result = TranscriptProcessor(
            isTrailingPunctuationStripEnabled: { true }
        ).process(rawTranscript: raw)

        XCTAssertEqual(result.rawTranscript, raw, "rawTranscript must stay byte-for-byte identical to the server text")
        XCTAssertEqual(result.cleanedText, "今天天氣很好")
        XCTAssertEqual(result.pasteText, "今天天氣很好")
        XCTAssertEqual(result.appliedSteps, ["stripTrailingPunctuation"])
    }

    func testStripTrailingPunctuationEnabledButNoTrailingMarkRecordsNoStep() {
        let result = TranscriptProcessor(
            isTrailingPunctuationStripEnabled: { true }
        ).process(rawTranscript: "今天天氣很好嗎？")

        XCTAssertEqual(result.cleanedText, "今天天氣很好嗎？")
        XCTAssertTrue(result.appliedSteps.isEmpty)
    }

    /// Flipped from off-by-default to on-by-default: the model appending a
    /// trailing full stop the user never spoke was reported twice, so this
    /// is no longer a silent opt-in (see `Settings.stripTrailingPunctuation`'s
    /// doc comment). The setting must still persist an explicit override in
    /// either direction.
    func testStripTrailingPunctuationSettingDefaultsToOnAndPersistsAnExplicitOverride() throws {
        let suiteName = "TeaASRClientTests.StripTrailingPunctuation.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let settings = Settings(defaults: defaults)
        XCTAssertTrue(settings.stripTrailingPunctuation)

        settings.stripTrailingPunctuation = false
        XCTAssertFalse(Settings(defaults: defaults).stripTrailingPunctuation)
    }

    func testTranscriptProcessorDefaultInitReadsLiveSettingValue() throws {
        let suiteName = "TeaASRClientTests.StripTrailingPunctuation.Live.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let settings = Settings(defaults: defaults)
        settings.stripTrailingPunctuation = false

        let processor = TranscriptProcessor(
            isTrailingPunctuationStripEnabled: { settings.stripTrailingPunctuation }
        )

        let beforeToggle = processor.process(rawTranscript: "今天天氣很好。")
        XCTAssertEqual(beforeToggle.cleanedText, "今天天氣很好。")

        settings.stripTrailingPunctuation = true

        let afterToggle = processor.process(rawTranscript: "今天天氣很好。")
        XCTAssertEqual(afterToggle.cleanedText, "今天天氣很好")
    }

    // MARK: - Trailing punctuation strip: now on by default

    func testStripTrailingPunctuationIsOnByDefaultAndDoesNotAffectRawTranscript() {
        let raw = "今天天氣很好。"

        let result = TranscriptProcessor().process(rawTranscript: raw)

        XCTAssertEqual(result.rawTranscript, raw, "rawTranscript must stay byte-for-byte identical to the server text")
        XCTAssertEqual(result.cleanedText, "今天天氣很好")
        XCTAssertEqual(result.pasteText, "今天天氣很好")
        XCTAssertEqual(result.appliedSteps, ["stripTrailingPunctuation"])
    }

    // MARK: - Spoken symbol names

    func testSpokenSymbolsReplacesEveryEntryInTheBuiltInTable() {
        let rule = SpokenSymbolReplacementRule()
        let expectations: [String: String] = [
            "逗號": "，",
            "句號": "。",
            "問號": "？",
            "驚嘆號": "！",
            "感嘆號": "！",
            "冒號": "：",
            "分號": "；",
            "頓號": "、",
            "左括號": "（",
            "開括號": "（",
            "右括號": "）",
            "閉括號": "）",
            "括號": "（）",
            "左引號": "「",
            "開引號": "「",
            "右引號": "」",
            "閉引號": "」",
            "引號": "「」",
            "破折號": "—",
            "刪節號": "…",
            "省略號": "…",
            "換行": "\n",
            "換行符": "\n",
            "新段落": "\n\n",
            "換段": "\n\n",
        ]
        XCTAssertEqual(SpokenSymbolReplacementRule.table.count, expectations.count, "table drifted from this test's coverage")
        for (name, mark) in expectations {
            XCTAssertEqual(rule.apply(to: "前面\(name)後面"), "前面\(mark)後面", "「\(name)」should become「\(mark)」")
        }
    }

    func testSpokenSymbolsReplacesAStandaloneNameMidSentence() {
        let rule = SpokenSymbolReplacementRule()

        XCTAssertEqual(rule.apply(to: "你好逗號很高興認識你"), "你好，很高興認識你")
    }

    /// A directional name ("左括號") must win over the shorter generic name
    /// ("括號") it contains as a suffix — otherwise scanning would emit a
    /// stray "左" followed by the generic pair.
    func testSpokenSymbolsLongestMatchWinsOverAShorterNameItContains() {
        let rule = SpokenSymbolReplacementRule()

        XCTAssertEqual(rule.apply(to: "請幫我打左括號"), "請幫我打（")
        XCTAssertEqual(rule.apply(to: "請幫我打括號"), "請幫我打（）")
    }

    /// The one deterministic mitigation this rule applies: a name quoted in
    /// `「」`/`『』`/curly or ASCII quotes is almost certainly being talked
    /// *about*, not spoken as a command, so it is left alone.
    func testSpokenSymbolsSkipsMatchesInsideQuotationMarks() {
        let rule = SpokenSymbolReplacementRule()

        XCTAssertEqual(rule.apply(to: "他說「逗號」是最常用的標點"), "他說「逗號」是最常用的標點")
        XCTAssertEqual(rule.apply(to: "老師說 \"句號\" 用來結束句子"), "老師說 \"句號\" 用來結束句子")
    }

    /// Documents the known, accepted limitation (see
    /// `SpokenSymbolReplacementRule`'s doc comment and
    /// `Settings.spokenSymbols`'s doc comment): outside of quotation marks,
    /// there is no local, deterministic way to tell a genuine, literal use of
    /// the word "逗號" apart from a command asking for a comma — both look
    /// identical as plain text. This is why the rule ships off by default.
    func testSpokenSymbolsMisfiresOnAGenuineUnquotedUseOfTheWordItself() {
        let rule = SpokenSymbolReplacementRule()

        XCTAssertEqual(rule.apply(to: "逗號的意思是用來分隔子句"), "，的意思是用來分隔子句")
    }

    func testSpokenSymbolsDisabledByDefaultLeavesProcessorAStrictNoOp() {
        let raw = "你好逗號世界"

        let result = TranscriptProcessor().process(rawTranscript: raw)

        XCTAssertEqual(result.cleanedText, raw)
        XCTAssertTrue(result.appliedSteps.isEmpty)
    }

    func testSpokenSymbolsEnabledAffectsCleanedAndPasteTextButNotRawTranscript() {
        let raw = "你好逗號世界"

        let result = TranscriptProcessor(isSpokenSymbolsEnabled: { true }).process(rawTranscript: raw)

        XCTAssertEqual(result.rawTranscript, raw)
        XCTAssertEqual(result.cleanedText, "你好，世界")
        XCTAssertEqual(result.pasteText, "你好，世界")
        XCTAssertEqual(result.appliedSteps, ["spokenSymbols"])
    }

    func testSpokenSymbolsSettingDefaultsToOffAndPersists() throws {
        let suiteName = "TeaASRClientTests.SpokenSymbols.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let settings = Settings(defaults: defaults)
        XCTAssertFalse(settings.spokenSymbols)

        settings.spokenSymbols = true
        XCTAssertTrue(Settings(defaults: defaults).spokenSymbols)
    }

    // MARK: - Execution order between the two rules

    /// Pins the order decision itself: trailing-punctuation strip must run
    /// *before* spoken-symbol replacement. If the order were reversed, a
    /// spoken "句號" at the very end of an utterance would first become "。"
    /// and then have that just-inserted period immediately stripped back off
    /// by the (now default-on) trailing strip rule — silently discarding the
    /// exact symbol the user asked to type. Running strip first means it
    /// only ever sees punctuation the ASR model produced on its own; the
    /// symbol this rule inserts on the user's explicit request is never
    /// touched, because strip has already run by the time it appears.
    func testTrailingPunctuationStripRunsBeforeSpokenSymbolReplacement() {
        let raw = "你好句號"

        let result = TranscriptProcessor(
            isTrailingPunctuationStripEnabled: { true },
            isSpokenSymbolsEnabled: { true }
        ).process(rawTranscript: raw)

        // Wrong order (replace, then strip) would produce "你好" — the
        // period the user explicitly asked for would vanish. Correct order
        // keeps it.
        XCTAssertEqual(result.cleanedText, "你好。")
        XCTAssertEqual(result.appliedSteps, ["spokenSymbols"], "strip ran but found nothing to remove, so only the replacement is recorded")
    }

    /// Same pin, but with both defaults now on (`stripTrailingPunctuation`
    /// defaults to true, `spokenSymbols` still defaults to false) — exercises
    /// the production default wiring end to end, not just the overridden
    /// closures above.
    func testTrailingPunctuationStripRunsBeforeSpokenSymbolReplacementWithBothEnabledViaDefaultInit() {
        let result = TranscriptProcessor(isSpokenSymbolsEnabled: { true }).process(rawTranscript: "你好句號")

        XCTAssertEqual(result.cleanedText, "你好。")
    }

    private struct ReplacementRule: TranscriptProcessingRule {
        let identifier: String
        let from: String
        let to: String

        func apply(to text: String) -> String {
            text.replacingOccurrences(of: from, with: to)
        }
    }
}
