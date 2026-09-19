import Foundation

/// FIFO used before the websocket session has received `session.started`.
/// It is deliberately bounded: a server that never becomes ready must not
/// allow microphone data to grow memory without limit.
struct AudioPreRollBuffer {
    let capacity: Int
    private(set) var frames: [Data] = []

    init(capacity: Int) {
        self.capacity = max(0, capacity)
    }

    var count: Int { frames.count }

    @discardableResult
    mutating func append(_ frame: Data) -> Bool {
        guard frames.count < capacity else { return false }
        frames.append(frame)
        return true
    }

    mutating func removeFirst() -> Data? {
        guard !frames.isEmpty else { return nil }
        return frames.removeFirst()
    }

    mutating func removeAll() {
        frames.removeAll(keepingCapacity: true)
    }
}
