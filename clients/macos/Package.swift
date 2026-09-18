// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "TeaASRClient",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "TeaASRClient",
            path: "Sources/TeaASRClient"
        )
    ]
)
