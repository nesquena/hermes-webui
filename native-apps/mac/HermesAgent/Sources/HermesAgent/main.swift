import Cocoa
import Foundation

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate

signal(SIGTERM) { _ in
    (NSApp.delegate as? AppDelegate)?.tunnelManager?.stop()
    Thread.sleep(forTimeInterval: 1.5)
    exit(0)
}
signal(SIGINT) { _ in
    (NSApp.delegate as? AppDelegate)?.tunnelManager?.stop()
    Thread.sleep(forTimeInterval: 1.5)
    exit(0)
}

app.run()
