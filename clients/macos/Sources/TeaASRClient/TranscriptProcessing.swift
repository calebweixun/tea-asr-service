import Foundation

/// A single, local and deterministic text transformation.
///
/// Rules are deliberately supplied by the client rather than inferred from
/// server output.  The production default has no rules, which makes the
/// processing boundary a strict no-op until a user-visible rule is explicitly
/// opted in.
protocol TranscriptProcessingRule {
    var identifier: String { get }
    func apply(to text: String) -> String
}

/// The immutable text values produced for one server final event.
///
/// `rawTranscript` is the exact server `text` field.  `cleanedText` is the
/// presentation value and `pasteText` is the value handed to the focused app.
/// Keeping all three values makes it possible to add local rules later without
/// changing server metadata or hiding what was actually recognized.
struct TranscriptProcessingResult: Equatable {
    let rawTranscript: String
    let cleanedText: String
    let pasteText: String
    let appliedSteps: [String]
}

/// Wire metadata that must survive client-side text processing unchanged.
/// `timestamp` is the client presentation time derived from the immutable
/// sample offset; the wire segment and sample/revision fields are copied
/// without normalization or re-numbering.
struct TranscriptSegmentMetadata: Equatable {
    let segmentID: String
    let segmentIndex: Int
    let revision: Int
    let startSample: Int
    let endSample: Int
    let timestampQuality: String
    let timestamp: Date
    let warnings: [String]?

    init(final transcript: Wire.Transcript, timestamp: Date) {
        segmentID = transcript.segmentId
        segmentIndex = transcript.segmentIndex
        revision = transcript.revision
        startSample = transcript.startSample
        endSample = transcript.endSample
        timestampQuality = transcript.timestampQuality
        self.timestamp = timestamp
        warnings = transcript.warnings
    }
}

/// A processed final paired with its immutable wire metadata.
struct ProcessedTranscript: Equatable {
    let metadata: TranscriptSegmentMetadata
    let rawTranscript: String
    let cleanedText: String
    let pasteText: String
    let appliedSteps: [String]
}

/// Applies deterministic client-side rules in a declared order.
///
/// Cleaning rules run first and produce `cleanedText`; paste rules then run on
/// that value and produce `pasteText`.  A rule is recorded only when it
/// changes the value, so `appliedSteps` describes the actual transformation
/// rather than a guessed or merely configured result.
struct TranscriptProcessor {
    let cleaningRules: [any TranscriptProcessingRule]
    let pasteRules: [any TranscriptProcessingRule]

    /// The default pipeline is intentionally a strict no-op.
    init(
        cleaningRules: [any TranscriptProcessingRule] = [],
        pasteRules: [any TranscriptProcessingRule] = []
    ) {
        self.cleaningRules = cleaningRules
        self.pasteRules = pasteRules
    }

    func process(rawTranscript: String) -> TranscriptProcessingResult {
        var appliedSteps: [String] = []
        let cleanedText = apply(
            rawTranscript,
            rules: cleaningRules,
            appliedSteps: &appliedSteps
        )
        let pasteText = apply(
            cleanedText,
            rules: pasteRules,
            appliedSteps: &appliedSteps
        )
        return TranscriptProcessingResult(
            rawTranscript: rawTranscript,
            cleanedText: cleanedText,
            pasteText: pasteText,
            appliedSteps: appliedSteps
        )
    }

    func process(final transcript: Wire.Transcript, timestamp: Date) -> ProcessedTranscript {
        let result = process(rawTranscript: transcript.text)
        return ProcessedTranscript(
            metadata: TranscriptSegmentMetadata(final: transcript, timestamp: timestamp),
            rawTranscript: result.rawTranscript,
            cleanedText: result.cleanedText,
            pasteText: result.pasteText,
            appliedSteps: result.appliedSteps
        )
    }

    private func apply(
        _ input: String,
        rules: [any TranscriptProcessingRule],
        appliedSteps: inout [String]
    ) -> String {
        var output = input
        for rule in rules {
            let next = rule.apply(to: output)
            if next != output {
                appliedSteps.append(rule.identifier)
            }
            output = next
        }
        return output
    }
}

/// Routes only server final events into the processing pipeline.
///
/// Partials intentionally return `nil`: they remain a preview concern and
/// cannot enter cleaned/paste text or mutate transcript metadata.
enum TranscriptEvent {
    case partial(Wire.Transcript)
    case final(Wire.Transcript)
}

struct TranscriptEventProcessor {
    let processor: TranscriptProcessor

    init(processor: TranscriptProcessor = TranscriptProcessor()) {
        self.processor = processor
    }

    func process(_ event: TranscriptEvent, timestamp: Date) -> ProcessedTranscript? {
        guard case .final(let transcript) = event else { return nil }
        return processor.process(final: transcript, timestamp: timestamp)
    }
}

/// Keeps presentation and insertion policy explicit when their text differs.
/// The unified UI and status-menu preview show `cleanedText`; only the
/// clipboard/text-injection boundary consumes `pasteText`.
enum TranscriptOutputPolicy {
    static func presentationText(from processed: ProcessedTranscript) -> String {
        processed.cleanedText
    }

    static func insertionText(from processed: ProcessedTranscript) -> String {
        processed.pasteText
    }
}
