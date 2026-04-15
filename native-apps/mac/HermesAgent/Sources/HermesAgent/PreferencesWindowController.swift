import Cocoa

class PreferencesWindowController: NSWindowController {

    var onSave: (() -> Void)?

    private var connectionModeSegment: NSSegmentedControl!
    private var sshViews: [NSView] = []
    private var usernameField: NSTextField!
    private var hostField: NSTextField!
    private var localPortField: NSTextField!
    private var remotePortField: NSTextField!
    private var targetURLField: NSTextField!

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 480),
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
        var y: CGFloat = 420

        func sectionHeader(_ text: String) -> NSTextField {
            let label = NSTextField(labelWithString: text)
            label.font = NSFont.systemFont(ofSize: 10, weight: .semibold)
            label.textColor = .secondaryLabelColor
            label.frame = NSRect(x: 24, y: y, width: 420, height: 16)
            content.addSubview(label)
            y -= 28
            return label
        }

        func row(
            _ labelText: String, placeholder: String, defaultsKey: String, width: CGFloat = 260,
            isSSH: Bool = false
        ) -> NSTextField {
            let label = NSTextField(labelWithString: labelText)
            label.font = NSFont.systemFont(ofSize: 13)
            label.frame = NSRect(x: 24, y: y, width: 130, height: 22)
            label.alignment = .right
            content.addSubview(label)
            if isSSH { sshViews.append(label) }

            let field = NSTextField()
            field.placeholderString = placeholder
            field.stringValue = UserDefaults.standard.string(forKey: defaultsKey) ?? ""
            field.font = NSFont.systemFont(ofSize: 13)
            field.frame = NSRect(x: 164, y: y, width: width, height: 22)
            field.bezelStyle = .roundedBezel
            content.addSubview(field)
            if isSSH { sshViews.append(field) }

            y -= 36
            return field
        }

        func divider() -> NSBox {
            let line = NSBox()
            line.boxType = .separator
            line.frame = NSRect(x: 24, y: y + 10, width: 432, height: 1)
            content.addSubview(line)
            y -= 20
            return line
        }

        // Connection Mode
        _ = sectionHeader("CONNECTION MODE")
        let modeLabel = NSTextField(labelWithString: "Mode")
        modeLabel.font = NSFont.systemFont(ofSize: 13)
        modeLabel.frame = NSRect(x: 24, y: y, width: 130, height: 22)
        modeLabel.alignment = .right
        content.addSubview(modeLabel)

        connectionModeSegment = NSSegmentedControl(
            labels: ["Direct (Local)", "SSH Tunnel"], trackingMode: .selectOne, target: self,
            action: #selector(modeChanged))
        connectionModeSegment.frame = NSRect(x: 164, y: y - 2, width: 260, height: 22)
        let mode = UserDefaults.standard.string(forKey: "connectionMode") ?? "direct"
        connectionModeSegment.selectedSegment = mode == "ssh" ? 1 : 0
        content.addSubview(connectionModeSegment)
        y -= 36

        let divider1 = divider()
        sshViews.append(divider1)

        // SSH Connection section (shown only in SSH mode)
        let sshHeader = sectionHeader("SSH CONNECTION")
        sshViews.append(sshHeader)
        sshHeader.isHidden = mode == "direct"

        usernameField = row("Username", placeholder: "hermes", defaultsKey: "sshUser", isSSH: true)
        usernameField.isHidden = mode == "direct"
        hostField = row("Host", placeholder: "your-server.com", defaultsKey: "sshHost", isSSH: true)
        hostField.isHidden = mode == "direct"

        let divider2 = divider()
        sshViews.append(divider2)

        // Port forwarding section
        let portHeader = sectionHeader("PORT FORWARDING")
        sshViews.append(portHeader)
        portHeader.isHidden = mode == "direct"

        localPortField = row(
            "Local port", placeholder: "8787", defaultsKey: "localPort", width: 80, isSSH: true)
        localPortField.isHidden = mode == "direct"
        remotePortField = row(
            "Remote port", placeholder: "8787", defaultsKey: "remotePort", width: 80, isSSH: true)
        remotePortField.isHidden = mode == "direct"

        _ = divider()

        // App section
        _ = sectionHeader("APP")
        targetURLField = row(
            "Target URL", placeholder: "http://localhost:8787", defaultsKey: "targetURL")

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
        let connectionMode = connectionModeSegment.selectedSegment == 0 ? "direct" : "ssh"

        guard !targetURLField.stringValue.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "Missing fields"
            alert.informativeText = "Please fill in the Target URL."
            alert.runModal()
            return
        }

        guard let targetURL = URL(string: targetURLField.stringValue),
            let scheme = targetURL.scheme?.lowercased(),
            ["http", "https"].contains(scheme)
        else {
            showValidationError("Target URL must be a valid http:// or https:// URL.")
            return
        }

        if connectionMode == "ssh" {
            guard !usernameField.stringValue.isEmpty,
                !hostField.stringValue.isEmpty,
                !localPortField.stringValue.isEmpty,
                !remotePortField.stringValue.isEmpty
            else {
                let alert = NSAlert()
                alert.messageText = "Missing SSH fields"
                alert.informativeText = "Please fill in all SSH settings."
                alert.runModal()
                return
            }

            guard let localPort = Int(localPortField.stringValue), (1...65535).contains(localPort)
            else {
                showValidationError("Local port must be a number between 1 and 65535.")
                return
            }

            guard let remotePort = Int(remotePortField.stringValue),
                (1...65535).contains(remotePort)
            else {
                showValidationError("Remote port must be a number between 1 and 65535.")
                return
            }

            let defaults = UserDefaults.standard
            defaults.set(connectionMode, forKey: "connectionMode")
            defaults.set(usernameField.stringValue, forKey: "sshUser")
            defaults.set(hostField.stringValue, forKey: "sshHost")
            defaults.set(String(localPort), forKey: "localPort")
            defaults.set(String(remotePort), forKey: "remotePort")
            defaults.set(targetURL.absoluteString, forKey: "targetURL")
        } else {
            let defaults = UserDefaults.standard
            defaults.set(connectionMode, forKey: "connectionMode")
            defaults.set(targetURL.absoluteString, forKey: "targetURL")
        }

        close()
        onSave?()
    }

    @objc func modeChanged() {
        let isSSHMode = connectionModeSegment.selectedSegment == 1
        sshViews.forEach { $0.isHidden = !isSSHMode }
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
