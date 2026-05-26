# Hermes Desktop Pet

User-facing product docs live in
[`../docs/desktop-pet.md`](../docs/desktop-pet.md). This file documents the
native Tauri shell, local development flow, route ownership, skin manifests,
and packaging boundary.

This is a thin desktop shell for the optional standalone Hermes pet page.

It intentionally does not reimplement Hermes UI. At launch, the shell loads the
current loopback WebUI base URL supplied by `/api/pet/launch` through
`HERMES_DESKTOP_PET_WEBUI_BASE`, then opens:

```text
<current WebUI base>/pet
<current WebUI base>/pet/bubbles
```

If the shell is run directly without that environment variable, it falls back to
`http://127.0.0.1:8787` for local development.

The Hermes WebUI server must already be running first. Starting WebUI alone does
not show the pet; the pet only appears when this Tauri shell is launched. The
WebUI Settings → Appearance desktop pet switch is a long-term preference: when
enabled, the desktop WebUI page will try to start the pet on the next page load.
Turning the same switch off calls `/api/pet/close`.

For local testing:

```bash
HERMES_WEBUI_PORT=8787 ./start.sh
```

Then run the shell from this directory:

```bash
npm install
HERMES_DESKTOP_PET_WEBUI_BASE=http://127.0.0.1:8787 npm run dev
```

Window intent:

- transparent background
- no native decorations
- always on top
- skipped from the taskbar / dock where supported
- pet-sized transparent viewport whose runtime size follows the active skin layout
- separate bubble window with dynamic height and top/bottom placement around the pet window
- right-click menu for switching detected skins, restarting the pet, or closing it

The bubble window is not only for session cards. It can render work attention
bubbles, the first-launch Welcome Card, and short ready/status toasts. Session
attention has priority: if the WebUI reports running, ready, approval, or
clarify work, the bubble window should show that work before onboarding copy.
When there is no session attention, the Welcome Card may appear once with a
closing countdown and a `Got it` action. A zero-attention update from the main
pet page must not hide an already-visible non-session bubble mode such as the
Welcome Card.

First-time shell preparation is driven by WebUI, not by a native install card in
this shell. Settings and `/pet wakeup` show setup progress while `/api/pet/install`
prepares the local shell; the native bubble window should not flash a transient
install card before the pet appears.

The default bundled skin is `keeper` / `May`. Additional skins can be added under
`static/pets/<id>/pet.json` plus a local spritesheet; the WebUI exposes the
detected list through `/api/pet/skins`.

Skin manifests use:

- `id`: directory-safe skin id matching `static/pets/<id>`
- `displayName`: human-readable name
- `spritesheetPath`: local spritesheet path inside the skin directory
- optional `layout`: normalized spritesheet layout
  - default is `8` columns × `9` rows
  - default frame size is `192 × 208`
  - states are `idle`, `running-right`, `running-left`, `waving`, `jumping`,
    `failed`, `waiting`, `running`, and `review`

The shell is backed by lazy WebUI endpoints:

- `/pet` serves the standalone pet page.
- `/pet/bubbles` serves the separate bubble-window page.
- `/api/pet/attention` returns the final display list for sessions that need attention.
- `/api/pet/skins` lists bundled and locally added skins.
- `/api/pet/navigation` lets an already-open WebUI tab consume pet commands.
- `/api/pet/navigation_ack` acknowledges that a WebUI tab consumed a pet command.
- `/api/pet/open_session` queues a session jump or reply through the existing
  WebUI bridge path, waits briefly for an acknowledgement from an open WebUI
  page, and uses a sanitized loopback browser fallback only when no page consumes
  the command.
- `/api/pet/register` records the native shell PID and current WebUI base URL in
  the active WebUI state directory so launch/status can distinguish isolated
  runtimes such as `8788` from the default `8787` pet.
- `/api/pet/status` checks whether a launchable native shell is already present
  and running for the current WebUI base URL.
- `/api/pet/install` prepares the local native shell when it is missing.
- `/api/pet/launch` starts the native desktop shell from a loopback WebUI
  request when an installed app, built binary, or local Tauri dev setup is
  available. Launch is single-instance for the same WebUI base URL; a registered
  pet from another base URL is closed before launching the current one, while an
  unregistered existing process is left alone and reported as a conflict.
- `/api/pet/close` stops the running desktop pet from a loopback WebUI request.

This is a desktop-only beta for macOS and Windows. macOS has been locally
verified; Windows is source-compatible but should be treated as beta until
verified on a Windows host. It is not part of the mobile or tablet WebUI surface,
and packaging/signing/release artifacts are intentionally outside this first
integration slice.
