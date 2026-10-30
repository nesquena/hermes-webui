# Layout-free running progress animation

Classification: upstream-candidate
Implementation base: 40d99676f2c78f12686e479d5c9c32caa29a9bfa.
Included upstream base: be5c07175049fc32e093231a8d7fc4b7127c0f0e.
Maintenance owner: fork maintainers until upstream adoption.
Upstream status: not-filed. Private details removed: yes.

## Evidence and change

Transparent Stream's running progress segment animates left/right offsets,
invalidating layout every frame even with no incoming tokens. Replace offsets
with transform translation across the existing clipped track. Preserve the
60%-width running segment, 1.6-second timing and settled 100% width. The segment
now travels from outside the left edge to outside the right edge rather than
starting at the left edge; this is an indeterminate status indicator, not a
measurement of actual tool completion. Respect reduced motion with a static bar.

Run `tests/browser_progress_animation.py` with the repository browser-test
interpreter. The baseline fails on layout-affecting keyframes. Chromium's
candidate has zero layouts in a one-second isolated animation sample;
Chromium/WebKit verify running width, reduced motion, and terminal width/transform.
41 neighboring running-indicator and Transparent Stream tests pass.

A visible Chromium fixture with 470 completed tools and a running tool measured
0.078 task seconds / 8 layouts over four seconds with the candidate, 0.788 seconds
/ 244 layouts when the old keyframes were restored, and 0.053 seconds / 8 layouts
when the candidate was restored. No physical iPad battery percentage is claimed.

## Maintenance

One stylesheet rule/keyframe group and one real-browser regression; no new
JavaScript, settings, dependencies, timer, cache or persistent state. Keep the
running-width assumption synchronized with `_syncTransparentEventControls` if
upstream changes it: the +200% endpoint needs a segment at least half the track
width to exit fully. The changed trajectory/speed is intentional; the 1.6-second
cycle remains unchanged. The browser gate is an explicit manual release check,
not collected by pytest; install both Chromium and WebKit before running it.
Completion/failure/interruption remove the running attribute
through existing code. Rollback is reverting this logical commit; no migration.
Reconstruct on current upstream and rerun before opening a contribution. Retire
when upstream uses layout-free running progress with equivalent state behavior.
