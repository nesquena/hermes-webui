import Cocoa
import Darwin
import Foundation

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate

// Safe signal handling via GCD (signal handlers must be async-signal-safe)
let sigTermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigTermSource.setEventHandler {
    (NSApp.delegate as? AppDelegate)?.tunnelManager?.stop()
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { NSApp.terminate(nil) }
}
sigTermSource.resume()
signal(SIGTERM, SIG_IGN)

let sigIntSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigIntSource.setEventHandler {
    (NSApp.delegate as? AppDelegate)?.tunnelManager?.stop()
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { NSApp.terminate(nil) }
}
sigIntSource.resume()
signal(SIGINT, SIG_IGN)

app.run()
