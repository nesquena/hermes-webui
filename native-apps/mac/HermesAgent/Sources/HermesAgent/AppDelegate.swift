import Cocoa

class AppDelegate: NSObject, NSApplicationDelegate {

    let appTitle = "Hermes Agent"

    let defaultSSHUser    = "root"
    let defaultSSHHost    = "your-server.com"
    let defaultLocalPort  = "8080"
    let defaultRemotePort = "8080"
    let defaultTargetURL  = "http://localhost:8080"

    var tunnelManager: TunnelManager!
    var splashWindow: SplashWindowController!
    var browserWindow: BrowserWindowController?
    var preferencesWindow: PreferencesWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        setupMenu()
        seedDefaultsIfNeeded()
        startTunnel()
    }

    func seedDefaultsIfNeeded() {
        let defaults = UserDefaults.standard
        if defaults.string(forKey: "sshUser") == nil {
            defaults.set(defaultSSHUser,    forKey: "sshUser")
            defaults.set(defaultSSHHost,    forKey: "sshHost")
            defaults.set(defaultLocalPort,  forKey: "localPort")
            defaults.set(defaultRemotePort, forKey: "remotePort")
            defaults.set(defaultTargetURL,  forKey: "targetURL")
        }
    }

    func startTunnel() {
        let defaults = UserDefaults.standard
        let user       = defaults.string(forKey: "sshUser")    ?? defaultSSHUser
        let host       = defaults.string(forKey: "sshHost")    ?? defaultSSHHost
        let localPort  = Int(defaults.string(forKey: "localPort")  ?? defaultLocalPort)  ?? 8080
        let remotePort = Int(defaults.string(forKey: "remotePort") ?? defaultRemotePort) ?? 8080
        let targetURL  = defaults.string(forKey: "targetURL")  ?? defaultTargetURL

        tunnelManager?.stop()
        tunnelManager = TunnelManager(
            user: user,
            host: host,
            localPort: localPort,
            remoteHost: "localhost",
            remotePort: remotePort
        )

        splashWindow = SplashWindowController(title: appTitle)
        splashWindow.showWindow(nil)
        browserWindow?.close()
        browserWindow = nil

        tunnelManager.onStatusChange = { [weak self] status in
            guard let self = self else { return }
            self.browserWindow?.updateStatus(status, host: host, port: localPort)
        }

        tunnelManager.start {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                self.splashWindow.close()
                let browser = BrowserWindowController(
                    urlString: targetURL,
                    title: self.appTitle
                )
                browser.onReconnect = { [weak self] in
                    self?.startTunnel()
                }
                browser.updateStatus(self.tunnelManager.status, host: host, port: localPort)
                browser.showWindow(nil)
                self.browserWindow = browser
            }
        }
    }

    func setupMenu() {
        let menuBar = NSMenu()

        let appMenuItem = NSMenuItem()
        menuBar.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenuItem.submenu = appMenu
        appMenu.addItem(withTitle: "About \(appTitle)", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Preferences…", action: #selector(openPreferences), keyEquivalent: ",")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit \(appTitle)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let windowMenuItem = NSMenuItem()
        menuBar.addItem(windowMenuItem)
        let windowMenu = NSMenu(title: "Window")
        windowMenuItem.submenu = windowMenu
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Zoom", action: #selector(NSWindow.zoom(_:)), keyEquivalent: "")

        NSApp.mainMenu = menuBar
    }

    @objc func openPreferences() {
        if preferencesWindow == nil {
            preferencesWindow = PreferencesWindowController()
            preferencesWindow?.onSave = { [weak self] in
                self?.preferencesWindow = nil
                self?.startTunnel()
            }
        }
        preferencesWindow?.showWindow(nil)
        preferencesWindow?.window?.makeKeyAndOrderFront(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        tunnelManager?.stop()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool {
        return true
    }
}
