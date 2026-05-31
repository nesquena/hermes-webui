// Hermes — native macOS shell.
//
// A thin WKWebView window that owns the whole Hermes lifecycle:
//   • on launch it starts the Python `supervisor` (which installs the agent on
//     first run, then starts the WebUI backend),
//   • it shows a loading screen until the supervisor prints `HERMES-READY`,
//   • it loads the local WebUI URL,
//   • on quit (⌘Q, window close, or app termination) it SIGTERMs the supervisor
//     and SIGKILLs it as a backstop, so the frontend, backend, and Hermes agent
//     all shut down with the window.
//
// Built as a single binary with `swiftc` — see build.sh.

import AppKit
import WebKit

// MARK: - Bundle layout helpers

enum BundlePaths {
    /// .../Hermes.app/Contents/Resources
    static var resources: URL {
        Bundle.main.resourceURL ?? URL(fileURLWithPath: ".")
    }
    /// Bundled relocatable Python interpreter. A universal build ships one Python
    /// per arch (python-arm64 / python-x86_64); each native slice of this binary
    /// picks its matching one. A single-arch build ships just `python`.
    static var python: URL {
        #if arch(arm64)
        let archDir = "python-arm64"
        #else
        let archDir = "python-x86_64"
        #endif
        let universal = resources.appendingPathComponent("\(archDir)/bin/python3")
        if FileManager.default.isExecutableFile(atPath: universal.path) { return universal }
        return resources.appendingPathComponent("python/bin/python3")
    }
    /// The WebUI checkout shipped inside the app.
    static var supervisor: URL {
        resources.appendingPathComponent("webui/packaging/macos/supervisor.py")
    }
    static var webuiDir: URL {
        resources.appendingPathComponent("webui")
    }
}

// MARK: - Supervisor process manager

final class Supervisor {
    private var process: Process?
    private(set) var url: URL?
    private var onReady: ((URL) -> Void)?
    private var onFailure: ((String) -> Void)?
    private var onProgress: ((String) -> Void)?

    func start(onReady: @escaping (URL) -> Void,
               onProgress: @escaping (String) -> Void,
               onFailure: @escaping (String) -> Void) {
        self.onReady = onReady
        self.onProgress = onProgress
        self.onFailure = onFailure

        let proc = Process()
        // Prefer the bundled Python; fall back to a discoverable python3 in dev.
        let py = FileManager.default.isExecutableFile(atPath: BundlePaths.python.path)
            ? BundlePaths.python
            : URL(fileURLWithPath: "/usr/bin/env")
        if py.lastPathComponent == "env" {
            proc.executableURL = py
            proc.arguments = ["python3", BundlePaths.supervisor.path]
        } else {
            proc.executableURL = py
            proc.arguments = [BundlePaths.supervisor.path]
        }

        var env = ProcessInfo.processInfo.environment
        env["HERMES_WEBUI_DIR"] = BundlePaths.webuiDir.path
        // Make the bundled python's bin dir discoverable to children.
        let pyBin = BundlePaths.python.deletingLastPathComponent().path
        env["PATH"] = pyBin + ":" + (env["PATH"] ?? "/usr/bin:/bin:/usr/local/bin") + ":" + NSHomeDirectory() + "/.local/bin"
        // NB: deliberately do NOT set HERMES_WEBUI_PYTHON. The bundled Python only
        // bootstraps; bootstrap.py auto-discovers the agent's venv interpreter
        // (which carries the heavy ML deps) to run the actual server. Forcing the
        // bundled Python here would break `from run_agent import AIAgent`.
        proc.environment = env

        let outPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = outPipe
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            FileHandle.standardError.write(data)  // forward logs to Console
            for line in text.split(separator: "\n") {
                if line.contains("HERMES-READY"),
                   let range = line.range(of: "url="),
                   let u = URL(string: String(line[range.upperBound...]).trimmingCharacters(in: .whitespaces)) {
                    self?.url = u
                    DispatchQueue.main.async { self?.onReady?(u) }
                } else if let range = line.range(of: "HERMES-PROGRESS ") {
                    let msg = String(line[range.upperBound...]).trimmingCharacters(in: .whitespaces)
                    if !msg.isEmpty {
                        DispatchQueue.main.async { self?.onProgress?(msg) }
                    }
                }
            }
        }

        proc.terminationHandler = { [weak self] p in
            if self?.url == nil {
                DispatchQueue.main.async {
                    self?.onFailure?("Hermes backend exited before it was ready (code \(p.terminationStatus)).")
                }
            }
        }

        do {
            try proc.run()
            process = proc
        } catch {
            onFailure("Failed to start Hermes backend: \(error.localizedDescription)")
        }
    }

    /// Stop the supervisor (and therefore the whole Hermes process tree).
    func stop() {
        guard let proc = process, proc.isRunning else { return }
        let pid = proc.processIdentifier
        kill(pid, SIGTERM)  // ask the supervisor to shut everything down gracefully
        // Give it up to 10s, then force-kill.
        let deadline = Date().addingTimeInterval(10)
        while proc.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.1)
        }
        if proc.isRunning {
            kill(pid, SIGKILL)
            proc.waitUntilExit()
        }
        process = nil
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var loadingTitle: NSTextField!
    private var loadingLabel: NSTextField!
    private var spinner: NSProgressIndicator!
    private var loadingStack: NSStackView!
    private let supervisor = Supervisor()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let frame = NSRect(x: 0, y: 0, width: 1180, height: 820)
        // Standard titlebar (no .fullSizeContentView): the web content sits
        // BELOW the titlebar so it never overlaps the traffic-light buttons, and
        // the titlebar gives a draggable region to move the window. (The embedded
        // WebUI page has no -webkit-app-region:drag strip of its own, so a
        // full-size-content transparent titlebar would leave the window unmovable.)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Hermes"
        window.isMovableByWindowBackground = true
        window.center()
        window.setFrameAutosaveName("HermesMainWindow")

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: frame, configuration: config)
        webView.navigationDelegate = self
        webView.autoresizingMask = [.width, .height]
        webView.isHidden = true

        loadingTitle = NSTextField(labelWithString: "Setting up Hermes")
        loadingTitle.alignment = .center
        loadingTitle.font = .systemFont(ofSize: 19, weight: .semibold)
        loadingTitle.textColor = .labelColor
        loadingTitle.translatesAutoresizingMaskIntoConstraints = false

        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        spinner.translatesAutoresizingMaskIntoConstraints = false

        loadingLabel = NSTextField(labelWithString: "Starting…")
        loadingLabel.alignment = .center
        loadingLabel.maximumNumberOfLines = 3
        loadingLabel.lineBreakMode = .byTruncatingTail
        loadingLabel.textColor = .secondaryLabelColor
        loadingLabel.font = .systemFont(ofSize: 13)
        loadingLabel.translatesAutoresizingMaskIntoConstraints = false

        loadingStack = NSStackView(views: [loadingTitle, spinner, loadingLabel])
        loadingStack.orientation = .vertical
        loadingStack.alignment = .centerX
        loadingStack.spacing = 16
        loadingStack.translatesAutoresizingMaskIntoConstraints = false

        let container = NSView(frame: frame)
        container.autoresizingMask = [.width, .height]
        container.addSubview(webView)
        container.addSubview(loadingStack)
        NSLayoutConstraint.activate([
            loadingStack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            loadingStack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            loadingStack.widthAnchor.constraint(lessThanOrEqualToConstant: 560),
            loadingLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 560),
        ])
        window.contentView = container
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        supervisor.start(
            onReady: { [weak self] url in
                guard let self else { return }
                self.spinner.stopAnimation(nil)
                self.loadingStack.isHidden = true
                self.webView.isHidden = false
                self.webView.load(URLRequest(url: url))
            },
            onProgress: { [weak self] message in
                self?.loadingLabel.stringValue = message
            },
            onFailure: { [weak self] message in
                self?.spinner.stopAnimation(nil)
                self?.loadingTitle.stringValue = "Couldn’t start Hermes"
                self?.loadingLabel.stringValue = "⚠️ \(message)\nSee Console.app → Hermes for details."
            })
    }

    // Quitting when the window closes gives us a single, predictable teardown path.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        supervisor.stop()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
