import AppKit

let arguments = CommandLine.arguments
if let index = arguments.firstIndex(of: "--selftest"), index + 1 < arguments.count {
    SelfTest.run(
        path: arguments[index + 1],
        realtime: !arguments.contains("--no-realtime"),
        forcePreview: arguments.contains("--preview")
    )
}

let application = NSApplication.shared
let controller = AppController()
application.delegate = controller
application.run()
