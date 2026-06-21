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
> Do not point `HERMES_WEBUI_EXTENSION_DIR` at a user-writable directory.

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

Extensions are disabled by default. Configure them with environment variables
before starting the WebUI server. `HERMES_WEBUI_EXTENSION_DIR` must point to an
existing directory before any script or stylesheet URLs are injected:

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

The endpoint is read-only and follows the normal WebUI authentication rules. The
same sanitized diagnostics are also shown in **Settings → Extensions** for
operators who prefer to inspect extension state from the browser. The panel does
not enable, disable, install, or mutate extensions; it only fetches
`/api/extensions/status` and offers a copy-diagnostics action.

The diagnostics return the same public asset URLs that can already be injected
into the HTML, plus coarse manifest status, asset counts, sanitized declared
loopback sidecars, and warning codes for rejected or unavailable configuration.
`manifest.script_count` and `manifest.stylesheet_count` count accepted assets
from the manifest only; `manifest.sidecar_count` counts accepted enabled loopback
sidecars from the manifest. `counts.script_urls` and `counts.stylesheet_urls`
count the final post-env-merge URLs, while `counts.sidecars` counts the sanitized
sidecar list returned in `sidecars`. `manifest.entry_count` counts the loaded
top-level manifest object and enabled extension entries that were inspected, not
every extension object in the file. The endpoint and Settings panel do **not**
return `HERMES_WEBUI_EXTENSION_DIR`, resolved manifest paths, raw environment
values, rejected URL strings, rejected sidecar origins, or rejected health paths.

When sanitized loopback sidecars are present, **Settings → Extensions** renders a
read-only sidecar monitor card. The browser checks each declared `health_url`
directly with `fetch(..., { credentials: 'omit', cache: 'no-store' })` and a
short timeout. WebUI does **not** proxy sidecar requests and does not send WebUI
cookies to sidecars. A successful HTTP response is shown as healthy, a non-OK
HTTP response as unhealthy, and CORS/network/timeouts as unreachable or blocked;
health response bodies are never rendered.
