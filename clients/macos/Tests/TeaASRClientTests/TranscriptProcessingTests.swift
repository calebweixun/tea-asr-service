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

    private struct ReplacementRule: TranscriptProcessingRule {
        let identifier: String
        let from: String
        let to: String

        func apply(to text: String) -> String {
            text.replacingOccurrences(of: from, with: to)
        }
    }
}
