# Media serving: `/api/media` and the generated-image mode

The chat renderer serves local artifacts (images, audio, video, downloads)
through a single route, `GET /api/media?path=<absolute path>`. This document
describes the ordinary mode and the `generated=1` mode added for model-emitted
generated images.

## Ordinary mode (default)

`GET /api/media?path=<absolute path>` serves a local file when **all** of the
following hold:

1. The path is absolute and normalizes to a real file.
2. The resolved path is inside an allowed root:
   - the active profile's `HERMES_HOME`,
   - the process-wide `HERMES_HOME`,
   - the base `~/.hermes`,
   - the OS temp directory,
   - the active workspace,
   - any root listed in `MEDIA_ALLOWED_ROOTS`.
3. The basename is not on the deny list (session state files and similar).

A transcript token (`?token=`) grants access to that session's own media only;
it never widens the allowed roots.

Failure modes: `403` for a path outside the allowed roots or on the deny list,
`404` for a path that does not exist or is not a regular file.

## Generated-image mode (`?generated=1`)

Model-emitted images are not ordinary local artifacts. Hermes image generation
writes into the owning profile's image cache (`<home>/cache/images/`) and
returns a bare filesystem path, which the renderer turns into a markdown image
(`![alt](/home/user/.hermes/cache/images/mai_20260918_101400_2248f18b.png)`).

The renderer routes such a bare path through `/api/media` with `generated=1`.
**Generated mode is ordinary mode plus one extra requirement** — it is never a
way around the ordinary authorization.

Extra requirement:

> The requested path, resolved through symlinks, must be canonically contained
> inside one of the owning image-cache roots.

The cache roots are the `cache/images` subtree of each home this process
serves: the active (owning) profile home, the process-wide `HERMES_HOME`, and
the base `~/.hermes`. Containment is checked on the **resolved** path, so a
symlink inside the cache pointing at `HERMES_HOME/private.png` does not
satisfy it.

Additionally, generated mode serves **images only**: the request's file
extension must be one of `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`,
`.ico`, `.avif`. This mirrors the client-side `_IMAGE_EXTS` set in
`static/ui.js`, but the server enforces it — the client check is advisory.

### When it returns 403

| Situation | Result |
|---|---|
| Path resolves outside every allowed root | 403 |
| Path resolves outside every image-cache root | 403 |
| Cache root is a symlink to a directory outside the allowed roots | 403 |
| File extension is not an image | 403 |
| Basename is on the deny list | 403 |
| Path does not exist / is not a regular file | 404 |

A sibling `cache/images` directory outside `HERMES_HOME` is refused, as is a
`cache/images-other` sibling (the containment check is on the canonical path,
not on a string prefix).

### Why the client-side check is not the gate

`static/ui.js` refuses to promote a bare path unless it matches
`/cache/images/` and carries an image extension. That check keeps ordinary
web assets (`/images/logo.png`) inert, but it is **defence in depth only**: the
client cannot know the server's `HERMES_HOME`, and the ordinary `/api/media`
roots are far broader than the image cache (they include the OS temp directory
and the active workspace). Containment is therefore enforced server-side, in
`api/routes.py` (`_generated_image_cache_roots()`,
`_generated_image_cache_root_for()`, and `_handle_media()`).

## Rendering side

`static/ui.js`:

- `_mdLocalImagePathRef()` — the single validator that decides whether a
  root-relative target is an acceptable local artifact. Percent-encoding and
  backslashes are rejected outright rather than normalized, `..` segments are
  refused, and the protocol-relative `//host/...` form is rejected before
  `new URL()` can reinterpret it.
- `_mdImageHtml()` — routes an accepted bare path through `_inlineMediaHtmlForRef()`
  with the generated flag set, so the browser never receives a bare
  `<img src="/abs/path">` it could not load. A refused path returns the
  original markdown source unchanged, so it renders as inert text.
- Both image regexes accept one line break between `]` and `(` (a model that
  wraps the target onto its own line), and the trailing whitespace is matched
  only inside the bare-path alternative so malformed input cannot backtrack
  quadratically.

## Contract routing

This document describes the current contract for `/api/media` and its
generated-image mode. Start here before changing the media allowlist, the
image-cache roots, the generated-mode gates, or the renderer's bare-path
branch.

**State layer.** The media route is stateless: it reads a path from the query
string and resolves it against the filesystem. It never mutates state, and the
resolution of the allowed roots happens per request (no cache), so a symlink
retarget is observed on the next request.

**Owner.** `api/routes.py` owns the authorization decision. `static/ui.js`
owns the rendering decision. Neither may be trusted in place of the other.

## Tests

- `tests/test_issue7634_md_image_split_lines.py` — renderer behaviour of the
  bare-path branch and the inert cases.
- `tests/test_issue8303_md_image_gate_fixes.py` — the client asks for the
  server-enforced mode, and the server enforces containment.
- `tests/test_issue8303_md_image_gate_round2.py` — the second gate: the
  generated mode is plain mode plus the cache-root requirement, refused alt
  text is sanitized, code spans stay literal, stash tokens never leak into
  alt text, and malformed image syntax cannot stall the renderer.
