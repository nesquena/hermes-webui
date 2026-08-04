# Message Renderer Capability (`hermes-webui-message-renderer`)

This document describes the **message renderer host seam** — a generic extension
point that lets a trusted local extension intercept the live-stream assistant turn
element and attach a custom renderer alongside (or on top of) the default markdown
rendering.

---

## Capability key

```
hermes-webui-message-renderer
```

Check for it before registering:

```javascript
const caps = window.hermesExt && window.hermesExt.capabilities;
if (!caps || !caps['hermes-webui-message-renderer']) {
  console.warn('Host does not support the message-renderer capability');
  return;
}
```

Or query via the `HermesExtensionSettings` API:

```javascript
const caps = window.HermesExtensionSettings.getCapabilities();
if (!caps['hermes-webui-message-renderer']) return;
```

---

## Registration API

```javascript
window.registerHermesRenderer(descriptor)
```

**Returns** `true` on success, `false` if the descriptor is rejected.

Call this once from your extension script after the page has loaded (or inside
a `DOMContentLoaded` listener).  The renderer is active for all subsequent live
streams in the page lifetime.

### Descriptor shape

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | `string` | ✓ | Non-empty slug identifying your renderer, e.g. `'my-ext-renderer'`. |
| `mount(root, source, ctx)` | `function` | ✓ | Called once per live stream immediately after the SSE source is registered. See [mount contract](#mount-contract). |
| `unmount(root)` | `function` | ✓ | Called on every terminal stream path (done / error / cancel). See [unmount contract](#unmount-contract). |
| `canActivate(caps)` | `function` | — | Optional capability gate. Receives the frozen `HERMES_HOST_CAPABILITIES` object. Return `false` to abort registration. |

Only **one** renderer can be active at a time.  Calling `registerHermesRenderer`
a second time replaces the previous registration (idempotent if the same `id` is
used, or silently replaces on a different `id`).

---

## Mount contract

```javascript
mount(root, source, ctx)
```

| Parameter | Type | Description |
|---|---|---|
| `root` | `HTMLElement` | The live assistant turn container element.  When the `liveAssistantTurn` DOM node already exists (reconnect / midstream) it is passed directly; otherwise the `messages` pane element is used as a stable fallback.  The renderer **owns only this element** — see [security model](#security-model). |
| `source` | `EventSource` | The SSE connection for this stream.  Your renderer may add its own `addEventListener` calls on `source` to observe token/tool events.  Do **not** close `source`. |
| `ctx` | `object` | Contextual metadata: `{ sessionId: string, streamId: string }`. |

`mount` is called once per `_wireSSE` invocation, which includes the initial
connection and any reconnect for the same stream.  On reconnect the same `root`
element is passed, so the renderer must be idempotent (guard against re-mounting).

`mount` errors are caught by the host and logged; they never crash the core stream.

---

## Unmount contract

```javascript
unmount(root)
```

| Parameter | Type | Description |
|---|---|---|
| `root` | `HTMLElement` | The same element passed to `mount` for this stream. |

Called on every terminal path:

- `done` event — normal stream completion
- `apperror` event — application-level server error (rate limit, crash, etc.)
- `cancel` event — user cancelled the running task

The renderer **MUST** clean up all event listeners, timers, and mutation observers
it registered on `root` or its descendants.  Failure to do so causes memory leaks
and may affect subsequent streams.

`unmount` errors are caught by the host and logged.

---

## Security model

The renderer owns **only the `root` element it receives**.

- Do **not** query or mutate elements outside `root`
  (`document.body`, siblings, the messages pane wrapper, etc.).
- Do **not** reach into `window.S` (the core session state object) or call
  internal helpers like `renderMessages`.
- Core SSE/state machinery (`INFLIGHT`, `S.messages`, `renderMessages`) is
  unchanged; your renderer is a purely additive visual layer inside its `root`.

This boundary is enforced by convention (no JS sandbox), so extensions are
trusted local scripts only — the same trust model as `registerHermesSkin`.

---

## Disabling the renderer at runtime

```javascript
// Suppress future mounts without un-registering the renderer.
window.HermesMessageRenderer.setDisabled(true);

// Re-enable.
window.HermesMessageRenderer.setDisabled(false);

// Check state.
window.HermesMessageRenderer.isDisabled(); // → boolean

// Get the id of the active renderer (or null).
window.HermesMessageRenderer.activeId();   // → string | null
```

To disable before any stream starts, call `setDisabled(true)` from a script that
runs before (or immediately after) `boot.js` is evaluated.

---

## Minimal example

```javascript
// my-extension.js — loaded as a trusted extension via docs/EXTENSIONS.md
window.registerHermesRenderer({
  id: 'my-ext-renderer',

  canActivate(caps) {
    return !!caps['hermes-webui-message-renderer'];
  },

  mount(root, source, ctx) {
    console.log('[my-renderer] mount', ctx.sessionId, ctx.streamId);
    // Listen for custom SSE events alongside the core handlers.
    source.addEventListener('token', e => {
      const { text } = JSON.parse(e.data);
      // Append text to a custom overlay inside root …
    });
  },

  unmount(root) {
    console.log('[my-renderer] unmount');
    // Remove custom overlay, cancel timers, etc.
  },
});
```

---

## Relationship to other extension APIs

| API | Purpose |
|---|---|
| `window.registerHermesRenderer(descriptor)` | Attach a custom live-stream renderer |
| `window.registerHermesSkin(descriptor)` | Contribute a custom color skin to the native picker |
| `window.hermesExt.settings.forExtension(id)` | Persistent settings storage for an extension |
| `window.hermesExt.capabilities` | Frozen capabilities manifest (read-only) |

---

## See also

- [`docs/EXTENSIONS.md`](../EXTENSIONS.md) — how to load extension scripts
- [`static/boot.js`](../../static/boot.js) — `registerHermesRenderer` implementation
- [`static/extension_settings.js`](../../static/extension_settings.js) — capabilities manifest
- [`static/messages.js`](../../static/messages.js) — `_wireSSE` host seam
