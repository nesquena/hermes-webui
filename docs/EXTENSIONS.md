# WebUI Extensions

Hermes WebUI supports a small, opt-in extension surface for self-hosted installs.
It lets an administrator serve local static assets and inject same-origin CSS or
JavaScript into the app shell without editing the WebUI source tree.

> **Trust model — read this first.** Extensions execute with full WebUI session
> authority. An extension JS file can call any API the logged-in user can call,
> including reading conversation history, sending messages, modifying settings,
> and triggering tool actions. **Only enable extensions you wrote yourself or
> from sources you trust as much as the WebUI source itself.** If your WebUI is
> shared with users you do not fully trust, do not enable extensions.
> If you set `HERMES_WEBUI_EXTENSION_DIR` yourself, do not point it at a
> user-writable directory on a shared host.

This is intentionally not a plugin marketplace or dependency system. It is a
safe escape hatch for local dashboards, internal tooling, and workflow-specific
panels that should not live in core Hermes WebUI.

## What extensions can do

Extensions can:

- serve files from one configured local directory at `/extensions/...`
- inject configured same-origin stylesheets into `<head>`
- inject configured same-origin scripts before `</body>`
- read a local JSON manifest that lists bundled scripts/styles to inject
- call the normal WebUI APIs available to the browser session
- call trusted local loopback sidecars directly from extension JavaScript when
  the browser Content Security Policy allows that origin

Extensions cannot, by themselves:

- bypass WebUI authentication
- serve files outside the configured extension directory
- load third-party scripts/styles through the built-in injection config
- register new WebUI backend routes or proxy arbitrary sidecar/backend traffic
- change Hermes Agent permissions, models, memory, or tools unless they call
  existing authenticated APIs that already allow those changes

## Configuration

### One-click install (no configuration required)

For a single-user self-hosted instance you do not need to configure anything.
Open **Settings → Extensions**, pick an extension from the gallery, and click
**Install** — it just works. The first install creates a WebUI-managed
extension directory under your state dir (`STATE_DIR/extensions`, e.g.
`~/.hermes/webui/extensions/`) and installs into it; gallery-installed
extensions load automatically on the next app-shell render with no environment
variables and no restart of your shell.

The managed directory lives alongside your sessions and settings in the
WebUI-owned state dir. That is a different trust domain from "a world-writable
directory on a shared box": only the WebUI process (and whoever can already
write your `~/.hermes` state) can place code there. The trust model below still
applies — installed extension code runs with full session authority — so only
install extensions from the vetted gallery or sources you trust as much as the
WebUI source itself.

Some gallery entries need more than WebUI assets. If an extension declares
post-install guidance or lifecycle requirements such as a loopback sidecar or a
native host, Settings -> Extensions shows a **Next step** note on the card after
install. For example, Desktop Companion can install the WebUI bridge from the
gallery, but the desktop pet is only visible after the local Desktop Companion
app is started.

### Manual / advanced configuration (optional)

`HERMES_WEBUI_EXTENSION_DIR` is **optional** and overrides the managed default.
Set it when you want extensions to live in a specific directory you control
(e.g. a checked-out bundle, or a path mounted into a container). When set it
must point to an existing directory before any script or stylesheet URLs are
injected; WebUI never auto-creates an admin-specified path:

```bash
export HERMES_WEBUI_EXTENSION_DIR=/path/to/my-extension/static
export HERMES_WEBUI_EXTENSION_SCRIPT_URLS=/extensions/app.js
export HERMES_WEBUI_EXTENSION_STYLESHEET_URLS=/extensions/app.css
./start.sh
```

Multiple URLs may be comma-separated:

```bash
export HERMES_WEBUI_EXTENSION_SCRIPT_URLS=/extensions/runtime.js,/extensions/app.js
export HERMES_WEBUI_EXTENSION_STYLESHEET_URLS=/extensions/base.css,/extensions/theme.css
```

For bundled or multi-extension installs, you may list assets in a manifest file
inside `HERMES_WEBUI_EXTENSION_DIR` instead of maintaining long comma-separated
environment variables:

```bash
cat > ~/.hermes/webui-extension-bundle/extensions.json <<'JSON'
{
  "extensions": [
    {
      "id": "templates",
      "scripts": ["templates/templates.js"],
      "stylesheets": ["templates/templates.css"]
    },
    {
      "id": "sidebar-tools",
      "scripts": ["sidebar-tools/sidebar-tools.js"],
      "stylesheets": ["sidebar-tools/sidebar-tools.css"]
    }
  ]
}
JSON

HERMES_WEBUI_EXTENSION_DIR=~/.hermes/webui-extension-bundle \
HERMES_WEBUI_EXTENSION_MANIFEST=extensions.json \
./start.sh
```

Manifest entries use the same URL safety rules as the environment variables.
Bare relative entries such as `templates/templates.js` resolve to
`/extensions/templates/templates.js`; absolute same-origin entries such as
`/extensions/shared.js` or `/static/theme.css` are also accepted. A manifest may
be an object with top-level `scripts` / `stylesheets`, an object with an
`extensions` array, or a top-level array of extension objects. Disabled entries
may be kept in the manifest with the JSON boolean `"enabled": false`. Explicit
`HERMES_WEBUI_EXTENSION_SCRIPT_URLS` and
`HERMES_WEBUI_EXTENSION_STYLESHEET_URLS` still work and are appended after
manifest assets, with duplicates ignored.

When an extension is installed from Settings -> Extensions, WebUI records the
installed package and loads that package's `manifest.json` automatically on the
next app-shell render. In this gallery-installed mode, a manifest located at
`HERMES_WEBUI_EXTENSION_DIR/<extension-id>/manifest.json` resolves bare relative
assets relative to that package directory. For example,
`"scripts": ["assets/companion-adapter.js"]` in
`desktop-companion/manifest.json` injects
`/extensions/desktop-companion/assets/companion-adapter.js`.

Manual manifests configured with `HERMES_WEBUI_EXTENSION_MANIFEST` follow the
same rule: relative assets resolve from the manifest file's directory. A root
manifest such as `extensions.json` keeps the existing
`/extensions/<asset-path>` behavior, while a subdirectory manifest such as
`desktop-companion/manifest.json` resolves relative assets under
`/extensions/desktop-companion/`.

Extension entries may also declare a read-only loopback sidecar for diagnostics:

```json
{
  "extensions": [
    {
      "id": "desktop-companion",
      "name": "Desktop Companion",
      "scripts": ["companion-adapter.js"],
      "stylesheets": ["companion-adapter.css"],
      "sidecar": {
        "type": "loopback",
        "origin": "http://127.0.0.1:17787",
        "health_path": "/health"
      }
    }
  ]
}
```

Loopback sidecars do **not** change asset injection behavior. They are only
reported by diagnostics so an operator can see that a local companion service was
declared and optionally check its health from the browser.

Extension entries may declare browser-local settings when they also request
extension-owned storage:

```json
{
  "id": "desktop-companion",
  "permissions": {
    "storage": {
      "owned": true
    }
  },
  "settings_schema": [
    {
      "key": "show_badge",
      "type": "boolean",
      "label": "Show badge",
      "default": true
    },
    {
      "key": "mode",
      "type": "enum",
      "label": "Mode",
      "options": [
        {"value": "compact", "label": "Compact"},
        {"value": "full", "label": "Full"}
      ],
      "default": "compact"
    }
  ]
}
```

Settings are a first-pass browser feature. WebUI sanitizes the manifest schema, injects the accepted schema before extension scripts, and leaves persistence to the browser. The backend does not store extension settings or expose a generic settings write route, and it does not treat these values as secrets.

The sanitizer accepts only `boolean`, `string`, `number`, `integer`, and `enum`
fields. It drops `sensitive: true` fields, unsupported types, malformed enum
options, duplicate keys after the first valid field, and defaults that do not
match the declared type. `settings_schema` is honored only when
`permissions.storage.owned` is exactly `true`.

Extension scripts can use the sanctioned browser accessors:

```js
const settings = window.HermesExtensionSettings.settingsForExtension("desktop-companion");
const value = settings.get("show_badge");
settings.set("show_badge", false);

const storage = window.HermesExtensionSettings.storageForExtension("desktop-companion");
storage.set("lastPanel", "settings");

const sameSettings = window.hermesExt.settings.forExtension("desktop-companion");
const sameStorage = window.hermesExt.storage.forExtension("desktop-companion");
```

Settings persist only non-default overrides. Resetting settings removes those
overrides and returns schema defaults. Extension-owned storage uses a separate
browser-local namespace, and clearing storage removes that namespace without
changing settings.

## URL rules

Injected asset URLs are deliberately restricted:

- must be same-origin paths
- must start with `/extensions/` or `/static/` after manifest normalization
- must not include a URL scheme, host, fragment, quote, angle bracket, newline,
  NUL byte, or backslash
- must not contain dot-segments or dotfiles after percent-decoding

Allowed examples:

```text
/extensions/app.js
/extensions/app.css
/extensions/app.js?v=1
/static/theme.css
```

Rejected examples:

```text
https://example.com/app.js
//example.com/app.js
javascript:alert(1)
/api/session
/extensions/app.js#fragment
```

These restrictions keep the existing Content Security Policy intact and avoid
turning the extension hook into a third-party script loader. Invalid configured
URLs are ignored rather than injected.

## Trusted local sidecars

Manifest-bundled extensions may integrate with a trusted local sidecar process,
such as a desktop companion listening on `http://127.0.0.1:17787`. The injected
extension JavaScript talks to that sidecar directly from the browser; Hermes
WebUI does not proxy those requests and does not create extension-owned backend
routes.

Loopback sidecar origins are already included in WebUI's enforced CSP
`connect-src` directive:

```text
http://127.0.0.1:*
http://localhost:*
http://ipc.localhost
ws://127.0.0.1:*
ws://localhost:*
```

The wildcard ports above cover any loopback port, including
`http://127.0.0.1:17787`. For a trusted non-loopback origin that you explicitly
control, append the exact origin with `HERMES_WEBUI_CSP_CONNECT_EXTRA` before
starting WebUI:

```bash
HERMES_WEBUI_CSP_CONNECT_EXTRA=https://companion.example.internal HERMES_WEBUI_EXTENSION_DIR=/path/to/my-extension/static HERMES_WEBUI_EXTENSION_MANIFEST=extensions.json ./start.sh
```

`HERMES_WEBUI_CSP_CONNECT_EXTRA` accepts space-separated `http(s)://` or
`ws(s)://` origins only. It rejects paths, directive injection, and invalid port
numbers. Avoid wildcard or remote origins unless you fully control the target;
extension JavaScript runs with the logged-in WebUI session's authority.

## Loopback sidecar declarations

Sidecar declarations are sanitized before they appear in diagnostics:

- only `"type": "loopback"` is supported
- `origin` must be an `http` or `https` origin on `127.0.0.1`, `localhost`, or
  `[::1]`
- `origin` must not include a username, password, path, query string, or fragment
- `health_path` is optional and defaults to `/health`
- when present, `health_path` must start with `/` and must not contain a scheme,
  host, query string, fragment, quotes, control characters, backslashes, empty
  segments, whitespace, or path traversal

Invalid sidecars are skipped with a stable warning code such as
`sidecar_origin_rejected`, `sidecar_type_unsupported`,
`sidecar_health_path_rejected`, or `sidecar_invalid`. Raw rejected origins and
paths are never returned by the status endpoint. If `health_path` is omitted,
diagnostics use `/health`; if `health_path` is present but invalid, the sidecar is
skipped rather than probed.

## Embedding an external web app in an iframe

By default the WebUI's Content-Security-Policy only allows it to embed
**same-origin** content in an `<iframe>` (the `frame-src` directive falls back to
`'self'`). An extension that wants to pin an external self-hosted web app — a
Grafana board, Vaultwarden, a personal dashboard — as a tab therefore needs the
operator to widen `frame-src`, opt-in, via an environment variable:

```bash
# space-separated http(s) origins; optional *. subdomain wildcard and port.
export HERMES_WEBUI_CSP_FRAME_EXTRA="https://grafana.example.com https://*.dash.example.com:8443"
```

Rules and guarantees:

- Only `http(s)` origins are accepted (an iframe `src` is always http(s)).
  Entries may include a `*.` subdomain wildcard and a port or `*` port; a path,
  a `ws://`/`wss://` scheme, an invalid port, or any attempt to inject another
  directive is rejected and the whole value is ignored (with a logged warning).
- This mirrors the existing `HERMES_WEBUI_CSP_CONNECT_EXTRA` knob (which widens
  `connect-src` for `fetch`/WebSocket); the two are independent.
- It only governs what the WebUI page may **embed**. It does **not** touch
  `frame-ancestors`, which stays `'none'` — so widening `frame-src` never lets
  another site embed the WebUI itself.
- Default-off: with the variable unset, the policy is unchanged (same-origin
  iframes only).

An "external app tab" extension should document the exact origin(s) it needs so
the operator can set this knob deliberately, rather than assuming a wide-open
policy.

## Static file serving

When `HERMES_WEBUI_EXTENSION_DIR` points at an existing directory, files under
that directory are available below `/extensions/`:

```text
/path/to/my-extension/static/app.js  ->  /extensions/app.js
/path/to/my-extension/static/ui.css  ->  /extensions/ui.css
```

The static handler is sandboxed:

- path traversal is rejected, including encoded traversal
- dotfiles and dot-directories are not served
- symlinks that resolve outside the extension directory are rejected
- missing or invalid extension directories behave as disabled
- manifest paths must be relative files inside the configured extension directory
- malformed, missing, or oversized manifests are ignored without enabling unsafe URLs
- failures return a generic 404 without exposing local filesystem paths

## Security notes

Only enable extensions from directories you control. Extension JavaScript runs in
the WebUI origin and can call the same authenticated WebUI APIs as the logged-in
browser session.

For shared or remotely exposed installations:

- keep `HERMES_WEBUI_PASSWORD` enabled
- bind to loopback unless you intentionally expose the service
- review extension code before enabling it
- prefer small, auditable extension files
- avoid serving generated or user-writable directories as extension roots

## Registering a custom theme (skin)

Extensions can contribute a custom **skin** that appears in the native
**Settings → Appearance** skin picker, instead of bolting on a parallel theme
switcher. Call `window.registerHermesSkin(descriptor)` from your extension
script:

```javascript
window.registerHermesSkin({
  name: 'E-Ink',            // display name (also the picker label)
  value: 'e-ink',           // optional stable key; slugified from name if omitted
  label: 'E-Ink',           // optional explicit picker label
  colors: ['#000000', '#ffffff', '#555555'],  // up to 3 preview swatches
  tokens: {                 // CSS design-token overrides for this skin
    '--bg': '#ffffff',
    '--surface': '#ffffff',
    '--text': '#000000',
    '--accent': '#000000',
    '--border': '#000000'
    // ...any of the allowed tokens below
  }
});
```

The call returns `true` on success and `false` if the descriptor was rejected
(so an extension can detect and log a bad theme). Once registered, the skin
shows up in the picker, can be selected, and persists across reloads exactly
like a built-in skin. Registering the same key again updates it in place
(idempotent), which is what a live theme editor relies on while the user edits.

**Core does the security-sensitive work for you.** Because token values are
written into CSS, every value is sanitized in core, once, so every theme
extension inherits the guard:

- **Allowed token names** (anything else is dropped): `--bg`, `--surface`,
  `--surface2`, `--surface-subtle`, `--text`, `--text2`, `--muted`, `--accent`,
  `--accent2`, `--accent3`, `--accent-contrast`, `--accent-hover`,
  `--accent-text`, `--accent-bg`, `--accent-bg-strong`, `--accent-rgb`,
  `--border`, `--border2`, `--hover-bg`, `--code-bg`, `--code-text`,
  `--sidebar`, `--sidebar-text`, `--user-bubble`, `--assistant-bubble`,
  `--success`, `--warning`, `--danger`, `--info`, `--link`.
- **Allowed value shapes** (anything else is dropped): hex colors, `rgb()` /
  `rgba()`, `hsl()` / `hsla()`, CSS color keywords, simple numeric-with-unit
  values (`px`/`em`/`rem`/`%`), and a bare RGB triple (e.g. `0, 0, 0` for
  `--accent-rgb`, which the app consumes inside `rgba(...)`). Values containing
  `url()`, `expression()`, semicolons, braces, or other CSS-injection vectors
  are rejected.
- **Reserved keys are protected** — an extension cannot overwrite a built-in
  skin key (e.g. `default`, `ares`, `graphite`).
- A descriptor with no valid tokens after sanitization is rejected entirely.

This is the supported, forward-looking way for theme-pack and theme-creator
extensions to integrate with the built-in appearance system.

## Registering a custom TTS engine

Extensions can contribute a **text-to-speech engine** that appears in the
**Settings → TTS Engine** dropdown alongside the built-ins (Browser / Edge /
ElevenLabs) and is used by **both** playback paths — the hands-free voice-mode
auto-read and the per-message "Listen" button. Call
`window.registerHermesTtsEngine(descriptor)`:

```javascript
window.registerHermesTtsEngine({
  id: 'voicevox',                 // [a-z0-9_-], not a built-in
  label: 'VOICEVOX (local)',      // shown in the dropdown (textContent — escaped)
  // synthesize(text, opts) -> Promise<ArrayBuffer | Blob | TypedArray> of audio.
  // opts carries the user's saved { voice, rate, pitch } (engine may ignore).
  synthesize(text, opts) {
    return fetch('http://127.0.0.1:50021/...', { /* ... */ })
      .then(r => r.arrayBuffer());
  }
});  // -> true on success, false if rejected
```

Rules and guarantees:

- **id** must be slug-safe (`[a-z0-9][a-z0-9_-]{0,31}`) and may **not** shadow a
  built-in engine (`browser`, `edge`, `elevenlabs`) — those are reserved.
- **label** is inserted with `textContent`, never `innerHTML` (no markup
  injection into the dropdown).
- `synthesize` must return audio bytes (`ArrayBuffer`, `Blob`, or a typed array);
  core coerces to an `ArrayBuffer` and plays it through the same `<audio>`
  lifecycle as the Edge engine (including stop/rearm in voice mode). A rejected
  promise or empty/invalid result fails gracefully (toast on the Listen button;
  re-listen in voice mode).
- Core owns selection, the dropdown option, and playback; the extension only
  produces audio. Re-registering the same id updates it in place.
- **Network note:** if your engine calls a local server (e.g. VOICEVOX on
  `http://127.0.0.1:50021`), that request is a same-origin-policy / CSP
  `connect-src` concern like any extension network call — loopback is already in
  the default `connect-src`. Declare `permissions.network_external` honestly
  based on where it calls.

## Extension authoring guidance

Extensions share the page with the WebUI app, so they should be additive and
reversible. Prefer small, well-scoped DOM changes that can be removed or hidden
without breaking the built-in Chat, Tasks, Settings, or session views.

Recommended patterns:

- create extension-specific containers with unique IDs or class prefixes
- add UI next to existing views instead of replacing large app containers
- keep event listeners scoped to extension-owned elements where possible
- preserve built-in navigation behavior and restore any view state you change
- use `hidden`, `aria-*`, and extension-scoped CSS for panels or overlays
- guard initialization so reloading or re-injecting the script does not create
  duplicate buttons, panels, timers, or event listeners

Avoid destructive mutations such as replacing `document.body.innerHTML`,
`main.innerHTML`, or other broad WebUI containers. Those patterns can remove or
mask the app's existing panels and leave normal navigation unable to recover
after an extension view is opened.

For custom pages, prefer adding a dedicated panel and toggling it alongside the
built-in views:

```javascript
(() => {
  if (document.getElementById('my-extension-panel')) return;

  const panel = document.createElement('section');
  panel.id = 'my-extension-panel';
  panel.className = 'main-view my-extension-panel';
  panel.hidden = true;
  panel.textContent = 'My extension page';

  document.querySelector('main')?.appendChild(panel);

  function showPanel() {
    document.querySelectorAll('main > .main-view').forEach((view) => {
      view.hidden = view !== panel;
    });
  }

  // Wire showPanel() to an extension-owned button or menu item.
})();
```

If host CSS overrides `[hidden]`, add an extension-scoped rule such as:

```css
.my-extension-panel[hidden] {
  display: none !important;
}
```

## Minimal example

Create a local extension directory:

```bash
mkdir -p ~/.hermes/webui-extension
cat > ~/.hermes/webui-extension/app.css <<'CSS'
.my-extension-badge {
  position: fixed;
  right: 12px;
  bottom: 12px;
  padding: 6px 10px;
  border-radius: 999px;
  background: #202236;
  color: #fff;
  font: 12px system-ui, sans-serif;
  z-index: 9999;
}
CSS
cat > ~/.hermes/webui-extension/app.js <<'JS'
(() => {
  const badge = document.createElement('div');
  badge.className = 'my-extension-badge';
  badge.textContent = 'Extension loaded';
  document.body.appendChild(badge);
})();
JS
```

Start WebUI with the extension enabled:

```bash
HERMES_WEBUI_EXTENSION_DIR=~/.hermes/webui-extension \
HERMES_WEBUI_EXTENSION_STYLESHEET_URLS=/extensions/app.css \
HERMES_WEBUI_EXTENSION_SCRIPT_URLS=/extensions/app.js \
./start.sh
```

Open the WebUI and confirm the badge appears.

## Diagnostics

Authenticated administrators can inspect sanitized extension configuration at:

```text
GET /api/extensions/status
```

The status endpoint is read-only and follows the normal WebUI authentication
rules. The same sanitized diagnostics are also shown in **Settings → Extensions**
for operators who prefer to inspect extension state from the browser. Installed
manifest entries can be enabled or disabled from that panel through the
authenticated `POST /api/extensions/toggle` endpoint. The toggle writes only a
WebUI-managed override in the WebUI state directory; it does not edit extension
manifests, fetch new extension assets, uninstall files, proxy sidecars, or add
extension-owned backend routes. Manifest entries with `"enabled": false` remain
manifest-disabled and cannot be re-enabled from WebUI.

The diagnostics return the same public asset URLs that can already be injected
into the HTML, plus coarse manifest status, per-extension effective state, asset
counts, sanitized declared loopback sidecars, and warning codes for rejected or
unavailable configuration. `manifest.script_count` and
`manifest.stylesheet_count` count accepted assets from the effectively enabled
manifest entries only; `manifest.sidecar_count` counts accepted enabled loopback
sidecars from the manifest. `counts.script_urls` and `counts.stylesheet_urls`
count the final post-env-merge URLs, while `counts.sidecars` counts the sanitized
sidecar list returned in `sidecars`. `counts.manifest_extensions` counts
sanitized manifest extension entries with valid IDs, and `counts.user_disabled`
counts installed manifest entries currently suppressed by the WebUI-managed
override. `manifest.entry_count` counts the loaded top-level manifest object and
effectively enabled extension entries that were inspected for injection, not
every extension object in the file. The endpoint and Settings panel do **not**
return `HERMES_WEBUI_EXTENSION_DIR`, resolved manifest paths, raw environment
values, rejected URL strings, rejected sidecar origins, rejected health paths, or
the override state-file path.

When sanitized loopback sidecars are present, **Settings → Extensions** renders a
read-only sidecar monitor card. The browser checks each declared `health_url`
directly with `fetch(..., { credentials: 'omit', cache: 'no-store' })` and a
short timeout. WebUI does **not** proxy sidecar requests and does not send WebUI
cookies to sidecars. A successful HTTP response is shown as healthy, a non-OK
HTTP response as unhealthy, and CORS/network/timeouts as unreachable or blocked;
raw health response bodies are never rendered. If a healthy response includes an
optional top-level `runtime` object, the panel may parse it and render only
allowlisted scalar fields such as `sidecar`, `native_host`, `bridge`,
`last_seen_at`, and `webui_origin`. This keeps sidecar-specific diagnostics
machine-readable without making WebUI depend on any one extension's private
payload shape.

When sanitized settings are present, Settings -> Extensions renders
browser-local controls for installed manifest entries. Save, reset, and clear
storage actions call `window.HermesExtensionSettings`; they do not call backend
storage routes and do not write WebUI settings.
