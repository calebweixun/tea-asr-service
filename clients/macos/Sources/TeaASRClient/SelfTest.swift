import Foundation

/// Streams a WAV through the real client path and prints what comes back.
///
/// This exercises the parts that can actually be wrong — the wire framing, the
/// flow window, the session state machine — without needing microphone or
/// accessibility permissions, so the client can be checked on a fresh machine.
enum SelfTest {
    static func run(
        path: String, realtime: Bool, forcePreview: Bool, urlOverride: String?
    ) -> Never {
        let url = URL(fileURLWithPath: path)
        let pcm: Data
        do {
            pcm = try readWAV(url)
        } catch {
            FileHandle.standardError.write(Data("讀取 WAV 失敗：\(error.localizedDescription)\n".utf8))
            exit(2)
        }

        var settings = Settings()
        if let urlOverride, let parsed = URL(string: urlOverride) {
            settings.overrideStreamURL = parsed
        }
        print("服務：\(settings.streamURL.absoluteString)")
        print("音訊：\(String(format: "%.2f", Double(pcm.count / 2) / 16_000)) 秒\n")

        // Callbacks are delivered on the main queue, so the main thread must
        // keep pumping its run loop rather than blocking on a semaphore.
        setvbuf(stdout, nil, _IOLBF, 0)
        let client = ASRClient(settings: settings)
        var finals = 0
        var failure: String?
        var finished = false

        client.onState = { state in
            switch state {
            case .listening(_, let preview):
                print("session 已建立\(preview ? "（含串流預覽）" : "")")
            case .failed(let message):
                failure = message
                finished = true
            case .idle:
                finished = true
            case .connecting:
                break
            case .loadingModel:
                print("模型載入中，等待服務…")
            }
        }
        client.onPartial = { item in
            print("  … \(item.text)")
        }
        client.onFinal = { item in
            finals += 1
            let start = Double(item.startSample) / 16_000
            let end = Double(item.endSample) / 16_000
            print(String(format: "  ✓ [%.2f-%.2f] %@", start, end, item.text))
        }
        client.onNotice = { print("  ! \($0)") }
        client.onTimelineGap = { reason in
            // Mirrors the app: a gap is recoverable, so start a fresh session.
            print("  ⟲ 時間軸缺口（\(reason)），重新建立 session")
            client.connect(wantsPreview: forcePreview)
        }

        client.connect(wantsPreview: forcePreview)

        DispatchQueue.global().async {
            // Give the handshake a moment before the first frame.
            Thread.sleep(forTimeInterval: 1.0)
            let frameBytes = Wire.frameSamples * 2
            var offset = 0
            while offset < pcm.count {
                let end = min(offset + frameBytes, pcm.count)
                client.send(pcm: pcm.subdata(in: offset..<end))
                offset = end
                if realtime {
                    Thread.sleep(forTimeInterval: Double(frameBytes / 2) / 16_000)
                }
            }
            client.stop()
        }

        let deadline = Date().addingTimeInterval(300)
        while !finished, Date() < deadline {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        if !finished {
            failure = "逾時：300 秒內沒有收到 session.stopped。"
        }

        if let failure {
            FileHandle.standardError.write(Data("\n失敗：\(failure)\n".utf8))
            exit(1)
        }
        print("\n共 \(finals) 段定稿。")
        exit(finals > 0 ? 0 : 1)
    }

    /// Minimal 16 kHz mono PCM16 WAV reader; anything else is rejected rather
    /// than silently misinterpreted.
    private static func readWAV(_ url: URL) throws -> Data {
        let data = try Data(contentsOf: url)
        guard data.count > 44, data.prefix(4) == Data("RIFF".utf8) else {
            throw Failure("不是 RIFF/WAV 檔")
        }
        var cursor = 12
        var format: (channels: Int, rate: Int, bits: Int)?
        while cursor + 8 <= data.count {
            let id = data.subdata(in: cursor..<(cursor + 4))
            let size = Int(le32(data, cursor + 4))
            let body = cursor + 8
            guard body + size <= data.count else { break }
            if id == Data("fmt ".utf8), size >= 16 {
                format = (
                    channels: Int(le16(data, body + 2)),
                    rate: Int(le32(data, body + 4)),
                    bits: Int(le16(data, body + 14))
                )
            } else if id == Data("data".utf8) {
                guard let format else { throw Failure("WAV 缺少 fmt chunk") }
                guard format.channels == 1, format.rate == 16_000, format.bits == 16 else {
                    throw Failure("WAV 必須是 16 kHz mono PCM16，實際為 \(format)")
                }
                return data.subdata(in: body..<(body + size))
            }
            cursor = body + size + (size % 2)
        }
        throw Failure("WAV 缺少 data chunk")
    }

    private static func le16(_ data: Data, _ offset: Int) -> UInt16 {
        UInt16(data[offset]) | (UInt16(data[offset + 1]) << 8)
    }

    private static func le32(_ data: Data, _ offset: Int) -> UInt32 {
        var value: UInt32 = 0
        for index in 0..<4 {
            value |= UInt32(data[offset + index]) << (8 * index)
        }
        return value
    }

    private struct Failure: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
}
