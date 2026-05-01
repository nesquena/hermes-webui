# WebUI Extensions

Hermes WebUI supports a small, opt-in extension surface for self-hosted installs.
It lets an administrator serve local static assets and inject same-origin CSS or
JavaScript into the app shell without editing the WebUI source tree.

This is intentionally not a plugin marketplace or dependency system. It is a
safe escape hatch for local dashboards, internal tooling, and workflow-specific
panels that should not live in core Hermes WebUI.

## What extensions can do

Extensions can:

- serve files from one configured local directory at `/extensions/...`
- inject configured same-origin stylesheets into `<head>`
- inject configured same-origin scripts before `</body>`
- call the normal WebUI APIs available to the browser session

Extensions cannot, by themselves:

- bypass WebUI authentication
- serve files outside the configured extension directory
- load third-party scripts/styles through the built-in injection config
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

## URL rules

Injected asset URLs are deliberately restricted:

- must be same-origin paths
- must start with `/extensions/` or `/static/`
- must not include a URL scheme, host, fragment, quote, angle bracket, newline,
  NUL byte, or backslash

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
