# Archived-session squash UI evidence (#6704)

This evidence addresses the desktop/mobile squash controls added by #6704. It
was captured with real headless Chromium against the production WebUI server,
using disposable state and no provider credentials. The browser created a real
WebUI session through `/api/session/new`, archived it through
`/api/session/archive`, and exercised the real squash preview and confirmation
flow.

- Before source: `c296673ebfaf98750fe38438bc71f0cbb1f75777` (PR merge base)
- After source: `aef874a971dca350e27b0f33c7742da131ec4fbb` (reviewed product head)
- Browser: Chromium `136.0.7103.25`
- Viewports: desktop `1440x900`, narrow `768x900`, mobile `390x844`
- State: isolated temporary WebUI/Hermes homes; no live agent, provider, or user data

| Viewport | Before | After |
| --- | --- | --- |
| Desktop 1440x900 | [No squash action](before-desktop.png) | [Footer squash action with tooltip](after-desktop.png) |
| Narrow 768x900 | [Compact menu without squash](before-narrow.png) | [Compact menu with full-width squash action](after-narrow.png) |
| Mobile 390x844 | [Phone menu without squash](before-mobile.png) | [Phone menu with 49px squash action](after-mobile.png) |

The after run also clicked the visible action at every viewport and verified the
real `Squash conversation` confirmation dialog opened with Cancel initially
focused. The narrow and mobile pages had no horizontal overflow; each squash
action stayed inside its viewport, and the compact action was at least 44px
high. At desktop, the action measured `34x34` and stayed inside the viewport.
The document's pre-existing 1454px scroll width was identical before and after
at the 1440px viewport, so it is not attributed to this control.

Machine-readable observations are in
[`results-before.json`](results-before.json) and
[`results-after.json`](results-after.json). They record source SHAs, browser,
viewport dimensions, DOM counts, target bounds, dialog results, and browser
errors. Both runs passed with no console or page errors.

Reproduce the after check (screenshots are optional):

```bash
python tests/browser_session_squash_visual.py \
  --expect after \
  --artifact-dir /path/to/output
```

To reproduce the before side, check out the merge base separately and pass it
with `--server-root ... --expect before`. This is browser viewport evidence, not
physical-device acceptance.
