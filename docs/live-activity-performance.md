# Live activity rendering

Tool rows in long active turns are reused when their inputs haven't changed. This avoids rebuilding completed tool DOM on each prose update in Compact and Transparent Stream modes. Changed inputs still use the existing card builder; prose and reasoning retain their existing render paths.

The reuse signature is stored on the DOM node, not in a global session cache. HTML-restored nodes therefore rebuild once, and removing a turn releases its cached signatures. Compact tool groups retain their disclosure wrapper when the card nodes are unchanged. Transparent ordering ignores hidden legacy prose anchors, which carry stream metadata but aren't visible activity.

The running-dot pulse animates opacity rather than a spreading shadow, and respects reduced motion.

## Checks

Use the repository test interpreter:

```sh
./scripts/test.sh tests/test_running_dot_energy.py -q
.venv/bin/python tests/browser_live_scene_energy.py
.venv/bin/python tests/browser_reconnect_scene_redraw.py
```

The energy browser test uses isolated state and synthetic SSE, with Chromium and WebKit. It checks zero tool builds on prose updates, retained selection/details, later completion, input corrections, ordering/removal, mode switching, and reduced motion. Reconnect coverage exercises session switching, restored rows, and subsequent completion across activity modes and mobile/desktop viewport sizes.

These tests establish rendering behavior, not physical-device battery savings. Remaining costs include scene projection/signature scans, changed prose/reasoning rendering, and summary/control synchronization. They do not establish a memory leak or eliminate every long-turn cost.
