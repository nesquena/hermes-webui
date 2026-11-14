# PR #6810 visual and browser evidence

## Scope and sources

Chromium via Playwright, default dark appearance, English. Three viewports:
1440×900 desktop, 720×900 narrow, and 390×844 phone (`isMobile`, `hasTouch`,
so `pointer:coarse` matches).

Two real WebUI servers were run side by side, each with its own isolated empty
`HERMES_HOME` and `HERMES_WEBUI_STATE_DIR`:

- `before-*`: master `f328d3f8e17f`.
- `after-*`: this PR's head (master merged in).

`/api/crons` reads are browser fixtures with six sample jobs: four active
(`daily-digest`, `weekly-report`, `backup-db`, `inbox-triage`) and two paused
(`paused-only-sync`, `daily-archive`). No real jobs, provider calls or
production state are used. The first-run onboarding overlay that a fresh
isolated state shows was removed from the DOM before capture. The gateway
warning is expected because no Agent gateway is running. On phone and narrow
widths the sidebar drawer was opened with `toggleMobileSidebar()`.

`capture.js` is the exact capture script; `results.json` holds its output.

## Observed results

| Check | 1440 | 720 | 390 touch |
| --- | --- | --- | --- |
| Master has a job filter | no | no | no |
| `daily` shows active + paused match | `daily-digest`, `daily-archive` | same | same |
| Paused section opens for a paused match | yes | yes | yes |
| Clear button hit area | 24×24 | 24×24 | 44×44 |
| Clear glyph | 14×14 | 14×14 | 14×14 |
| Clear restores the full list and focuses the input | yes | yes | yes |
| `/api/crons` reads caused by typing/clearing | 0 | 0 | 0 |
| `cron-paused-collapsed` written by the query | no | no | no |
| Uncaught page errors | 0 | 0 | 0 |

At 1440 only: `paused-only` shows `paused-only-sync` with the paused section
open, `zzz` shows "No jobs match your search.", and the search box is hidden
when the Tasks panel switches to the Scripts subtab.

## Images

| State | Desktop 1440 | Narrow 720 | Phone 390 |
| --- | --- | --- | --- |
| Master | [list](before-1440.png) | [list](before-720.png) | [drawer](before-390.png) |
| PR, no query | [list](after-1440.png) | [list](after-720.png) | [drawer](after-390.png) |
| PR, query `daily` | [mixed match](after-1440-query-mixed.png) | [mixed match](after-720-query-mixed.png) | [mixed match](after-390-query-mixed.png) |

Desktop only: [paused-only match](after-1440-query-paused-only.png),
[no match](after-1440-no-match.png).

## Limitations

Default dark appearance only; Chromium only; phone checked by device emulation,
not on real iOS/Android hardware. The cross-profile ("Show N from other
profiles") view was not exercised with live data. These screenshots are
contributor evidence, not maintainer acceptance.

## Reproduce

Start master on port 8790 and this branch on port 8789, each with an isolated
`HERMES_HOME` / `HERMES_WEBUI_STATE_DIR`, then run `capture.js` as a Playwright
`async (page) => {...}` function. Set `globalThis.PR_MEDIA_OUT` to an absolute
output directory if the Playwright process does not run from the repository
root.
