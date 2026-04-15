import Cocoa

class PreferencesWindowController: NSWindowController {

    var onSave: (() -> Void)?

    private var usernameField: NSTextField!
    private var hostField: NSTextField!
    private var localPortField: NSTextField!
    private var remotePortField: NSTextField!
    private var targetURLField: NSTextField!

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 380),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Preferences"
        window.center()
        super.init(window: window)
        buildUI()
    }

    required init?(coder: NSCoder) { fatalError() }

    private func buildUI() {
        let content = window!.contentView!
        var y: CGFloat = 320

        func sectionHeader(_ text: String) {
            let label = NSTextField(labelWithString: text)
            label.font = NSFont.systemFont(ofSize: 10, weight: .semibold)
            label.textColor = .secondaryLabelColor
            label.frame = NSRect(x: 24, y: y, width: 420, height: 16)
            content.addSubview(label)
            y -= 28
        }

        func row(_ labelText: String, placeholder: String, defaultsKey: String, width: CGFloat = 260) -> NSTextField {
            let label = NSTextField(labelWithString: labelText)
            label.font = NSFont.systemFont(ofSize: 13)
            label.frame = NSRect(x: 24, y: y, width: 130, height: 22)
            label.alignment = .right
            content.addSubview(label)

            let field = NSTextField()
            field.placeholderString = placeholder
            field.stringValue = UserDefaults.standard.string(forKey: defaultsKey) ?? ""
            field.font = NSFont.systemFont(ofSize: 13)
            field.frame = NSRect(x: 164, y: y, width: width, height: 22)
            field.bezelStyle = .roundedBezel
            content.addSubview(field)

            y -= 36
            return field
        }

        func divider() {
            let line = NSBox()
            line.boxType = .separator
            line.frame = NSRect(x: 24, y: y + 10, width: 432, height: 1)
            content.addSubview(line)
            y -= 20
        }

        // SSH Connection
        sectionHeader("SSH CONNECTION")
        usernameField  = row("Username",    placeholder: "root",             defaultsKey: "sshUser")
        hostField      = row("Host",        placeholder: "your-server.com",  defaultsKey: "sshHost")

        divider()

        // Port forwarding
        sectionHeader("PORT FORWARDING")
        localPortField  = row("Local port",  placeholder: "8080", defaultsKey: "localPort",  width: 80)
        remotePortField = row("Remote port", placeholder: "8080", defaultsKey: "remotePort", width: 80)

        divider()

        // App
        sectionHeader("APP")
        targetURLField = row("Target URL", placeholder: "http://localhost:8080", defaultsKey: "targetURL")

        // Buttons
        let cancelBtn = NSButton(title: "Cancel", target: self, action: #selector(cancel))
        cancelBtn.bezelStyle = .rounded
        cancelBtn.frame = NSRect(x: 264, y: 16, width: 90, height: 32)
        content.addSubview(cancelBtn)

        let saveBtn = NSButton(title: "Save & Reconnect", target: self, action: #selector(save))
        saveBtn.bezelStyle = .rounded
        saveBtn.keyEquivalent = "\r"
        saveBtn.frame = NSRect(x: 362, y: 16, width: 100, height: 32)
        content.addSubview(saveBtn)
    }

    @objc func save() {
        // Validate fields aren't empty
        guard !usernameField.stringValue.isEmpty,
              !hostField.stringValue.isEmpty,
              !localPortField.stringValue.isEmpty,
              !remotePortField.stringValue.isEmpty,
              !targetURLField.stringValue.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "Missing fields"
            alert.informativeText = "Please fill in all fields before saving."
            alert.runModal()
            return
        }

        guard let localPort = Int(localPortField.stringValue), (1...65535).contains(localPort) else {
            showValidationError("Local port must be a number between 1 and 65535.")
            return
        }

        guard let remotePort = Int(remotePortField.stringValue), (1...65535).contains(remotePort) else {
            showValidationError("Remote port must be a number between 1 and 65535.")
            return
        }

        guard let targetURL = URL(string: targetURLField.stringValue),
              let scheme = targetURL.scheme?.lowercased(),
              ["http", "https"].contains(scheme) else {
            showValidationError("Target URL must be a valid http:// or https:// URL.")
            return
        }

        let defaults = UserDefaults.standard
        defaults.set(usernameField.stringValue,  forKey: "sshUser")
        defaults.set(hostField.stringValue,      forKey: "sshHost")
        defaults.set(String(localPort),          forKey: "localPort")
        defaults.set(String(remotePort),         forKey: "remotePort")
        defaults.set(targetURL.absoluteString,   forKey: "targetURL")

        close()
        onSave?()
    }

    private func showValidationError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Invalid value"
        alert.informativeText = message
        alert.runModal()
    }

    @objc func cancel() {
        close()
    }
}
