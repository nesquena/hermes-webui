// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "HermesAgent",
    platforms: [.macOS(.v12)],
    targets: [
        .executableTarget(
            name: "HermesAgent",
            path: "Sources/HermesAgent"
        )
    ]
)
