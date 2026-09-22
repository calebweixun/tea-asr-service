import AppKit

/// Semantic system colours keep the settings control legible in both appearances
/// and make it follow the user's accent-colour settings automatically.
final class AudioLevelBarView: NSView {
    /// Already on the 0…1 dBFS display scale produced by `AudioLevelScale`.
    var sample: AudioLevelSample = .zero {
        didSet {
            guard oldValue != sample else { return }
            // Only the moving part of the bar is invalidated.  Marking the
            // whole view dirty ~30 times a second would redraw the track, the
            // border and the status text for nothing.
            setNeedsDisplay(Self.invalidationRect(
                from: oldValue,
                to: sample,
                in: trackRect
            ))
        }
    }

    var state: AudioLevelMonitorState = .idle {
        didSet {
            guard oldValue != state else { return }
            needsDisplay = true
        }
    }

    override var intrinsicContentSize: NSSize {
        NSSize(width: 220, height: 18)
    }

    /// The rectangle a level change can possibly affect: everything between
    /// the smaller and the larger of the two fills, plus both peak markers.
    static func invalidationRect(
        from old: AudioLevelSample,
        to new: AudioLevelSample,
        in trackRect: NSRect
    ) -> NSRect {
        guard trackRect.width > 0, trackRect.height > 0 else { return trackRect }
        func fillX(_ value: Float) -> CGFloat {
            trackRect.minX + trackRect.width * CGFloat(max(0, min(1, value)))
        }
        let xs = [fillX(old.rms), fillX(new.rms), fillX(old.peak), fillX(new.peak)]
        let minX = (xs.min() ?? trackRect.minX) - 2
        let maxX = (xs.max() ?? trackRect.maxX) + 2
        return NSRect(
            x: max(trackRect.minX, minX),
            y: trackRect.minY,
            width: min(trackRect.maxX, maxX) - max(trackRect.minX, minX),
            height: trackRect.height
        )
    }

    private var trackRect: NSRect {
        bounds.insetBy(dx: 1, dy: max(1, (bounds.height - 12) / 2))
    }

    func setLevel(_ sample: AudioLevelSample) {
        self.sample = sample
    }

    func setState(_ state: AudioLevelMonitorState) {
        self.state = state
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)

        let trackRect = self.trackRect
        guard trackRect.width > 0, trackRect.height > 0 else { return }
        let radius = min(trackRect.height / 2, 4)
        let trackPath = NSBezierPath(
            roundedRect: trackRect,
            xRadius: radius,
            yRadius: radius
        )

        NSColor.controlBackgroundColor.setFill()
        trackPath.fill()

        if case .monitoring = state {
            let normalizedRMS = CGFloat(max(0, min(1, sample.rms)))
            let fillRect = NSRect(
                x: trackRect.minX,
                y: trackRect.minY,
                width: trackRect.width * normalizedRMS,
                height: trackRect.height
            )
            if fillRect.width > 0 {
                NSColor.controlAccentColor.setFill()
                NSBezierPath(
                    roundedRect: fillRect,
                    xRadius: radius,
                    yRadius: radius
                ).fill()
            }

            let normalizedPeak = CGFloat(max(0, min(1, sample.peak)))
            let peakX = trackRect.minX + trackRect.width * normalizedPeak
            NSColor.controlAccentColor.withAlphaComponent(0.85).setFill()
            NSBezierPath(
                rect: NSRect(
                    x: max(trackRect.minX, min(trackRect.maxX - 1, peakX - 1)),
                    y: trackRect.minY,
                    width: 2,
                    height: trackRect.height
                )
            ).fill()
        }

        NSColor.separatorColor.setStroke()
        trackPath.lineWidth = 1
        trackPath.stroke()

        guard let title = statusTitle else { return }
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
            .foregroundColor: NSColor.secondaryLabelColor
        ]
        let textSize = title.size(withAttributes: attributes)
        let textRect = NSRect(
            x: bounds.midX - textSize.width / 2,
            y: bounds.midY - textSize.height / 2,
            width: textSize.width,
            height: textSize.height
        )
        title.draw(in: textRect, withAttributes: attributes)
    }

    private var statusTitle: String? {
        switch state {
        case .idle:
            return nil
        case .permissionRequired:
            return "需要麥克風權限"
        case .permissionDenied:
            return "麥克風權限遭拒"
        case .starting:
            return "正在開啟輸入裝置…"
        case .monitoring:
            return nil
        case .noData:
            return "沒有收到音訊資料"
        case .failed(let reason):
            return reason
        }
    }
}
