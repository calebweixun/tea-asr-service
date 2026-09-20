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

/// Removes a single trailing full-stop-class character from the end of the
/// text. Interior punctuation, and any other trailing mark such as "？" or
/// "！", are left untouched.
///
/// Scope, deliberately narrow (see `clients/macos/README.md`, "文字處理邊界"):
/// - Only a *period* is removed. The user's report was specifically about
///   the model appending a sentence-final full stop; question marks and
///   exclamation marks carry intonation the user did not ask to lose, so a
///   trailing "？"/"！" is never touched by this rule.
/// - Three characters count as a period: the CJK ideographic full stop "。"
///   (U+3002), the full-width Latin full stop "．" (U+FF0E), and the ASCII
///   period "." (U+002E). Any other trailing character — including
///   whitespace — leaves the text unchanged, so this rule looks past
///   trailing whitespace to find the last non-whitespace character.
/// - A decimal point ("3.14") or a common abbreviation ("Inc.") must never
///   be stripped. Both are a single ASCII/full-width period that is
///   indistinguishable from a sentence-final period by position alone, so
///   two local, deterministic guards run before removing one:
///     1. the character immediately before it is not a digit (protects any
///        digit-period ending, not only a period followed by more digits —
///        a conservative choice that never mistakes a decimal for a full
///        stop, at the cost of also protecting a rarer digit-ending
///        sentence such as "編號 5。"), and
///     2. for the ASCII period specifically, the run of ASCII letters
///        immediately before it, compared case-insensitively, is not in a
///        small fixed abbreviation list (`TrailingPeriodStripRule.abbreviations`).
///   Both guards are local string inspection: no dictionary, network call,
///   or model inference, matching the "no cloud formatter" constraint.
struct TrailingPeriodStripRule: TranscriptProcessingRule {
    let identifier = "stripTrailingPunctuation"

    /// Common English abbreviations that legitimately end a sentence with a
    /// single ASCII period. Deliberately small, fixed, and single-token
    /// (no internal "." such as "u.s") so the lookback stays a simple
    /// contiguous run of ASCII letters.
    static let abbreviations: Set<String> = [
        "inc", "ltd", "co", "corp", "llc", "etc",
        "mr", "mrs", "ms", "dr", "st", "jr", "sr",
        "vs", "eg", "ie", "no", "vol", "fig", "approx"
    ]

    private static let fullStops: Set<Character> = ["\u{3002}", "\u{FF0E}", "."]

    func apply(to text: String) -> String {
        guard let lastIndex = text.indices.last(where: { !text[$0].isWhitespace }) else {
            // Empty, or whitespace-only: nothing to do.
            return text
        }
        let lastChar = text[lastIndex]
        guard Self.fullStops.contains(lastChar) else { return text }

        if lastIndex > text.startIndex {
            let precedingIndex = text.index(before: lastIndex)
            let precedingChar = text[precedingIndex]
            if precedingChar.isNumber {
                // Guard 1: a digit immediately before the mark — protects a
                // decimal point regardless of what follows it.
                return text
            }
            if lastChar == "." {
                let word = Self.trailingASCIILetters(before: lastIndex, in: text)
                if !word.isEmpty, Self.abbreviations.contains(word.lowercased()) {
                    // Guard 2: a known abbreviation such as "Inc.".
                    return text
                }
            }
        }

        var result = text
        result.remove(at: lastIndex)
        return result
    }

    private static func trailingASCIILetters(before index: String.Index, in text: String) -> String {
        var start = index
        while start > text.startIndex {
            let prev = text.index(before: start)
            let character = text[prev]
            guard character.isASCII, character.isLetter else { break }
            start = prev
        }
        return String(text[start..<index])
    }
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

    /// Re-evaluated on every `process` call rather than baked in once at
    /// construction time. `TranscriptEventProcessor` is built once and kept
    /// for the app's lifetime (see `AppController`), so if this were decided
    /// only in `init`, toggling the setting would need no changes to that
    /// file at all — but it also would not take effect until the app
    /// restarted. Re-checking per call lets the setting apply to the very
    /// next transcript instead.
    let isTrailingPunctuationStripEnabled: () -> Bool

    /// The default pipeline is intentionally a strict no-op: `cleaningRules`
    /// and `pasteRules` default to empty, and the trailing-punctuation rule
    /// defaults to the user setting, which itself defaults to off.
    init(
        cleaningRules: [any TranscriptProcessingRule] = [],
        pasteRules: [any TranscriptProcessingRule] = [],
        isTrailingPunctuationStripEnabled: @escaping () -> Bool = { Settings().stripTrailingPunctuation }
    ) {
        self.cleaningRules = cleaningRules
        self.pasteRules = pasteRules
        self.isTrailingPunctuationStripEnabled = isTrailingPunctuationStripEnabled
    }

    func process(rawTranscript: String) -> TranscriptProcessingResult {
        var appliedSteps: [String] = []
        var effectiveCleaningRules = cleaningRules
        if isTrailingPunctuationStripEnabled() {
            effectiveCleaningRules.append(TrailingPeriodStripRule())
        }
        let cleanedText = apply(
            rawTranscript,
            rules: effectiveCleaningRules,
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
