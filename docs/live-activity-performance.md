# Live activity rendering

Tool rows in long active turns are reused when their inputs haven't changed. This avoids rebuilding completed tool DOM on each prose update in Compact and Transparent Stream modes. Changed inputs still use the existing card builder; prose and reasoning retain their existing render paths.

The reuse signature is stored on the DOM node, not in a global session cache. HTML-restored nodes therefore rebuild once, and removing a turn releases its cached signatures. Compact tool groups retain their disclosure wrapper when the card nodes are unchanged. Transparent ordering ignores hidden legacy prose anchors, which carry stream metadata but aren't visible activity.

The running-dot pulse lives on a `::after` ring animated with `transform:scale` and `opacity` — both compositor-only, so no scene repaint per frame. The dot core itself stays solid for full-opacity contrast in every theme (an opacity-only pulse on the dot dropped to ~1.6:1 in light theme), and reduced motion disables the ring. Lifecycle/state semantics are unchanged; the pulse and progress trajectory intentionally change visible motion and still require normal UX review.

Timestamp visibility is reconciled in place on transparent tool cache hits rather than rebuilding the card. The timestamp picker also synchronizes mounted live and historical rows immediately, preserving detail DOM and text selection without waiting for SSE. Unchanged visibility performs no timestamp DOM writes; language and simplified-tool preferences remain content-signature inputs.

The conversation lifecycle workflow runs the Chromium energy, progress and running-ring inset oracles as informational rows, like the existing normal/error settlement and historical hydration rows; only the reconnect scene redraw and #7676 settle visibility rows fail CI. Repository branch-protection requirements remain administrator-owned.

## Checks

Use the repository test interpreter:

```sh
./scripts/test.sh tests/test_running_dot_energy.py -q
.venv/bin/python tests/browser_live_scene_energy.py
.venv/bin/python tests/browser_reconnect_scene_redraw.py
```

The energy browser test uses isolated state and synthetic SSE, with Chromium and WebKit. It checks zero tool builds on prose updates, retained selection/details, later completion, input corrections, ordering/removal, mode switching, and reduced motion. Reconnect coverage exercises session switching, restored rows, and subsequent completion across activity modes and mobile/desktop viewport sizes.

These tests establish rendering behavior, not physical-device battery savings. Remaining costs include scene projection/signature scans, changed prose/reasoning rendering, and summary/control synchronization. They do not establish a memory leak or eliminate every long-turn cost.
