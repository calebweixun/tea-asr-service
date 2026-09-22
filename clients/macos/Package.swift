// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "TeaASRClient",
    platforms: [.macOS(.v13)],
    targets: [
        .target(
            name: "AVAudioEngineTapShim",
            path: "Sources/AVAudioEngineTapShim",
            publicHeadersPath: "include",
            linkerSettings: [
                .linkedFramework("AVFAudio")
            ]
        ),
        .executableTarget(
            name: "TeaASRClient",
            dependencies: ["AVAudioEngineTapShim"],
            path: "Sources/TeaASRClient"
        ),
        .testTarget(
            name: "TeaASRClientTests",
            dependencies: ["TeaASRClient", "AVAudioEngineTapShim"],
            path: "Tests/TeaASRClientTests"
        )
    ]
)
