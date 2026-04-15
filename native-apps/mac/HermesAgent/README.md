# Hermes Agent (macOS)

A native macOS app for accessing the Hermes Web UI locally or via SSH tunnel.

## Building

```bash
cd native-apps/mac/HermesAgent
swift build -c release
./build.sh
```

## Installation

The build script installs to `/Applications/Hermes Agent.app`.

## Connection Modes

The app supports two connection types:

- **Direct (Local)** — Connect directly to a local Hermes Web UI instance. Default configuration is `http://localhost:8787`. Use this if you run hermes-webui on your machine.
- **SSH Tunnel** — Connect to a remote Hermes Web UI instance through an SSH tunnel. Configure SSH credentials (username, host, local/remote ports) and the app will forward the connection securely.

Switch between modes in Preferences → Connection Mode.

## Security Notes

This app is not signed or notarized. macOS may warn that the app is unsigned. In that case:

- Right-click the app and choose "Open", or
- Run: `xattr -dr com.apple.quarantine "Hermes Agent.app"`

For production use, consider code signing and notarization with an Apple Developer account.

### SSH Security

SSH connections use `StrictHostKeyChecking=accept-new` to prevent MITM attacks while allowing first-run connections. New hosts are automatically added to `~/.ssh/known_hosts`. If a host key changes unexpectedly, the connection will fail as a safety measure.

## Features

- Direct local access or SSH tunnel to Hermes Web UI
- Clipboard integration (text and images)
- Native preferences window with mode switching
- Real-time tunnel connection status indicator
- Safe signal handling for graceful shutdown

## Configuration

Configure connection settings in Preferences:

1. Choose connection mode (Direct or SSH Tunnel)
2. For SSH mode: enter username, host, and port mappings
3. Set target URL (verified as valid http/https)
4. Click "Save & Reconnect"