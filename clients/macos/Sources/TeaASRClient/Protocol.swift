import Foundation

/// Wire types from docs/04-api.md. Only the v0.1 subset the client needs.
enum Wire {
    static let sampleRate = 16_000
    static let frameSamples = 1_600          // 100 ms
    static let maxFramePCMBytes = 6_400      // the contract's hard limit
    static let headerBytes = 16

    struct Hello: Decodable {
        let protocolVersion: String
        let modelState: String

        enum CodingKeys: String, CodingKey {
            case protocolVersion = "protocol_version"
            case modelState = "model_state"
        }
    }

    struct SessionStarted: Decodable {
        let sessionId: String
        let profile: String
        let transcriptMode: String
        let sendUntilSample: Int

        enum CodingKeys: String, CodingKey {
            case sessionId = "session_id"
            case profile
            case transcriptMode = "transcript_mode"
            case sendUntilSample = "send_until_sample"
        }
    }

    struct FlowControl: Decodable {
        let sendUntilSample: Int

        enum CodingKeys: String, CodingKey {
            case sendUntilSample = "send_until_sample"
        }
    }

    struct Transcript: Decodable {
        let segmentId: String
        let segmentIndex: Int
        let revision: Int
        let startSample: Int
        let endSample: Int
        let text: String
        let warnings: [String]?

        enum CodingKeys: String, CodingKey {
            case segmentId = "segment_id"
            case segmentIndex = "segment_index"
            case revision
            case startSample = "start_sample"
            case endSample = "end_sample"
            case text
            case warnings
        }
    }

    struct SegmentTerminal: Decodable {
        let segmentId: String
        let segmentIndex: Int
        let reason: String?
        let code: String?
        let message: String?

        enum CodingKeys: String, CodingKey {
            case segmentId = "segment_id"
            case segmentIndex = "segment_index"
            case reason
            case code
            case message
        }
    }

    struct ErrorEvent: Decodable {
        let code: String
        let message: String
        let retryable: Bool
    }

    /// Binary frame: 16-byte header (uint64 LE seq, uint64 LE start_sample) + PCM.
    static func frame(seq: UInt64, startSample: UInt64, pcm: Data) -> Data {
        var out = Data(capacity: headerBytes + pcm.count)
        withUnsafeBytes(of: seq.littleEndian) { out.append(contentsOf: $0) }
        withUnsafeBytes(of: startSample.littleEndian) { out.append(contentsOf: $0) }
        out.append(pcm)
        return out
    }
}
