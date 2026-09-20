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

    func testStripTrailingPunctuationSettingDefaultsToOffAndPersists() throws {
        let suiteName = "TeaASRClientTests.StripTrailingPunctuation.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let settings = Settings(defaults: defaults)
        XCTAssertFalse(settings.stripTrailingPunctuation)

        settings.stripTrailingPunctuation = true
        XCTAssertTrue(Settings(defaults: defaults).stripTrailingPunctuation)
    }

    func testTranscriptProcessorDefaultInitReadsLiveSettingValue() throws {
        let suiteName = "TeaASRClientTests.StripTrailingPunctuation.Live.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let settings = Settings(defaults: defaults)

        let processor = TranscriptProcessor(
            isTrailingPunctuationStripEnabled: { settings.stripTrailingPunctuation }
        )

        let beforeToggle = processor.process(rawTranscript: "今天天氣很好。")
        XCTAssertEqual(beforeToggle.cleanedText, "今天天氣很好。")

        settings.stripTrailingPunctuation = true

        let afterToggle = processor.process(rawTranscript: "今天天氣很好。")
        XCTAssertEqual(afterToggle.cleanedText, "今天天氣很好")
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
