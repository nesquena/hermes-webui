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
            "-o", "StrictHostKeyChecking=no",
            "-o", "ExitOnForwardFailure=yes",
            "-L", "\(localPort):\(remoteHost):\(remotePort)",
            "\(user)@\(host)"
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

        DispatchQueue.global().asyncAfter(deadline: .now() + 2.0) {
            DispatchQueue.main.async {
                if p.isRunning {
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
        monitorTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            guard let self = self, let p = self.process else { return }
            if !p.isRunning {
                self.setStatus(.disconnected)
                self.monitorTimer?.invalidate()
            }
        }
    }

    private func setStatus(_ newStatus: TunnelStatus) {
        status = newStatus
        onStatusChange?(newStatus)
    }
}
