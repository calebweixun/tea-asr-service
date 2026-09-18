import AppKit

let arguments = CommandLine.arguments
if let index = arguments.firstIndex(of: "--selftest"), index + 1 < arguments.count {
    var override: String?
    if let urlIndex = arguments.firstIndex(of: "--url"), urlIndex + 1 < arguments.count {
        override = arguments[urlIndex + 1]
    }
    SelfTest.run(
        path: arguments[index + 1],
        realtime: !arguments.contains("--no-realtime"),
        forcePreview: arguments.contains("--preview"),
        urlOverride: override
    )
}

let application = NSApplication.shared
let controller = AppController()
application.delegate = controller
application.run()
