import AppKit
import CoreGraphics
import Foundation

// Takes a generated square PNG and turns it into macOS icon assets.
//
// Generated images arrive as a rounded icon painted on an opaque square: the
// corners are white, not transparent. Pasting that into an .icns gives a white
// box behind every rounded corner. This crops to the real artwork, masks it to
// the Apple squircle so the corners are actually transparent, pads it to the
// proportions macOS expects, and writes every size plus an .icns.
//
//   swift mask-icon.swift input.png output-directory [name]

struct Options {
    /// Apple draws app icons inset inside the canvas rather than bleeding to
    /// the edge; 1024pt art sits in roughly 824pt of shape.
    static let contentRatio: CGFloat = 0.8046875
    /// Superellipse exponent. A plain rounded rectangle reads subtly wrong next
    /// to real macOS icons; the continuous curve is what makes it sit right.
    static let squircleExponent: CGFloat = 5.0
    static let sizes = [16, 32, 64, 128, 256, 512, 1024]
}

func loadImage(_ path: String) -> CGImage? {
    guard
        let data = NSData(contentsOfFile: path),
        let source = CGImageSourceCreateWithData(data, nil)
    else { return nil }
    return CGImageSourceCreateImageAtIndex(source, 0, nil)
}

func makeContext(_ size: Int) -> CGContext {
    CGContext(
        data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    )!
}

/// The bounding box of everything that is not the surrounding flat background.
/// Sampling the corner tells us what that background is, so this works whether
/// the generator emitted white, transparent, or a tinted margin.
func artworkBounds(_ image: CGImage) -> CGRect {
    let width = image.width
    let height = image.height
    let ctx = makeContext(max(width, height))
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    guard let data = ctx.data else {
        return CGRect(x: 0, y: 0, width: width, height: height)
    }
    let rowBytes = ctx.bytesPerRow
    let pixels = data.bindMemory(to: UInt8.self, capacity: rowBytes * ctx.height)

    func sample(_ x: Int, _ y: Int) -> (r: Int, g: Int, b: Int, a: Int) {
        let offset = y * rowBytes + x * 4
        return (
            Int(pixels[offset]), Int(pixels[offset + 1]),
            Int(pixels[offset + 2]), Int(pixels[offset + 3])
        )
    }

    let background = sample(1, 1)
    func isBackground(_ p: (r: Int, g: Int, b: Int, a: Int)) -> Bool {
        if p.a < 12 { return true }
        let distance = abs(p.r - background.r) + abs(p.g - background.g) + abs(p.b - background.b)
        return background.a >= 12 && distance < 36
    }

    var minX = width, minY = height, maxX = -1, maxY = -1
    for y in 0..<height {
        for x in 0..<width where !isBackground(sample(x, y)) {
            if x < minX { minX = x }
            if x > maxX { maxX = x }
            if y < minY { minY = y }
            if y > maxY { maxY = y }
        }
    }
    guard maxX >= minX, maxY >= minY else {
        return CGRect(x: 0, y: 0, width: width, height: height)
    }
    // The context is y-up while the image is y-down; the crop is symmetric in
    // y only if we flip, so convert here rather than guessing later.
    let flippedMinY = height - 1 - maxY
    return CGRect(
        x: CGFloat(minX), y: CGFloat(flippedMinY),
        width: CGFloat(maxX - minX + 1), height: CGFloat(maxY - minY + 1)
    )
}

/// Apple's continuous corner, as a superellipse rather than a rounded rect.
func squircle(in rect: CGRect, exponent: CGFloat = Options.squircleExponent) -> CGPath {
    let path = CGMutablePath()
    let a = rect.width / 2
    let b = rect.height / 2
    let cx = rect.midX
    let cy = rect.midY
    let steps = 720
    for step in 0...steps {
        let theta = CGFloat(step) / CGFloat(steps) * 2 * .pi
        let cosT = cos(theta)
        let sinT = sin(theta)
        let x = cx + a * pow(abs(cosT), 2 / exponent) * (cosT < 0 ? -1 : 1)
        let y = cy + b * pow(abs(sinT), 2 / exponent) * (sinT < 0 ? -1 : 1)
        if step == 0 { path.move(to: CGPoint(x: x, y: y)) } else {
            path.addLine(to: CGPoint(x: x, y: y))
        }
    }
    path.closeSubpath()
    return path
}

func render(_ artwork: CGImage, size: Int) -> CGImage {
    let ctx = makeContext(size)
    ctx.interpolationQuality = .high
    let canvas = CGFloat(size)
    let content = (canvas * Options.contentRatio).rounded()
    let origin = ((canvas - content) / 2).rounded()
    let rect = CGRect(x: origin, y: origin, width: content, height: content)
    ctx.addPath(squircle(in: rect))
    ctx.clip()
    ctx.draw(artwork, in: rect)
    return ctx.makeImage()!
}

func write(_ image: CGImage, to url: URL) {
    guard let data = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])
    else { return }
    try? data.write(to: url)
}

let arguments = CommandLine.arguments
guard arguments.count >= 3, let source = loadImage(arguments[1]) else {
    FileHandle.standardError.write(Data("用法：swift mask-icon.swift <input.png> <outdir> [name]\n".utf8))
    exit(2)
}
let outputDirectory = URL(fileURLWithPath: arguments[2])
let name = arguments.count > 3 ? arguments[3] : "AppIcon"
try? FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)

let bounds = artworkBounds(source)
guard let cropped = source.cropping(to: bounds) else {
    FileHandle.standardError.write(Data("無法裁切\n".utf8))
    exit(1)
}
print("原圖 \(source.width)x\(source.height) → 裁出 \(Int(bounds.width))x\(Int(bounds.height))")

let iconset = outputDirectory.appendingPathComponent("\(name).iconset")
try? FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

for size in Options.sizes {
    let image = render(cropped, size: size)
    write(image, to: outputDirectory.appendingPathComponent("\(name)-\(size).png"))
    if size <= 512 {
        write(image, to: iconset.appendingPathComponent("icon_\(size)x\(size).png"))
    }
    if size >= 32 {
        write(image, to: iconset.appendingPathComponent("icon_\(size / 2)x\(size / 2)@2x.png"))
    }
}
print("iconset：\(iconset.path)")
