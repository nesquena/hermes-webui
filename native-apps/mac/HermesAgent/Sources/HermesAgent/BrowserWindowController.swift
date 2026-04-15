import Cocoa
import WebKit

class BrowserWindow: NSWindow {
    var onPaste: (() -> Void)?

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.contains(.command) &&
           event.charactersIgnoringModifiers == "v" {
            onPaste?()
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

class BrowserWindowController: NSWindowController, WKUIDelegate, WKNavigationDelegate {

    private var webView: WKWebView!
    private var statusDot: NSView!
    private var statusLabel: NSTextField!
    private var reconnectButton: NSButton!
    private let urlString: String
    private let appTitle: String
    var onReconnect: (() -> Void)?

    init(urlString: String, title: String) {
        self.urlString = urlString
        self.appTitle = title

        let window = BrowserWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 830),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = title
        window.center()
        super.init(window: window)

        window.onPaste = { [weak self] in
            self?.handlePaste()
        }

        buildUI()
    }

    required init?(coder: NSCoder) { fatalError() }

    private func buildUI() {
        guard let contentView = window?.contentView else { return }
        let bounds = contentView.bounds
        let statusBarHeight: CGFloat = 28

        let config = WKWebViewConfiguration()
        let prefs = WKPreferences()
        prefs.setValue(true, forKey: "javaScriptCanAccessClipboard")
        prefs.setValue(true, forKey: "DOMPasteAllowed")
        config.preferences = prefs
        let script = WKUserScript(
            source: "document.addEventListener('paste', function(e) { e.stopImmediatePropagation(); }, true);",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false
        )
        config.userContentController.addUserScript(script)

        let webFrame = NSRect(x: 0, y: statusBarHeight, width: bounds.width, height: bounds.height - statusBarHeight)
        webView = WKWebView(frame: webFrame, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.allowsMagnification = true
        contentView.addSubview(webView)

        let statusBar = NSView(frame: NSRect(x: 0, y: 0, width: bounds.width, height: statusBarHeight))
        statusBar.autoresizingMask = [.width]
        statusBar.wantsLayer = true
        statusBar.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        contentView.addSubview(statusBar)

        let separator = NSView(frame: NSRect(x: 0, y: statusBarHeight - 1, width: bounds.width, height: 1))
        separator.autoresizingMask = [.width]
        separator.wantsLayer = true
        separator.layer?.backgroundColor = NSColor.separatorColor.cgColor
        contentView.addSubview(separator)

        statusDot = NSView(frame: NSRect(x: 12, y: 9, width: 10, height: 10))
        statusDot.wantsLayer = true
        statusDot.layer?.cornerRadius = 5
        statusDot.layer?.backgroundColor = NSColor.systemGray.cgColor
        statusBar.addSubview(statusDot)

        statusLabel = NSTextField(labelWithString: "Connecting…")
        statusLabel.font = NSFont.systemFont(ofSize: 11)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.frame = NSRect(x: 30, y: 6, width: 500, height: 16)
        statusBar.addSubview(statusLabel)

        reconnectButton = NSButton(title: "Reconnect", target: self, action: #selector(reconnectTapped))
        reconnectButton.bezelStyle = .rounded
        reconnectButton.font = NSFont.systemFont(ofSize: 11)
        reconnectButton.frame = NSRect(x: bounds.width - 110, y: 2, width: 100, height: 24)
        reconnectButton.autoresizingMask = [.minXMargin]
        reconnectButton.isHidden = true
        statusBar.addSubview(reconnectButton)

        if let url = URL(string: urlString) {
            webView.load(URLRequest(url: url))
        }
    }

    // MARK: - Paste

    func handlePaste() {
        let pb = NSPasteboard.general

        // Image paste — write to temp file and inject via fetch
        if let image = NSImage(pasteboard: pb),
           let tiff = image.tiffRepresentation,
           let bitmap = NSBitmapImageRep(data: tiff),
           let png = bitmap.representation(using: .png, properties: [:]) {

            // Write to temp file that the webview can fetch
            let tmpURL = URL(fileURLWithPath: NSTemporaryDirectory())
                .appendingPathComponent("clipboard_\(Int(Date().timeIntervalSince1970)).png")
            try? png.write(to: tmpURL)

            let base64 = png.base64EncodedString()

            // Try multiple strategies to get the image into the web app
            let js = """
            (function() {
                const base64 = '\(base64)';
                const binary = atob(base64);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
                const blob = new Blob([bytes], { type: 'image/png' });
                const file = new File([blob], 'screenshot.png', { type: 'image/png', lastModified: Date.now() });

                // Strategy 1: fire paste event on active element with clipboardData
                const active = document.activeElement || document.body;
                const dt = new DataTransfer();
                dt.items.add(file);

                // Override clipboardData getter so web app can read items
                const pasteEvent = new Event('paste', { bubbles: true, cancelable: true });
                Object.defineProperty(pasteEvent, 'clipboardData', {
                    value: dt,
                    writable: false
                });
                active.dispatchEvent(pasteEvent);

                // Strategy 2: also try on document and body
                document.dispatchEvent(new Event('paste', { bubbles: true }));

                // Strategy 3: simulate drop on active element
                const dropDt = new DataTransfer();
                dropDt.items.add(file);
                const rect = active.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                ['dragenter','dragover','drop'].forEach(type => {
                    const ev = new DragEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        clientX: cx,
                        clientY: cy,
                        dataTransfer: dropDt
                    });
                    active.dispatchEvent(ev);
                });

                return 'ok';
            })();
            """
            webView.evaluateJavaScript(js) { result, error in
                if let error = error {
                    print("Paste JS error: \(error)")
                } else {
                    print("Paste JS result: \(result ?? "nil")")
                }
            }

        } else if let text = pb.string(forType: .string) {
            let escaped = text
                .replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "`", with: "\\`")
                .replacingOccurrences(of: "\r\n", with: "\\n")
                .replacingOccurrences(of: "\n", with: "\\n")
            webView.evaluateJavaScript(
                "document.execCommand('insertText', false, `\(escaped)`);",
                completionHandler: nil
            )
        } else {
            webView.evaluateJavaScript("document.execCommand('paste')", completionHandler: nil)
        }
    }

    // MARK: - Status

    func updateStatus(_ status: TunnelStatus, host: String, port: Int) {
        DispatchQueue.main.async {
            switch status {
            case .connecting:
                self.statusDot.layer?.backgroundColor = NSColor.systemGray.cgColor
                self.statusLabel.stringValue = "Connecting…"
                self.reconnectButton.isHidden = true
            case .connected:
                self.statusDot.layer?.backgroundColor = NSColor.systemGreen.cgColor
                self.statusLabel.stringValue = "Tunnel connected · \(host) · port \(port)"
                self.reconnectButton.isHidden = true
            case .disconnected:
                self.statusDot.layer?.backgroundColor = NSColor.systemRed.cgColor
                self.statusLabel.stringValue = "Tunnel disconnected · click Reconnect to retry"
                self.reconnectButton.isHidden = false
            }
        }
    }

    @objc func reconnectTapped() {
        onReconnect?()
    }

    // MARK: - File upload

    func webView(_ webView: WKWebView,
                 runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: self.window!) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }
}
