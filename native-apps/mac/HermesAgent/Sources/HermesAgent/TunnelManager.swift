import Foundation

enum TunnelStatus {
    case connecting
    case connected
    case disconnected
}

class TunnelManager {
    private var process: Process?
    private let user: String
    private let host: String
    private let localPort: Int
    private let remoteHost: String
    private let remotePort: Int

    var onStatusChange: ((TunnelStatus) -> Void)?
    private(set) var status: TunnelStatus = .connecting
    private var monitorTimer: Timer?

    init(user: String, host: String, localPort: Int, remoteHost: String, remotePort: Int) {
        self.user = user
        self.host = host
        self.localPort = localPort
        self.remoteHost = remoteHost
        self.remotePort = remotePort
    }

    func start(onReady: @escaping () -> Void) {
        setStatus(.connecting)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        p.arguments = [
            "-N",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ExitOnForwardFailure=yes",
            "-L", "\(localPort):\(remoteHost):\(remotePort)",
            "\(user)@\(host)",
        ]

        let pipe = Pipe()
        p.standardError = pipe

        // Detect if process dies unexpectedly
        p.terminationHandler = { [weak self] process in
            guard let self = self else { return }
            if process.terminationReason == .exit && process.terminationStatus != 0 {
                DispatchQueue.main.async {
                    self.setStatus(.disconnected)
                }
            }
        }

        do {
            try p.run()
            self.process = p
            print("SSH tunnel started (pid \(p.processIdentifier))")
        } catch {
            print("Failed to start SSH: \(error)")
            setStatus(.disconnected)
            onReady()
            return
        }

        DispatchQueue.global().async { [weak self] in
            guard let self = self else { return }
            let connected = self.waitForPortForward(timeout: 5.0, interval: 0.5)
            DispatchQueue.main.async {
                if connected {
                    self.setStatus(.connected)
                    self.startMonitoring()
                } else {
                    self.setStatus(.disconnected)
                }
                onReady()
            }
        }
    }

    func stop() {
        monitorTimer?.invalidate()
        monitorTimer = nil
        guard let p = process else { return }
        let pid = p.processIdentifier
        p.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 1.0) {
            if p.isRunning { kill(pid, SIGKILL) }
        }
        process = nil
    }

    private func startMonitoring() {
        monitorTimer?.invalidate()
        monitorTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) {
            [weak self] _ in
            guard let self = self, let p = self.process else { return }
            if !p.isRunning {
                self.setStatus(.disconnected)
                self.monitorTimer?.invalidate()
            }
        }
    }

    private func waitForPortForward(timeout: TimeInterval = 5.0, interval: TimeInterval = 0.5)
        -> Bool
    {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if process?.isRunning != true {
                return false
            }
            if portIsListening(localPort) {
                return true
            }
            Thread.sleep(forTimeInterval: interval)
        }
        return false
    }

    private func portIsListening(_ port: Int) -> Bool {
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return false }
        defer { close(sock) }

        var addr = sockaddr_in()
        addr.sin_len = __uint8_t(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(port).bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")

        return withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPtr in
                connect(sock, sockaddrPtr, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }

    private func setStatus(_ newStatus: TunnelStatus) {
        status = newStatus
        onStatusChange?(newStatus)
    }
}
