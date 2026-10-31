# Synthetic renderer evidence for #8427

These are synthetic, isolated Chromium fixture captures, **not** private chat transcripts or live-provider output. The fixture loads the production renderer and CSS, disables `fetch`, uses a minimal transcript shell, and does not load `boot.js` or a running WebUI service.

| Capture | Before | After |
|---|---|---|
| Desktop | [before-desktop.png](before-desktop.png) | [after-desktop.png](after-desktop.png) |
| Narrow | [before-narrow.png](before-narrow.png) | [after-narrow.png](after-narrow.png) |

- Engine: Chromium 155.0.8059.79, fresh disposable profile per run.
- Baseline: `01af02003fba4e495b3015d3804de71aa7f9dbdb`; the relevant source blobs were also identical in checked `master` `f03b203ea4a7ac345b7ed1f745600535f619a256`.
- Screenshots use Compact Worklog with Thinking enabled. The full matrix also exercises Transparent Stream and Final answer only with Thinking on/off.
- Fixture-reported CSS viewports at assertion time: desktop 1280×857; narrow 500×701, DPR 1. Screenshot window/bitmap sizes are 1280×1000 and 500×844. An earlier request for 390 pixels was clamped by Chromium to a 500-pixel CSS viewport; these public captures request 500 explicitly rather than label the result as a 390-pixel test.
- The fixture opens and closes the real disclosure summaries through DOM `click()` in Chromium, checking final-answer layout/ownership at initial, expanded, collapsed, and re-render stages. This is page behavior, not a claim about OS-level pointer delivery.

## Result and important limit

The unchanged baseline fails the **cross-user tool ownership** assertion. Its existing turn-final guard keeps the final-answer paragraphs visible in this synthetic scene; the before image must **not** be described as reproducing disappearing answers or persisted-data loss.

The one-condition guard makes all 14 helper controls and six renderer mode/Thinking combinations pass in Node. The real-browser matrix has 12 fixed passes and 12 expected baseline ownership failures across the two widths. Fixed cases preserve the three ordinary final-answer paragraphs outside collapsed activity through the full disclosure/re-render sequence. Direct reasoning-owner variants are covered by helper tests; the browser transcript itself does not contain a reasoning payload.

No API/session loading, live SSE/reconnect, stored preferences, real Agent execution, paid provider requests, or database migration is certified by these fixtures. Temporary browser state is discarded.

## Reproduce

Follow `TESTING.md` → “User-bounded legacy tool anchors (#8427)”. Use a disposable checkout/profile and the portable `tests/fixtures/issue8427_tool_anchor.html` page, keeping the actual `static/` assets in their relative locations. Repeat the six mode/Thinking combinations at desktop and narrow widths. The header and `data-result` expose the assertion result; `data-viewport-width`, `data-viewport-height`, and `data-device-pixel-ratio` expose the measured browser dimensions.
