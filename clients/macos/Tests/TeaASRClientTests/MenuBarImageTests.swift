import AppKit
import XCTest
@testable import TeaASRClient

@MainActor
final class MenuBarImageTests: XCTestCase {
    func testLoaderFindsAllScaleRepresentationsAndKeepsLogicalSize() throws {
        let bundleURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("TeaASRMenuBar-\(UUID().uuidString).bundle")
        try FileManager.default.createDirectory(at: bundleURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: bundleURL) }

        for scale in [1, 2, 3] {
            let pixels = 18 * scale
            let representation = try XCTUnwrap(
                NSBitmapImageRep(
                    bitmapDataPlanes: nil,
                    pixelsWide: pixels,
                    pixelsHigh: pixels,
                    bitsPerSample: 8,
                    samplesPerPixel: 4,
                    hasAlpha: true,
                    isPlanar: false,
                    colorSpaceName: .deviceRGB,
                    bitmapFormat: [],
                    bytesPerRow: 0,
                    bitsPerPixel: 0
                )
            )
            representation.size = NSSize(width: 18, height: 18)
            let data = try XCTUnwrap(
                representation.representation(using: .png, properties: [:])
            )
            let suffix = scale == 1 ? "" : "@\(scale)x"
            try data.write(to: bundleURL.appendingPathComponent("MenuBar-idle\(suffix).png"))
        }

        let bundle = try XCTUnwrap(Bundle(url: bundleURL))
        let image = try XCTUnwrap(MenuBarImageLoader.image(state: "idle", bundle: bundle))

        XCTAssertEqual(image.size, NSSize(width: 18, height: 18))
        XCTAssertTrue(image.isTemplate)
        XCTAssertEqual(
            Set(image.representations.map { "\($0.pixelsWide)x\($0.pixelsHigh)" }),
            Set(["18x18", "36x36", "54x54"])
        )
    }

    func testStatusItemSizingUsesFixedNonZeroLengthForImageOnlyItems() {
        XCTAssertEqual(
            MenuBarStatusItemSizing.length,
            MenuBarImageLoader.logicalSize.width
        )
        XCTAssertGreaterThan(MenuBarStatusItemSizing.length, 0)
    }
}
