# Hermes Agent (macOS)

A native macOS app for accessing the Hermes Web UI via SSH tunnel.

## Building

```bash
cd native-apps/mac/HermesAgent
swift build -c release
./build.sh
```

## Installation

The build script installs to `/Applications/Hermes Agent.app`.

## Security Notes

This app is not signed or notarized. macOS may warn that the app is unsigned. In that case:

- Right-click the app and choose "Open", or
- Run: `xattr -dr com.apple.quarantine "Hermes Agent.app"`

For production use, consider code signing and notarization with an Apple Developer account.

## Features

- SSH tunnel to Hermes Web UI
- Clipboard integration (text and images)
- Native preferences window
- Status indicator

## Configuration

Configure SSH connection details and target URL in the app's Preferences window.