# Desktop Pet Beta

Desktop Pet is an optional desktop companion for Hermes WebUI. It keeps a small
native pet outside the browser and shows session attention states while the
main WebUI continues to own chat, settings, auth, and session history.

## Status

- Desktop-only beta for macOS and Windows.
- macOS has local validation coverage; Windows host validation is still a
  follow-up gate before calling the feature production-ready on Windows.
- Source-only first slice: no signed installer, release bundle, notarization,
  auto-update channel, or packaged binary distribution is included.
- Disabled by default: the pet is not installed, launched, or shown unless the
  user opts in through Settings -> Appearance or `/pet wakeup`.
- The default bundled skin is `keeper`, displayed to users as `May`.
- Not part of the mobile or tablet WebUI experience.

## What It Does

The pet is an ambient status surface for long-running Hermes sessions. It is not
a replacement for the WebUI and does not duplicate the full chat interface.

The pet can show:

- Running sessions with elapsed time and current progress text.
- Ready sessions that completed and need a quick return path.
- Approval requests for commands that need a user decision.
- Clarify prompts, including fixed choices and custom replies.
- Inline reply cards for a focused response without switching browser tabs.

Clicking a bubble opens or focuses the matching WebUI session. Approval,
clarify, and reply actions submit through the existing WebUI session path rather
than a separate desktop-only state store.

## How To Use

- Settings -> Appearance exposes the Desktop Pet (Beta) control. It describes
  the pet as an ambient companion first; first launch may set up the local shell
  before the pet appears. During a long first build, Settings and `/pet wakeup`
  keep showing non-repeating progress copy and then elapsed-time updates instead
  of sitting on one static message.
- `/pet wakeup` launches the pet when the native shell is available.
- `/pet sleep` closes the pet and disables future autostart.
- The native right-click menu supports skin switching, pet reload, and close.
- Closing the pet from the native menu saves the disabled preference so the pet
  does not restart automatically on the next WebUI page load.

The Settings toggle and `/pet wakeup` go through the same WebUI install/launch
flow. If the native shell is missing, WebUI attempts the local Desktop Pet setup
path before launch; it does not silently show a pet on a fresh WebUI start.
After first launch, the pet shows session bubbles first when active work exists.
If there is no active session bubble, it can show a short Welcome Card with a
countdown and a Got it action.

## First Launch Experience

Desktop Pet is opt-in and source-only in this slice. On a fresh checkout or after
local artifacts are cleared, the first launch may need to prepare the local
Tauri/Rust shell before the pet appears.

During that setup:

- Settings and `/pet wakeup` use the same install-before-launch path.
- The setup request allows for a long first build instead of failing on a short
  UI timeout.
- Settings inline status and slash-command toasts keep changing while setup is
  still running.
- Progress copy uses friendly, non-repeating stage messages and then switches
  to elapsed-time updates if setup runs longer than the predefined stages.
- The native pet window should not briefly flash a first-start install card or
  pet image before the real pet appears.
- Success is reported only after the native pet launch succeeds.

Once the pet appears, session attention wins over onboarding. If running,
ready, approval, or clarify bubbles already exist, the pet enters work mode
immediately. If no session bubble is available, the bubble window may show a
Welcome Card with a title, feature-focused copy, a visible closing countdown, and
a `Got it` action. A real session bubble that arrives while the Welcome Card is
pending or visible hides it without marking the card as seen.

## Runtime Model

The Hermes WebUI server must be running before the native pet starts. The native
Tauri shell receives the current loopback WebUI base URL from `/api/pet/launch`
through `HERMES_DESKTOP_PET_WEBUI_BASE`, then loads:

```text
<current WebUI base>/pet
<current WebUI base>/pet/bubbles
```

The shell registers its PID and WebUI base URL in the active WebUI state
directory. That lets `/api/pet/status`, `/api/pet/launch`, and `/api/pet/close`
distinguish an isolated development runtime such as `8788` from the normal
`8787` runtime.

Pet control endpoints are loopback-only. Browser-originating POST requests keep
the normal WebUI auth and CSRF checks. Session navigation uses a narrow bridge:
an already-open WebUI tab can consume the pet command and acknowledge it; if no
live bridge is available, WebUI opens a sanitized loopback session URL in the
browser.

## Included Surfaces

- Settings -> Appearance Desktop Pet (Beta) control.
- `/pet wakeup` and `/pet sleep` slash commands.
- `/pet` and `/pet/bubbles` standalone pet pages.
- `api/pet_routes.py` pet-owned routes for attention, skins, status, launch,
  close, preference sync, registration, and session navigation.
- `static/pet_bridge.js` for browser-tab command consumption and acknowledgement.
- `static/desktop_pet/` for the pet and bubble UI.
- `static/pets/` for bundled skin manifests and spritesheets.
- `desktop-pet/` for the thin native Tauri shell.

## Boundaries

- Desktop Pet is not a remote-control API surface. Control routes require
  loopback access and the browser routes keep WebUI auth boundaries.
- It does not add mobile or tablet UI.
- It does not ship a signed installer, notarized app, auto-updater, or release
  artifact in this slice.
- Extra bundled skins are included for review and can be trimmed before release
  if maintainers want a smaller first ship.
- Optional pet pages and spritesheets are not pre-cached by the main service
  worker, so ordinary WebUI users do not pay the offline-cache cost for a beta
  desktop surface.

## Local Development

Start WebUI first, then run the native shell from `desktop-pet/`:

```bash
npm install
HERMES_DESKTOP_PET_WEBUI_BASE=http://127.0.0.1:8787 npm run dev
```

For isolated PR validation, use the development runtime and state directory
described in `TESTING.md`, normally on port `8788`. Do not use the port alone as
the isolation proof; confirm the WebUI state directory and Hermes home are the
development ones.

## Troubleshooting

If the pet does not appear:

- Confirm WebUI is running and `/health` returns ok.
- Confirm the native shell dependencies were installed in `desktop-pet/`.
- Check `/api/pet/status` from the same loopback WebUI runtime.

If the pet connects to the wrong runtime:

- Relaunch it through the intended WebUI runtime instead of starting the shell
  directly.
- Check that `HERMES_DESKTOP_PET_WEBUI_BASE` points at the intended loopback
  port.
- Confirm the registered pet base URL matches the runtime under test.

If bubble clicks open the wrong browser or do nothing:

- Keep an authenticated WebUI tab open for the bridge path.
- If no WebUI tab is open, the fallback should open the sanitized loopback
  session URL.
- Restart or reload the pet if the embedded WebView has stale cached assets.

If hidden bubbles block clicks:

- Collapse and expand the badge once, or reload the pet.
- Treat any hidden transparent native bubble window intercepting desktop clicks
  as a regression.

## Manual Acceptance

Use the Desktop Pet checklist in
[`TESTING.md`](../TESTING.md#desktop-pet-macos-beta-acceptance) before moving the
PR from draft to ready.

## Follow-ups

- Windows host validation.
- Packaging, signing, notarization, and release distribution decisions.
- Final bundled skin policy.
- Screenshot or short-video evidence for Settings, pet window, running/ready
  bubbles, approval/clarify bubbles, overflow, and edge placement.
