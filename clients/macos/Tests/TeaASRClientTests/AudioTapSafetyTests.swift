import AVFoundation
import AVAudioEngineTapShim
import CoreAudio
import Foundation
import XCTest
@testable import TeaASRClient

final class AudioTapSafetyTests: XCTestCase {
    private let validFormat = AudioInputFormatSignature(
        sampleRate: 48_000,
        channelCount: 2,
        commonFormat: .pcmFormatFloat32
    )

    func testZeroHertzCurrentFormatIsRejectedBeforeTapInstallation() {
        let invalid = AudioInputFormatSignature(
            sampleRate: 0,
            channelCount: 2,
            commonFormat: .pcmFormatFloat32
        )
        var tapAttempts = 0

        XCTAssertThrowsError(
            try AudioInputTapInstallationPolicy.installIfCurrentFormatIsValid(
                preparedFor: invalid,
                current: invalid,
                channelPolicy: .mixdown,
                install: { tapAttempts += 1 }
            )
        ) { error in
            XCTAssertEqual(error as? AudioInputFormatError, .invalidSampleRate(0))
            XCTAssertTrue(error.localizedDescription.contains("無效"))
        }
        XCTAssertEqual(tapAttempts, 0)
    }

    func testZeroChannelCurrentFormatIsRejectedBeforeTapInstallation() {
        let invalid = AudioInputFormatSignature(
            sampleRate: 48_000,
            channelCount: 0,
            commonFormat: .pcmFormatFloat32
        )
        var tapAttempts = 0

        XCTAssertThrowsError(
            try AudioInputTapInstallationPolicy.installIfCurrentFormatIsValid(
                preparedFor: invalid,
                current: invalid,
                channelPolicy: .mixdown,
                install: { tapAttempts += 1 }
            )
        ) { error in
            XCTAssertEqual(error as? AudioInputFormatError, .noInputChannels)
            XCTAssertTrue(error.localizedDescription.contains("零個"))
        }
        XCTAssertEqual(tapAttempts, 0)
    }

    func testFormatChangeDuringOpeningFailsBeforeTapInstallation() {
        let changed = AudioInputFormatSignature(
            sampleRate: 44_100,
            channelCount: 2,
            commonFormat: .pcmFormatFloat32
        )
        var tapAttempts = 0

        XCTAssertThrowsError(
            try AudioInputTapInstallationPolicy.installIfCurrentFormatIsValid(
                preparedFor: validFormat,
                current: changed,
                channelPolicy: .mixdown,
                install: { tapAttempts += 1 }
            )
        ) { error in
            guard case .changedDuringTapSetup(let prepared, let current) = error as? AudioInputFormatError else {
                return XCTFail("expected a format-change failure, got \(error)")
            }
            XCTAssertEqual(prepared, self.validFormat)
            XCTAssertEqual(current, changed)
            XCTAssertTrue(error.localizedDescription.contains("變更"))
        }
        XCTAssertEqual(tapAttempts, 0)
    }

    func testReconfigurationDoesNotOpenAnotherUIDAsFallback() throws {
        enum ReconfigureError: Error { case deviceChanged }

        let selected = AudioInputSourceLifecycle.Configuration(
            deviceUID: "bluetooth-headset",
            channelPolicy: .mixdown
        )
        let requested = AudioInputSourceLifecycle.Configuration(
            deviceUID: "built-in-microphone",
            channelPolicy: .mixdown
        )
        var opened: [AudioInputSourceLifecycle.Configuration] = []
        let source = AudioInputSourceLifecycle(
            open: { opened.append($0) },
            close: {},
            reconfigure: { _ in throw ReconfigureError.deviceChanged }
        )
        let consumer = try source.attach(configuration: selected) { _ in }

        XCTAssertThrowsError(try source.update(consumer, configuration: requested))
        XCTAssertTrue(opened.allSatisfy { $0.deviceUID != requested.deviceUID })
        XCTAssertEqual(source.activeConfiguration, selected)
        try source.detach(consumer)
    }

    func testObjectiveCShimTranslatesAnNSExceptionIntoNSError() {
        let fake = ExceptionRaisingAudioNode()
        let node = Unmanaged<AVAudioInputNode>.fromOpaque(
            Unmanaged.passUnretained(fake).toOpaque()
        ).takeUnretainedValue()
        var error: NSError?

        let installed = TEAInstallAudioInputTap(
            node,
            512,
            nil,
            { _, _ in },
            &error
        )

        XCTAssertFalse(installed)
        XCTAssertEqual(error?.domain, "com.tea-asr.audio-engine-tap-shim")
        XCTAssertTrue(error?.localizedDescription.contains("synthetic tap failure") == true)
    }
}

private final class ExceptionRaisingAudioNode: NSObject {
    @objc(installTapOnBus:bufferSize:format:block:)
    func installTap(
        onBus: AVAudioNodeBus,
        bufferSize: AVAudioFrameCount,
        format: AVAudioFormat?,
        block: @escaping (AVAudioPCMBuffer, AVAudioTime?) -> Void
    ) {
        NSException(
            name: .genericException,
            reason: "synthetic tap failure",
            userInfo: nil
        ).raise()
    }
}
