# #7685 — Scripts subtab: responsive evidence

Before/after evidence for the Tasks panel's **Scripts** subtab, captured at
three widths. The "before" side is `nesquena/hermes-webui` master
(`72ac887e3`); the "after" side is this PR's head.

| Width | Viewport | Files |
| --- | --- | --- |
| desktop | 1440 × 900 | `before-desktop.png`, `after-desktop.png` |
| narrow | 900 × 820 | `before-narrow.png`, `after-narrow.png` |
| mobile | 390 × 844 | `before-mobile.png`, `after-mobile.png` |

## How these were captured

Both sides were produced by the same procedure against a real `server.py`
(agent-free, isolated `HERMES_HOME` and state dir), so the pair differs only
by the code under review:

1. Plant three scripts in the active profile's `scripts/` directory
   (`backup.sh`, `cleanup.sh`, `report.py`), each with a leading comment or
   docstring so the panel has a description to show.
2. Boot the server and wait for `/health`.
3. Assert the difference is real at the API level before shooting: on the
   **before** tree `GET /api/scripts/list` must answer `404` (the endpoint is
   what this PR adds), and on the **after** tree it must answer `200` and
   list the three planted scripts.
4. Open the Tasks panel, then the Scripts subtab (the after side only), and
   assert the panel really renders three script rows.
5. Capture the panel's visible rect through a viewport clip, so both sides
   frame the same region — an element screenshot scrolls a panel taller than
   the viewport and would make the two sides incomparable.

No console errors and no uncaught page exceptions were recorded in any of the
six runs.

## What the pair shows

- **Before**: the panel is `SCHEDULED JOBS` only. There is no subtab strip
  and no script listing at any width — the three planted scripts are
  invisible to the user.
- **After**: a `Jobs | Scripts` strip sits above the list, `Scripts` is
  selected, and each script row shows its name, its size, and the
  description taken from its own leading text.

## Responsive notes

- The subtab strip uses the panel pill idiom with a 44 px touch target, so it
  stays tappable on mobile.
- At 390 px the panel lives in the slide-in sidebar drawer; the shot is the
  drawer with the Scripts subtab active, which is where a mobile user lands
  after tapping the Tasks icon.
- No text is clipped and nothing overflows at any of the three widths; the
  description wraps to two lines and the file-size badge stays on one line.
- The panel header shows the resolved scripts directory, truncated with an
  ellipsis at narrow and mobile widths — the full path is in the endpoint
  payload.
