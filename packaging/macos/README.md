# Hermes.app — macOS desktop packaging

Wraps the Hermes WebUI in a native macOS window (`.app`) and ships it as a
`.dmg`. The app owns the whole lifecycle: launching it starts the backend (and
installs the Hermes agent on first run); **quitting it stops the frontend, the
backend, and the Hermes agent** — nothing lingers.

## Pieces

| File | Role |
|------|------|
| `HermesApp/main.swift` | Native `WKWebView` window. Starts `supervisor.py`, shows a loading screen until it prints `HERMES-READY`, loads the local URL, and on quit tears the supervisor (and its whole process tree) down. |
| `supervisor.py` | The lifecycle manager the app launches. Installs the agent on first run, starts the WebUI in its own process group, health-checks it, and on SIGTERM / parent-death stops the WebUI **and** any background `hermes gateway`. Includes a watchdog so a force-quit of the app still cleans up. |
| `Info.plist` / `entitlements.plist` | Bundle metadata + hardened-runtime entitlements the bundled Python needs. |
| `build.sh` | Fetches a relocatable CPython, assembles the bundle, compiles the Swift shell, optionally signs + notarizes, and builds the DMG. |

## How "close → everything stops" works

```
Hermes.app (Swift)                    ⌘Q / close window / force-quit
   └─ supervisor.py  ◄── SIGTERM ──┘   (or watchdog: getppid()==1)
        └─ bootstrap.py / server.py    ← WebUI, runs the agent in-process
             └─ agent + terminal subprocesses   (same process group)
```

On quit the app SIGTERMs the supervisor; the supervisor SIGTERMs the WebUI
process group, runs `hermes gateway stop`, then SIGKILLs the group as a backstop.
The WebUI runs the agent **in-process** (`from run_agent import AIAgent`), so
stopping the WebUI stops the agent.

## Build

Prereqs: Xcode command-line tools, `create-dmg` (`brew install create-dmg`),
network access (downloads CPython + installs the agent on first app launch).

```bash
cd packaging/macos

# 1) Local test build (unsigned) — runs only on THIS Mac (Gatekeeper will warn).
./build.sh

# 2) Signed build for distribution to others.
export SIGN_IDENTITY="Developer ID Application: bo wang (772APYS786)"
./build.sh --sign

# 3) Signed + notarized + stapled DMG (what you actually ship).
#    One-time: store your notarization credentials in the keychain:
xcrun notarytool store-credentials hermes-notary \
  --apple-id YOUR_APPLE_ID --team-id 772APYS786 --password APP_SPECIFIC_PASSWORD
export SIGN_IDENTITY="Developer ID Application: bo wang (772APYS786)"
./build.sh --notarize          # uses keychain profile "hermes-notary"
```

Output: `build/Hermes.app` and `build/Hermes-<version>.dmg`.

## Notes / gotchas

- **The agent is NOT bundled** (its ML venv is multi-GB and arch-specific). The
  app installs it on first launch via the official installer into `~/.hermes`.
  First launch therefore needs internet and takes a few minutes.
- **Notarization needs an Apple Developer account** ($99/yr) and an app-specific
  password. Without notarization, other users get a Gatekeeper "damaged" warning.
- **Architecture**: `build.sh` bundles CPython for the *host* arch. Build on an
  Apple-Silicon Mac for an arm64 app; build on / for Intel separately, or extend
  the script to assemble a universal Python if you need a single fat binary.
- **App icon**: drop an `AppIcon.icns` in this folder before building to brand it.
- `HERMES_PY_MINOR` (default `3.12`) selects the bundled CPython series.
