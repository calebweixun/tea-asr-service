import Foundation
import XCTest
@testable import TeaASRClient

final class AudioFlowTests: XCTestCase {
    func testPreRollBufferIsBoundedAndDoesNotEvictOlderAudio() {
        var buffer = AudioPreRollBuffer(capacity: 2)
        let first = Data([1, 2])
        let second = Data([3, 4])
        let third = Data([5, 6])

        XCTAssertTrue(buffer.append(first))
        XCTAssertTrue(buffer.append(second))
        XCTAssertFalse(buffer.append(third))
        XCTAssertEqual(buffer.count, 2)
        XCTAssertEqual(buffer.removeFirst(), first)
        XCTAssertEqual(buffer.removeFirst(), second)
        XCTAssertNil(buffer.removeFirst())
    }

    func testPreRollBufferRejectsAllFramesWhenCapacityIsZero() {
        var buffer = AudioPreRollBuffer(capacity: 0)

        XCTAssertFalse(buffer.append(Data(repeating: 0, count: 3_200)))
        XCTAssertEqual(buffer.count, 0)
    }

    func testSessionGenerationRejectsCallbacksFromAnOlderConnection() {
        var generations = SessionGeneration()
        let first = generations.begin()

        XCTAssertTrue(generations.accepts(first))

        generations.invalidate()
        let second = generations.begin()

        XCTAssertFalse(generations.accepts(first))
        XCTAssertTrue(generations.accepts(second))
    }

    func testPreRollFramesRemainInTimelineOrder() {
        var buffer = AudioPreRollBuffer(capacity: 3)
        let frames = [
            Data(repeating: 1, count: Wire.frameSamples * 2),
            Data(repeating: 2, count: Wire.frameSamples * 2),
            Data(repeating: 3, count: Wire.frameSamples * 2),
        ]

        frames.forEach { XCTAssertTrue(buffer.append($0)) }

        XCTAssertEqual(buffer.removeFirst(), frames[0])
        XCTAssertEqual(buffer.removeFirst(), frames[1])
        XCTAssertEqual(buffer.removeFirst(), frames[2])
        XCTAssertNil(buffer.removeFirst())
    }

    func testMeetingListeningStateTellsUserToSpeak() {
        let state = ASRClient.State.listening(protocolVersion: "1.1", preview: true)

        XCTAssertEqual(
            MeetingSessionPresentation.status(for: state),
            "聆聽中，可以開始說話"
        )
    }

    func testMeetingLoadingStateExplainsAudioIsBuffered() {
        XCTAssertEqual(
            MeetingSessionPresentation.status(for: .loadingModel),
            "模型載入中…音訊暫存中"
        )
    }
}
