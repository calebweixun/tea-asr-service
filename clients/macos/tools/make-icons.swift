import AppKit
import CoreGraphics
import Foundation

// Draws the TEA ASR marks. A tea cup carries the product name; what rises from
// it says what the product does. Run: swift make-icons.swift <outputDirectory>

enum Variant: String, CaseIterable {
    case waves      // steam drawn as sound waves
    case mic        // a microphone rising out of the cup
    case caption    // caption lines rising out of the cup
    case wenSteam   // 文 rising from the cup as the steam itself
    case wenInCup   // 文 sitting inside the cup: the tea is text
    case wenWaves   // 文 held inside two arcs: sound, but unmistakably text
    case wenRim     // the cup's rim doubles as the horizontal stroke of 文
    case wenCup     // 文 becomes the cup: 丶 is a waveform, 一 the rim, 乂 the bowl
    case wenSolid   // a solid cup whose specular highlight traces 文
    case cupTilt    // the cup seen from above at an angle; the tea carries text
    case leafVoice  // a tea leaf whose midrib is a waveform and veins are text
}

struct Palette {
    static let teaDark = CGColor(red: 0.11, green: 0.25, blue: 0.21, alpha: 1)
    static let teaMid = CGColor(red: 0.17, green: 0.43, blue: 0.33, alpha: 1)
    static let cream = CGColor(red: 0.98, green: 0.96, blue: 0.91, alpha: 1)
    static let brew = CGColor(red: 0.85, green: 0.62, blue: 0.27, alpha: 1)
}

func context(size: Int) -> CGContext {
    let space = CGColorSpaceCreateDeviceRGB()
    guard
        let ctx = CGContext(
            data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
            space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
    else { fatalError("cannot create context") }
    ctx.setAllowsAntialiasing(true)
    ctx.interpolationQuality = .high
    return ctx
}

/// Cup body, handle and saucer. `u` is one unit of the 1000-unit design grid.
func drawCup(_ ctx: CGContext, u: CGFloat, stroke: CGColor, fill: CGColor?, lineWidth: CGFloat) {
    // Body: a tapered cup, wider at the rim.
    let rimY = 470 * u
    let baseY = 250 * u
    let rimHalf = 210 * u
    let baseHalf = 150 * u
    let centre = 500 * u

    let body = CGMutablePath()
    body.move(to: CGPoint(x: centre - rimHalf, y: rimY))
    body.addLine(to: CGPoint(x: centre - baseHalf, y: baseY + 26 * u))
    body.addQuadCurve(
        to: CGPoint(x: centre - baseHalf + 40 * u, y: baseY),
        control: CGPoint(x: centre - baseHalf, y: baseY)
    )
    body.addLine(to: CGPoint(x: centre + baseHalf - 40 * u, y: baseY))
    body.addQuadCurve(
        to: CGPoint(x: centre + baseHalf, y: baseY + 26 * u),
        control: CGPoint(x: centre + baseHalf, y: baseY)
    )
    body.addLine(to: CGPoint(x: centre + rimHalf, y: rimY))
    body.closeSubpath()

    if let fill {
        ctx.setFillColor(fill)
        ctx.addPath(body)
        ctx.fillPath()
    }
    ctx.setStrokeColor(stroke)
    ctx.setLineWidth(lineWidth)
    ctx.setLineJoin(.round)
    ctx.setLineCap(.round)
    ctx.addPath(body)
    ctx.strokePath()

    // Handle: an arc that bulges to the right of the body, not through it.
    let handle = CGMutablePath()
    handle.addArc(
        center: CGPoint(x: centre + 186 * u, y: 372 * u),
        radius: 92 * u,
        startAngle: -.pi / 2.4, endAngle: .pi / 2.4, clockwise: false
    )
    ctx.addPath(handle)
    ctx.strokePath()

    // Saucer
    let saucer = CGMutablePath()
    saucer.move(to: CGPoint(x: centre - 312 * u, y: 206 * u))
    saucer.addQuadCurve(
        to: CGPoint(x: centre + 312 * u, y: 206 * u),
        control: CGPoint(x: centre, y: 112 * u)
    )
    ctx.addPath(saucer)
    ctx.strokePath()
}

func drawWaves(_ ctx: CGContext, u: CGFloat, stroke: CGColor, lineWidth: CGFloat) {
    ctx.setStrokeColor(stroke)
    ctx.setLineCap(.round)
    // Concentric arcs above the rim: steam that reads as sound.
    for (index, radius) in [92, 168, 244].enumerated() {
        let r = CGFloat(radius) * u
        let path = CGMutablePath()
        path.addArc(
            center: CGPoint(x: 500 * u, y: 500 * u),
            radius: r,
            startAngle: .pi * 0.18, endAngle: .pi * 0.82, clockwise: false
        )
        ctx.setLineWidth(lineWidth * (1.0 - CGFloat(index) * 0.13))
        ctx.addPath(path)
        ctx.strokePath()
    }
}

func drawMic(_ ctx: CGContext, u: CGFloat, stroke: CGColor, fill: CGColor, lineWidth: CGFloat) {
    let centre = 500 * u
    // Capsule
    let capsule = CGPath(
        roundedRect: CGRect(x: centre - 62 * u, y: 592 * u, width: 124 * u, height: 214 * u),
        cornerWidth: 62 * u, cornerHeight: 62 * u, transform: nil
    )
    ctx.setFillColor(fill)
    ctx.addPath(capsule)
    ctx.fillPath()
    ctx.setStrokeColor(stroke)
    ctx.setLineWidth(lineWidth)
    ctx.addPath(capsule)
    ctx.strokePath()

    // Cradle
    // Cradle: arms start below the capsule's midpoint, or it reads as ears.
    let cradle = CGMutablePath()
    cradle.addArc(
        center: CGPoint(x: centre, y: 700 * u), radius: 152 * u,
        startAngle: .pi, endAngle: 0, clockwise: true
    )
    ctx.setLineCap(.round)
    ctx.addPath(cradle)
    ctx.strokePath()
    ctx.move(to: CGPoint(x: centre, y: 548 * u))
    ctx.addLine(to: CGPoint(x: centre, y: 486 * u))
    ctx.strokePath()
}

/// Draws 文 centred on `centre` at the given cap height, using a real CJK face
/// so the strokes sit right; hand-drawn strokes never match a font's balance.
func drawWen(_ ctx: CGContext, u: CGFloat, centre: CGPoint, capHeight: CGFloat, color: CGColor) {
    let candidates = ["PingFangTC-Semibold", "STHeitiTC-Medium", "HiraginoSans-W6"]
    var font: CTFont?
    for name in candidates {
        let candidate = CTFontCreateWithName(name as CFString, capHeight, nil)
        if CTFontCopyFamilyName(candidate) as String != ".LastResort" {
            font = candidate
            break
        }
    }
    let resolved = font ?? CTFontCreateUIFontForLanguage(.system, capHeight, "zh-Hant" as CFString)!
    let attributes: [NSAttributedString.Key: Any] = [
        .font: resolved,
        .foregroundColor: NSColor(cgColor: color) ?? .white,
    ]
    let text = NSAttributedString(string: "文", attributes: attributes)
    let line = CTLineCreateWithAttributedString(text)
    let bounds = CTLineGetBoundsWithOptions(line, .useGlyphPathBounds)
    ctx.saveGState()
    ctx.textPosition = CGPoint(
        x: centre.x - bounds.width / 2 - bounds.origin.x,
        y: centre.y - bounds.height / 2 - bounds.origin.y
    )
    CTLineDraw(line, ctx)
    ctx.restoreGState()
    _ = u
}

/// The outline of 文 as a path, so it can be stroked like a catch of light
/// rather than filled like printed type.
func wenPath(capHeight: CGFloat, centre: CGPoint) -> CGPath? {
    let candidates = ["PingFangTC-Semibold", "STHeitiTC-Medium", "HiraginoSans-W6"]
    var font: CTFont?
    for name in candidates {
        let candidate = CTFontCreateWithName(name as CFString, capHeight, nil)
        if CTFontCopyFamilyName(candidate) as String != ".LastResort" {
            font = candidate
            break
        }
    }
    guard let resolved = font else { return nil }
    var glyph = CTFontGetGlyphWithName(resolved, "uni6587" as CFString)
    if glyph == 0 {
        var characters: [UniChar] = Array("文".utf16)
        var glyphs = [CGGlyph](repeating: 0, count: characters.count)
        guard CTFontGetGlyphsForCharacters(resolved, &characters, &glyphs, characters.count) else {
            return nil
        }
        glyph = glyphs[0]
    }
    guard let path = CTFontCreatePathForGlyph(resolved, glyph, nil) else { return nil }
    let bounds = path.boundingBox
    var transform = CGAffineTransform(
        translationX: centre.x - bounds.midX, y: centre.y - bounds.midY
    )
    return path.copy(using: &transform)
}

func drawCaption(_ ctx: CGContext, u: CGFloat, stroke: CGColor, lineWidth: CGFloat) {
    ctx.setStrokeColor(stroke)
    ctx.setLineCap(.round)
    ctx.setLineWidth(lineWidth)
    // A short block of caption lines, the last one clipped: still being written.
    let rows: [(CGFloat, CGFloat)] = [
        (150, 760),
        (190, 660),
        (95, 560),
    ]
    for (halfWidth, y) in rows {
        ctx.move(to: CGPoint(x: (500 - halfWidth) * u, y: y * u))
        ctx.addLine(to: CGPoint(x: (500 + halfWidth) * u, y: y * u))
        ctx.strokePath()
    }
}

func roundedBackground(_ ctx: CGContext, size: CGFloat) {
    // macOS app icons sit on a rounded square with a margin.
    let inset = size * 0.085
    let rect = CGRect(x: inset, y: inset, width: size - inset * 2, height: size - inset * 2)
    let radius = rect.width * 0.2237
    let path = CGPath(
        roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil
    )
    ctx.saveGState()
    ctx.addPath(path)
    ctx.clip()
    let space = CGColorSpaceCreateDeviceRGB()
    let gradient = CGGradient(
        colorsSpace: space, colors: [Palette.teaMid, Palette.teaDark] as CFArray,
        locations: [0, 1]
    )!
    ctx.drawLinearGradient(
        gradient, start: CGPoint(x: 0, y: size), end: CGPoint(x: size, y: 0), options: []
    )
    ctx.restoreGState()
}

/// A small level meter standing in for the 丶 of 文, and for the steam.
func drawWaveform(_ ctx: CGContext, u: CGFloat, baseY: CGFloat, color: CGColor, unit: CGFloat) {
    ctx.setStrokeColor(color)
    ctx.setLineCap(.round)
    ctx.setLineWidth(unit)
    let heights: [CGFloat] = [72, 136, 96]
    let spacing = 96 * u
    let start = 500 * u - spacing
    for (index, height) in heights.enumerated() {
        let x = start + CGFloat(index) * spacing
        ctx.move(to: CGPoint(x: x, y: baseY))
        ctx.addLine(to: CGPoint(x: x, y: baseY + height * u))
        ctx.strokePath()
    }
}

/// 文 redrawn as a cup: the horizontal stroke is the rim, the 乂 is the bowl.
/// Nothing here is decoration laid on a cup — the character *is* the cup.
func drawWenCup(
    _ ctx: CGContext, u: CGFloat, stroke: CGColor, accent: CGColor, lineWidth: CGFloat
) {
    let centre = 500 * u
    let rimY = 500 * u

    ctx.setLineCap(.round)
    ctx.setLineJoin(.round)
    ctx.setLineWidth(lineWidth)

    // 一 — the rim.
    ctx.setStrokeColor(stroke)
    ctx.move(to: CGPoint(x: centre - 232 * u, y: rimY))
    ctx.addLine(to: CGPoint(x: centre + 232 * u, y: rimY))
    ctx.strokePath()

    // 乂 — two strokes that cross low and read as the bowl of the cup.
    let left = CGMutablePath()
    left.move(to: CGPoint(x: centre - 158 * u, y: rimY - 16 * u))
    left.addQuadCurve(
        to: CGPoint(x: centre + 96 * u, y: 286 * u),
        control: CGPoint(x: centre + 4 * u, y: 408 * u)
    )
    ctx.addPath(left)
    ctx.strokePath()

    let right = CGMutablePath()
    right.move(to: CGPoint(x: centre + 158 * u, y: rimY - 16 * u))
    right.addQuadCurve(
        to: CGPoint(x: centre - 96 * u, y: 286 * u),
        control: CGPoint(x: centre - 4 * u, y: 408 * u)
    )
    ctx.addPath(right)
    ctx.strokePath()

    // Handle, so the cup still reads as a cup and not only as a character.
    let handle = CGMutablePath()
    handle.addArc(
        center: CGPoint(x: centre + 244 * u, y: 424 * u), radius: 82 * u,
        startAngle: -.pi / 2.6, endAngle: .pi / 2.6, clockwise: false
    )
    ctx.addPath(handle)
    ctx.strokePath()

    // Saucer
    let saucer = CGMutablePath()
    saucer.move(to: CGPoint(x: centre - 300 * u, y: 224 * u))
    saucer.addQuadCurve(
        to: CGPoint(x: centre + 300 * u, y: 224 * u),
        control: CGPoint(x: centre, y: 138 * u)
    )
    ctx.addPath(saucer)
    ctx.strokePath()

    // 丶 — the dot, now a level meter rising off the rim.
    drawWaveform(ctx, u: u, baseY: 548 * u, color: accent, unit: lineWidth * 0.92)
}

/// A solid cup lit from the upper left; the specular highlight traces 文.
func drawSolidWenCup(_ ctx: CGContext, u: CGFloat, size: CGFloat, accent: CGColor) {
    let centre = 500 * u
    let rimY = 494 * u
    let baseY = 250 * u
    let rimHalf = 236 * u
    let baseHalf = 150 * u

    let body = CGMutablePath()
    body.move(to: CGPoint(x: centre - rimHalf, y: rimY))
    body.addLine(to: CGPoint(x: centre - baseHalf, y: baseY + 30 * u))
    body.addQuadCurve(
        to: CGPoint(x: centre - baseHalf + 46 * u, y: baseY),
        control: CGPoint(x: centre - baseHalf, y: baseY)
    )
    body.addLine(to: CGPoint(x: centre + baseHalf - 46 * u, y: baseY))
    body.addQuadCurve(
        to: CGPoint(x: centre + baseHalf, y: baseY + 30 * u),
        control: CGPoint(x: centre + baseHalf, y: baseY)
    )
    body.addLine(to: CGPoint(x: centre + rimHalf, y: rimY))
    body.closeSubpath()

    // Volume: a light-to-shadow gradient across the body.
    ctx.saveGState()
    ctx.addPath(body)
    ctx.clip()
    let space = CGColorSpaceCreateDeviceRGB()
    // A dark glazed cup: white light has nothing to say on a white cup.
    let porcelain = CGColor(red: 0.42, green: 0.27, blue: 0.13, alpha: 1)
    let shade = CGColor(red: 0.18, green: 0.11, blue: 0.05, alpha: 1)
    let gradient = CGGradient(
        colorsSpace: space, colors: [porcelain, shade] as CFArray, locations: [0, 1]
    )!
    ctx.drawLinearGradient(
        gradient,
        start: CGPoint(x: centre - rimHalf, y: rimY),
        end: CGPoint(x: centre + rimHalf, y: baseY),
        options: []
    )
    ctx.restoreGState()

    // Handle, solid.
    ctx.setLineCap(.round)
    ctx.setStrokeColor(CGColor(red: 0.33, green: 0.21, blue: 0.10, alpha: 1))
    ctx.setLineWidth(52 * u)
    let handle = CGMutablePath()
    handle.addArc(
        center: CGPoint(x: centre + 226 * u, y: 386 * u), radius: 96 * u,
        startAngle: -.pi / 2.4, endAngle: .pi / 2.4, clockwise: false
    )
    ctx.addPath(handle)
    ctx.strokePath()

    // Tea surface.
    ctx.setFillColor(CGColor(red: 0.88, green: 0.66, blue: 0.31, alpha: 1))
    let surface = CGPath(
        ellipseIn: CGRect(
            x: centre - rimHalf, y: rimY - 34 * u,
            width: rimHalf * 2, height: 66 * u
        ),
        transform: nil
    )
    ctx.addPath(surface)
    ctx.fillPath()

    // The highlight: 文 traced by reflected light, brightest where the light
    // falls. Stroked rather than filled, so it reads as a glint on porcelain
    // and not as a character printed on the cup.
    ctx.saveGState()
    ctx.addPath(body)
    ctx.clip()
    if let glyph = wenPath(capHeight: 232 * u, centre: CGPoint(x: centre, y: 374 * u)) {
        ctx.saveGState()
        ctx.addPath(glyph)
        ctx.setLineWidth(19 * u)
        ctx.setLineJoin(.round)
        ctx.setLineCap(.round)
        ctx.replacePathWithStrokedPath()
        ctx.clip()
        let space = CGColorSpaceCreateDeviceRGB()
        let lit = CGColor(red: 1, green: 1, blue: 1, alpha: 0.98)
        let dim = CGColor(red: 1, green: 1, blue: 1, alpha: 0.34)
        let sheen = CGGradient(
            colorsSpace: space, colors: [lit, dim] as CFArray, locations: [0, 1]
        )!
        ctx.drawLinearGradient(
            sheen,
            start: CGPoint(x: centre - 200 * u, y: 500 * u),
            end: CGPoint(x: centre + 220 * u, y: 250 * u),
            options: [.drawsBeforeStartLocation, .drawsAfterEndLocation]
        )
        ctx.restoreGState()
    }
    // A broad diagonal band of light across the body, which is what makes the
    // traced strokes read as reflection rather than paint.
    let band = CGMutablePath()
    band.move(to: CGPoint(x: centre - 250 * u, y: rimY))
    band.addLine(to: CGPoint(x: centre - 110 * u, y: rimY))
    band.addLine(to: CGPoint(x: centre - 30 * u, y: baseY))
    band.addLine(to: CGPoint(x: centre - 140 * u, y: baseY))
    band.closeSubpath()
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 0.16))
    ctx.addPath(band)
    ctx.fillPath()
    ctx.restoreGState()

    // Saucer
    ctx.setStrokeColor(CGColor(red: 0.96, green: 0.94, blue: 0.89, alpha: 1))
    ctx.setLineWidth(48 * u)
    let saucer = CGMutablePath()
    saucer.move(to: CGPoint(x: centre - 316 * u, y: 208 * u))
    saucer.addQuadCurve(
        to: CGPoint(x: centre + 316 * u, y: 208 * u),
        control: CGPoint(x: centre, y: 118 * u)
    )
    ctx.addPath(saucer)
    ctx.strokePath()

    drawWaveform(ctx, u: u, baseY: 560 * u, color: accent, unit: 46 * u)
    _ = size
}

/// Rounded bar, the unit both the caption lines and the waveform are built from.
func bar(_ ctx: CGContext, from: CGPoint, to: CGPoint, width: CGFloat, color: CGColor) {
    ctx.setStrokeColor(color)
    ctx.setLineCap(.round)
    ctx.setLineWidth(width)
    ctx.move(to: from)
    ctx.addLine(to: to)
    ctx.strokePath()
}

/// A teacup seen from above and to the side, so the tea itself is visible —
/// and what floats on the tea is text. Looking *into* the cup is the point:
/// the product's output lives in the drink, not beside it.
func drawTiltedCup(_ ctx: CGContext, u: CGFloat, cream: CGColor, brew: CGColor, ink: CGColor) {
    let cx = 500 * u
    let rimY = 604 * u
    let rimX = 250 * u
    let rimYr = 92 * u
    let baseY = 306 * u
    let baseX = 146 * u
    let baseYr = 50 * u

    // Handle first, so the body overlaps it where they meet.
    ctx.setStrokeColor(cream)
    ctx.setLineWidth(52 * u)
    ctx.setLineCap(.round)
    let handle = CGMutablePath()
    handle.addArc(
        center: CGPoint(x: cx + 240 * u, y: 486 * u), radius: 100 * u,
        startAngle: -.pi / 2.5, endAngle: .pi / 2.5, clockwise: false
    )
    ctx.addPath(handle)
    ctx.strokePath()

    // The lip: a full ellipse. Everything else hangs from it.
    let lip = CGPath(
        ellipseIn: CGRect(
            x: cx - rimX, y: rimY - rimYr, width: rimX * 2, height: rimYr * 2
        ),
        transform: nil
    )
    ctx.setFillColor(cream)
    ctx.addPath(lip)
    ctx.fillPath()

    // The wall: from the lip's widest points down to the base, closed across
    // the front of the base and back along the front of the lip.
    let wall = CGMutablePath()
    wall.move(to: CGPoint(x: cx - rimX, y: rimY))
    wall.addCurve(
        to: CGPoint(x: cx - baseX, y: baseY),
        control1: CGPoint(x: cx - rimX + 14 * u, y: rimY - 160 * u),
        control2: CGPoint(x: cx - baseX - 16 * u, y: baseY + 130 * u)
    )
    // Front of the base bulges toward the viewer.
    wall.addCurve(
        to: CGPoint(x: cx + baseX, y: baseY),
        control1: CGPoint(x: cx - baseX, y: baseY - baseYr * 1.34),
        control2: CGPoint(x: cx + baseX, y: baseY - baseYr * 1.34)
    )
    wall.addCurve(
        to: CGPoint(x: cx + rimX, y: rimY),
        control1: CGPoint(x: cx + baseX + 16 * u, y: baseY + 130 * u),
        control2: CGPoint(x: cx + rimX - 14 * u, y: rimY - 160 * u)
    )
    // Front of the lip, also bulging toward the viewer.
    wall.addCurve(
        to: CGPoint(x: cx - rimX, y: rimY),
        control1: CGPoint(x: cx + rimX, y: rimY - rimYr * 1.34),
        control2: CGPoint(x: cx - rimX, y: rimY - rimYr * 1.34)
    )
    wall.closeSubpath()
    ctx.addPath(wall)
    ctx.fillPath()

    // The tea, inset inside the lip.
    let inset = 36 * u
    let tea = CGPath(
        ellipseIn: CGRect(
            x: cx - rimX + inset, y: rimY - rimYr + inset * 0.42,
            width: (rimX - inset) * 2, height: (rimYr - inset * 0.42) * 2
        ),
        transform: nil
    )
    ctx.setFillColor(brew)
    ctx.addPath(tea)
    ctx.fillPath()

    // Lines of text floating on the surface, foreshortened by the ellipse.
    ctx.saveGState()
    ctx.addPath(tea)
    ctx.clip()
    let rows: [(CGFloat, CGFloat, CGFloat, CGFloat)] = [
        (-34, -92, 58, 20),
        (2, -132, 116, 22),
        (38, -70, 44, 20),
    ]
    for (dy, x0, x1, weight) in rows {
        bar(
            ctx,
            from: CGPoint(x: cx + x0 * u, y: rimY + dy * u),
            to: CGPoint(x: cx + x1 * u, y: rimY + dy * u),
            width: weight * u, color: ink
        )
    }
    ctx.restoreGState()

    // Voice rising out of the cup.
    let heights: [CGFloat] = [62, 118, 84]
    for (index, height) in heights.enumerated() {
        let x = cx + CGFloat(index - 1) * 88 * u
        bar(
            ctx,
            from: CGPoint(x: x, y: 744 * u),
            to: CGPoint(x: x, y: (744 + height) * u),
            width: 42 * u, color: brew
        )
    }
}

/// A tea leaf whose veins are lines of caption, and whose vein lengths rise and
/// fall like a level meter. One set of marks carrying both meanings beats
/// stacking a waveform on top of text; that always looks like two icons glued
/// together.
func drawLeafVoice(_ ctx: CGContext, u: CGFloat, leaf: CGColor, ink: CGColor, accent: CGColor) {
    let cx = 500 * u
    let cy = 516 * u
    let tilt: CGFloat = .pi / 4.4

    ctx.saveGState()
    ctx.translateBy(x: cx, y: cy)
    ctx.rotate(by: tilt)

    // Leaf: a pointed oval, drawn in local space so the veins line up with it.
    let half = 330 * u
    let belly = 176 * u
    let shape = CGMutablePath()
    shape.move(to: CGPoint(x: -half, y: 0))
    shape.addCurve(
        to: CGPoint(x: half, y: 0),
        control1: CGPoint(x: -half * 0.42, y: belly),
        control2: CGPoint(x: half * 0.46, y: belly)
    )
    shape.addCurve(
        to: CGPoint(x: -half, y: 0),
        control1: CGPoint(x: half * 0.46, y: -belly),
        control2: CGPoint(x: -half * 0.42, y: -belly)
    )
    shape.closeSubpath()
    ctx.setFillColor(leaf)
    ctx.addPath(shape)
    ctx.fillPath()

    // Stem
    ctx.setStrokeColor(leaf)
    ctx.setLineCap(.round)
    ctx.setLineWidth(40 * u)
    ctx.move(to: CGPoint(x: -half + 8 * u, y: 0))
    ctx.addLine(to: CGPoint(x: -half - 116 * u, y: 0))
    ctx.strokePath()

    ctx.saveGState()
    ctx.addPath(shape)
    ctx.clip()

    // A leaf's veins already carry meaning; overwriting them with a second one
    // just reads as a fish bone. So the blade stays plain and takes a seal
    // instead — the way a tea wrapper is stamped.
    ctx.rotate(by: -tilt)
    if let glyph = wenPath(capHeight: 232 * u, centre: .zero) {
        ctx.setFillColor(ink)
        ctx.addPath(glyph)
        ctx.fillPath()
    }
    ctx.rotate(by: tilt)

    // Veins: lengths follow a level-meter envelope, so the same marks read as
    // caption lines and as sound.
    _ = accent
    ctx.restoreGState()
    ctx.restoreGState()

    // Voice leaving the leaf tip.
    let heights: [CGFloat] = [50, 96, 66]
    for (index, height) in heights.enumerated() {
        let x = 636 * u + CGFloat(index) * 74 * u
        bar(
            ctx,
            from: CGPoint(x: x, y: 760 * u),
            to: CGPoint(x: x, y: (760 + height) * u),
            width: 34 * u, color: accent
        )
    }
}

func drawOrnament(
    _ ctx: CGContext, variant: Variant, u: CGFloat,
    stroke: CGColor, glyph: CGColor, micFill: CGColor, micStroke: CGColor, lineWidth: CGFloat
) {
    switch variant {
    case .waves:
        drawWaves(ctx, u: u, stroke: stroke, lineWidth: lineWidth)
    case .mic:
        drawMic(ctx, u: u, stroke: micStroke, fill: micFill, lineWidth: lineWidth)
    case .caption:
        drawCaption(ctx, u: u, stroke: stroke, lineWidth: lineWidth)
    case .wenSteam:
        drawWen(ctx, u: u, centre: CGPoint(x: 500 * u, y: 690 * u), capHeight: 280 * u, color: glyph)
    case .wenInCup:
        drawWen(ctx, u: u, centre: CGPoint(x: 500 * u, y: 372 * u), capHeight: 190 * u, color: glyph)
    case .wenCup, .wenSolid, .cupTilt, .leafVoice:
        break  // these draw the whole mark themselves
    case .wenRim:
        // 文 straddles the rim: its 乂 sits in the tea, its 亠 rises as steam.
        // The cup line and the character share one stroke, so neither reads as
        // decoration on top of the other.
        drawWen(ctx, u: u, centre: CGPoint(x: 500 * u, y: 486 * u), capHeight: 330 * u, color: glyph)
    case .wenWaves:
        // Two arcs keep the "sound" reading; the glyph stops it being Wi-Fi.
        ctx.setStrokeColor(stroke)
        ctx.setLineCap(.round)
        for (index, radius) in [250, 324].enumerated() {
            let path = CGMutablePath()
            path.addArc(
                center: CGPoint(x: 500 * u, y: 500 * u), radius: CGFloat(radius) * u,
                startAngle: .pi * 0.2, endAngle: .pi * 0.8, clockwise: false
            )
            ctx.setLineWidth(lineWidth * (1.0 - CGFloat(index) * 0.15))
            ctx.addPath(path)
            ctx.strokePath()
        }
        drawWen(ctx, u: u, centre: CGPoint(x: 500 * u, y: 618 * u), capHeight: 162 * u, color: glyph)
    }
}

func renderAppIcon(_ variant: Variant, size: Int) -> CGImage {
    let ctx = context(size: size)
    let s = CGFloat(size)
    let u = s / 1000
    roundedBackground(ctx, size: s)
    ctx.translateBy(x: 0, y: -s * 0.02)
    let line = 46 * u
    if variant == .wenCup {
        drawWenCup(ctx, u: u, stroke: Palette.cream, accent: Palette.brew, lineWidth: line)
        return ctx.makeImage()!
    }
    if variant == .wenSolid {
        drawSolidWenCup(ctx, u: u, size: s, accent: Palette.brew)
        return ctx.makeImage()!
    }
    if variant == .cupTilt {
        drawTiltedCup(ctx, u: u, cream: Palette.cream, brew: Palette.brew, ink: Palette.teaDark)
        return ctx.makeImage()!
    }
    if variant == .leafVoice {
        drawLeafVoice(ctx, u: u, leaf: Palette.cream, ink: Palette.teaDark, accent: Palette.brew)
        return ctx.makeImage()!
    }
    drawCup(ctx, u: u, stroke: Palette.cream, fill: nil, lineWidth: line)
    drawOrnament(ctx, variant: variant, u: u, stroke: Palette.brew, glyph: Palette.brew,
                 micFill: Palette.brew, micStroke: Palette.cream, lineWidth: line)
    return ctx.makeImage()!
}

/// Menu bar icons are template images: black plus alpha, tinted by the system.
func renderTemplate(_ variant: Variant, size: Int) -> CGImage {
    let ctx = context(size: size)
    let s = CGFloat(size)
    let u = s / 1000
    let black = CGColor(red: 0, green: 0, blue: 0, alpha: 1)
    // Scale the drawing up: no background plate, so it can fill the frame.
    ctx.translateBy(x: s * 0.5, y: s * 0.46)
    ctx.scaleBy(x: 1.22, y: 1.22)
    ctx.translateBy(x: -s * 0.5, y: -s * 0.5)
    let line = 62 * u
    if variant == .wenCup || variant == .wenSolid {
        drawWenCup(ctx, u: u, stroke: black, accent: black, lineWidth: line)
        return ctx.makeImage()!
    }
    if variant == .cupTilt {
        drawTiltedCup(ctx, u: u, cream: black, brew: black, ink: CGColor(gray: 1, alpha: 1))
        return ctx.makeImage()!
    }
    if variant == .leafVoice {
        drawLeafVoice(ctx, u: u, leaf: black, ink: CGColor(gray: 1, alpha: 1), accent: black)
        return ctx.makeImage()!
    }
    drawCup(ctx, u: u, stroke: black, fill: nil, lineWidth: line)
    drawOrnament(ctx, variant: variant, u: u, stroke: black, glyph: black,
                 micFill: black, micStroke: black, lineWidth: line)
    return ctx.makeImage()!
}

func write(_ image: CGImage, to url: URL) {
    let rep = NSBitmapImageRep(cgImage: image)
    guard let data = rep.representation(using: .png, properties: [:]) else { return }
    try? data.write(to: url)
}

let output = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ".")
try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

for variant in Variant.allCases {
    write(renderAppIcon(variant, size: 1024), to: output.appendingPathComponent("app-\(variant.rawValue)-1024.png"))
    write(renderTemplate(variant, size: 512), to: output.appendingPathComponent("menu-\(variant.rawValue)-512.png"))
    for scale in [18, 36] {
        write(renderTemplate(variant, size: scale), to: output.appendingPathComponent("menu-\(variant.rawValue)-\(scale).png"))
    }
}
print("wrote icons to \(output.path)")
