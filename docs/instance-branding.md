# Shared installation identity (API v1)

The server can persist an installation name and one image for every device.
Custom Branding owns the editor and logo placement; core owns validation,
storage, the initial HTML metadata, public icon derivatives and PWA manifest.
This complements the optional `instance_name` setting from
[#8307](https://github.com/nesquena/hermes-webui/pull/8307). It never renames
the assistant (`bot_name`) or profiles. The original image branding proposals
[#2208](https://github.com/nesquena/hermes-webui/pull/2208) and
[#3307](https://github.com/nesquena/hermes-webui/pull/3307), and gavinssr's
Custom Branding extension, informed this integration.

This API is introduced by this change. Earlier releases, including
`exp-v0.52.653`, do not provide it. Detect `api_version: 1` at runtime rather
than inferring support from an older release number. The editor remains in
the separately distributed extension; core does not add a settings panel.

## Provider activation and compatibility

An enabled `custom-branding` entry in the server's effective extension
inventory must explicitly advertise `"instance_branding_api": 1` in its
runtime `manifest.json` entry. The same field works in a gallery-installed
top-level manifest or a manual `extensions` entry. It is a numeric version,
not a boolean, and is not an extension permission or browser registration.
For example, a minimal isolated provider fixture is:

```json
{
  "extensions": [{
    "id": "custom-branding",
    "scripts": ["branding.js"],
    "instance_branding_api": 1
  }]
}
```

Only advertise this version in an extension that respects the public
`window.__HERMES_INSTANCE_BRANDING__` projection. While `effective.active`
is true, its shared icons take priority over personal preferences. The
extension must suppress its local favicon/Apple swaps and use shared images
for its DOM logos. Preserve old personal storage keys without uploading them
automatically. Importing a personal image is an explicit public upload.

Custom Branding 0.1.0 does not advertise this protocol and cannot activate
shared branding. Absent, disabled, uninstalled, downgraded or incompatible
providers make the shared projection inactive immediately on the next server
request. Configuration and files remain available for reactivation. This
handshake prevents an old extension's local icon swaps from competing with
an active shared identity; it does not sandbox extension code. Extensions
retain the native WebUI trust model and installation/toggle permissions.

## Optional image dependency

Image import uses **Pillow 13.0.0**, an optional dependency compatible with
Python 3.11–3.13. Install into the interpreter environment that runs WebUI:

```sh
python -m pip install '.[branding]'
# For a source checkout without installing the WebUI package:
python -m pip install 'Pillow==13.0.0'
```

The server does not install it automatically. Without Pillow it still starts,
reads settings, renders metadata/manifests, serves previously generated icons
and permits name/activation changes. `can_upload` is false and image imports
return 503. Roll back by disabling shared branding, retaining the data, then
removing this optional dependency from the WebUI environment. Do not remove
Pillow if another installed feature depends on it.

Upstream references: [Python support](https://pillow.readthedocs.io/en/stable/installation/python-support.html),
[13.0.0 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html),
[security advisories](https://github.com/python-pillow/Pillow/security/advisories),
and [Image decoding](https://pillow.readthedocs.io/en/stable/reference/Image.html).
Keep the optional pin current when upstream publishes security fixes.

## Management API

All management responses use `Cache-Control: no-store` and a strong quoted
ETag. Resolve paths relative to the app's `document.baseURI`, including when
opening the editor from `/session/<id>` under a proxy prefix:

```js
const endpoint = new URL('api/instance-branding', document.baseURI);
const response = await fetch(endpoint, {cache: 'no-store'});
const state = await response.json();
const etag = response.headers.get('ETag');
```

| Method and path | Request |
| --- | --- |
| `GET api/instance-branding` | Capabilities, configuration, effective identity, ETag |
| `POST api/instance-branding` | JSON patch, at most 4096 bytes, `Content-Type: application/json` |
| `POST api/instance-branding/image` | Binary static PNG, at most 2 MiB, `Content-Type: image/png` |

Both POST routes require the exact `If-Match` from the preceding GET. They
accept one decimal `Content-Length` and reject transfer encoding. Rejected
unread bodies close the connection. Apply native CSRF headers for browser
writes. Do not retry a mutation blindly: reload on 412 and show the current
state. Successful mutations return the canonical state and a new ETag.

JSON accepts only `enabled` (boolean), `instance_name` (string, trimmed,
at most 80 characters, strictly UTF-8 encodable, no C0/DEL controls), `manages_instance_name` (boolean),
or the standalone `{"action":"reset"}`. Uploading does not enable branding
or change the name. No client-supplied revision, filesystem path, owner id,
URL or data URL is accepted. An unknown persisted schema falls back and
requires an explicit reset before management can resume.

The response shape is:

```json
{
  "api_version": 1,
  "name_supported": true,
  "can_manage": true,
  "can_upload": true,
  "provider_available": true,
  "config": {
    "enabled": true,
    "instance_name": "Team Console",
    "manages_instance_name": true,
    "revision": "opaque-configuration-token",
    "image_revision": "64-lowercase-hex-image-token"
  },
  "effective": {
    "active": true,
    "name": "Team Console",
    "revision": "opaque-effective-token",
    "favicon_32": "branding/64-lowercase-hex-image-token/favicon-32.png",
    "icon_192": "branding/64-lowercase-hex-image-token/icon-192.png",
    "icon_512": "branding/64-lowercase-hex-image-token/icon-512.png",
    "apple_touch_icon": "branding/64-lowercase-hex-image-token/apple-touch-icon.png",
    "favicon_ico": "branding/64-lowercase-hex-image-token/favicon.ico"
  }
}
```

Inactive assets and absent names are `null`. `config.revision` identifies a
branding write; `effective.revision` and the HTTP ETag also include raw name,
effective provider state and image completeness. They can change after native
name edits or provider changes even without a branding write. The initial
HTML contains only `api_version` and `effective`, never management configuration,
authority, settings or private filesystem paths.

Status codes: 400 malformed JSON/framing/fields, 401 native authentication
required, 403 owner authority or CSRF denied, 409 incompatible/unavailable
provider or incomplete image, 412 stale ETag, 413 oversized body, 415 wrong
media type, 422 invalid PNG, 428 missing ETag, 503 missing normalizer, 500
persistence failure. Reset and `{"enabled":false}` remain available if the
provider is missing. A future/unknown schema permits only reset.

## Authority and canonical name

There is no new administrator role. Management requires enabled WebUI
authentication and a valid native session without `bound_profile`. Native
password/passkey sessions and accepted unrestricted OIDC/trusted-proxy
sessions follow WebUI's existing owner trust model. Unknown auth types,
expired/revoked sessions and profile-bound identities cannot manage branding.
Trusted-auth identity is reconciled before authorization; an old signed
proxy cookie does not substitute for a current trusted upstream identity.

With authentication disabled, management is refused even on loopback;
capability reads follow the native API policy and report `can_manage:false`.
This does not create a multi-tenant RBAC boundary for other WebUI APIs.

`instance_name` remains the only canonical raw name. `manages_instance_name`
explicitly adopts that name into branding: inactivity hides it in effective
HTML/manifest/settings responses, while retaining the raw value on disk.
Without adoption, an independent native instance label survives image disable
and reset. Reset clears the raw name only when branding owns it. Neither
operation alters `bot_name`, profile names or a stored icon tint.

The generic settings API rejects the reserved `instance_branding` object and
cannot change a branding-managed name. Use the dedicated API for that name.
Unrelated settings saves omit `instance_name` and update the browser mirror
from the server response, so an old tab cannot silently overwrite a new label.

## Storage, image pipeline and public resources

The existing `SETTINGS_FILE` (including its explicit environment override)
owns `instance_name` and the reserved `instance_branding` object. The latter
contains schema version 1, enabled flag, opaque revision, fixed owner
`custom-branding`, name adoption flag, current `image_revision` and internal
`previous_image_revision` for retention. All native and branding saves share
one lock around the complete read/modify/atomic-write operation and preserve
the existing settings file's permissions. This is process-local serialization;
external settings editors do not participate in the lock.

Icons live under `STATE_DIR/branding/<image_revision>/`, outside the checkout
and profile directories. Import accepts only static square PNGs, 512–2048 px,
at most 4,194,304 pixels. The pipeline validates PNG structure, chunk order,
CRC and exact IEND, rejects animations and unknown critical chunks, strips
ancillary metadata before decoding, decodes all pixels with Pillow, then
re-encodes new pixels. It does not preserve the input filename or original bytes.

Pipeline v1 uses RGBA pixels, Lanczos resampling, PNG 32/192/512,
an opaque 512 px Apple icon on `#0d0d1a`, and ICO sizes 16/32/48. A normalized
master is private. The image token hashes the normalized master and pipeline
version. Derivatives are fully written and fsynced in a private staging
directory, then published before the single settings commit. A failed settings
write leaves at most an unreferenced image and preserves the former identity.
Maintenance retains the current and previous image reference across name edits;
reset removes references and makes those files collectable.

Only the five fixed derivative names are public, at
`branding/<64-lowercase-hex>/<fixed-name>` and the speculative
`session/branding/...` aliases. No directory listing, master, settings or arbitrary
file route exists. Successful responses have a fixed image MIME type,
`nosniff`, content ETag and `public, max-age=31536000, immutable`. Invalid/missing
assets return 404 after the native auth gate; invalid paths are not auth-exempt.
POSIX reads/publication use anchored descriptors and no-follow opens. Platforms
without `openat` retain the existing workspace API's pathname fallback and
reject observed symlinks; that fallback does not provide the same race protection.
Same-user code capable of modifying server state is outside this trust boundary.

## PWA, prefix, cache and lifecycle

Branding is applied per request after the shell template cache and before
extension injection. HTML text/attributes and the public JS payload are escaped.
The SVG favicon link is removed only while a valid shared image is active;
otherwise the native chain returns. Login icon/title metadata follow the same
projection while its authentication text and behavior remain native.

The dynamic manifest retains its linked URL, **raw `id`, `scope`, `start_url`
and shortcut URLs**. Names change together; icons and shortcut icons use the
shared PNGs with `purpose: any`, never an automatic maskable claim. Root
manifests use `branding/...`, session aliases use `../branding/...`. Proxies
must strip their configured prefix as for other WebUI routes. No untrusted
forwarded prefix is interpreted. Existing differences in PWA identity resolution
between root/session/prefix routes are deliberately preserved.

If integrating [#6266](https://github.com/nesquena/hermes-webui/pull/6266),
compute its native tinted manifest/HTML fallback first, then apply the shared
projection last. Its client tint helper must skip links carrying
`data-instance-branding="1"`. This branch does not import that PR; server
projection composition is tested, and no claim of a merged client integration
is made. Disabling shared images restores the caller's native/tinted fallback.

Notifications use the shared 192 px image; the native monochrome badge and
notification title rules remain unchanged. The service worker bypasses mutable
manifests and versioned branding resources; it does not precache the manifest.
API/auth caching is unchanged. An offline navigation may retain an older shell.

After a successful editor write, reload the page. Existing tabs compare the
effective revision on `pageshow`/return to visibility and reload if it changed;
there is no background polling. Fresh/reloaded clients receive one server
identity without localStorage migration.

Browser/OS policy controls refresh of an already-installed PWA icon or name.
Versioned resources do not force an immediate launcher update. Distinguish
new installation, updated web content and an existing Android/iOS launcher;
some users may need to reinstall a shortcut. Automated HTTP/JS tests prove
responses and payloads, not rendering on physical devices.

## Verification

Run with disposable `HERMES_HOME`, `HERMES_WEBUI_STATE_DIR` and
`HERMES_WEBUI_TEST_STATE_DIR`, using the repository runner:

```sh
./scripts/test.sh tests/test_instance_branding.py \
  tests/test_issue7611_instance_name_title.py tests/test_pwa_manifest_sw.py \
  tests/test_pwa_manifest_csp.py tests/test_service_worker_api_cache.py \
  tests/test_atomic_settings_writes.py -q
```

Install the optional image extra into the test environment to run image cases;
those cases skip when Pillow is absent. The HTTP tests use the real WebUI
handler and a compatible provider fixture, with mount stripping modeled by a
handler adapter. They do not require an Agent runtime or contact a real IdP.
Independent validation should also cover two clean browser contexts, a real
proxy and OIDC provider, visible page transitions, and new/existing Android/iOS
PWA installations. Never use a running installation as test state.
