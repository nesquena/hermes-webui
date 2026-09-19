# Transcript Auto-Follow Scroll Contract

- **Status:** Implemented
- **Applies to:** `static/ui.js` scroll-follow state machine, `static/style.css`
  `.messages` scroller, and the queued live-render restore paths.
- **Regression coverage:** `tests/test_fast_stream_shrink_clamp_unpin.py`,
  `tests/test_issue5637_stale_anchor_guard.py`,
  `tests/test_issue3319_pinned_scroll_jump.py`,
  `tests/test_pinned_restore_stale_bottom_gap.py`,
  `tests/test_scroll_collapse_clamp_keeps_follow.py`,
  `tests/test_2111_ios_pwa_bottom_scroll_stutter.py`, and
  `tests/test_mobile_layout.py`.

This document is the authority for how the transcript decides to stay glued to
the live tail, when reader input releases that glue, and how a reader regains
it. The scroll listener, the settle writer, and the queued live-render restore
all consult this same state, so a change here must keep all three consistent.

## State model

Three pieces of state own the outcome, all inside `static/ui.js`:

- `_scrollPinned` — the live-tail follow latch. While true, every streamed
  write re-anchors the viewport to the bottom.
- `_messageUserUnpinned` — the sticky reader-takeover flag. Reader input sets
  it; only a genuine return to the tail (or the bottom jump control) clears it.
- `_messageScrollInputGeneration` — a monotonic ownership token bumped by every
  real reader input (wheel, touch, keyboard, scrollbar drag). Delayed restores
  compare their captured generation against it to distinguish "input happened
  after the snapshot" from "input merely happened recently".

## Input-tail capture and reader-resume re-pinning (catch-tail contract)

During a fast stream (200+ tok/s) the true bottom moves DOWN between the
reader's wheel event and the scroll handler running. Distance measured against
the freshly-grown `scrollHeight` chronically reads far from the tail, so an
unpinned reader chasing the tail could never satisfy a "reach bottom" re-pin
gate.

The contract that closes that race:

1. **Capture at input.** Every real reader input captures the tail height at
   the moment of the event (`_captureMessageScrollInputTail`), BEFORE the
   browser applies that input, and stamps it with the input generation. Event
   values are the only authority for "the tail the reader was aiming at";
   `scrollHeight` observed later by a scroll callback is not, because streaming
   can add arbitrary height between the event and the callback.
2. **Consume once.** Each captured input tail authorizes exactly one scroll
   event (`_messageScrollInputTailConsumedGeneration`). A later programmatic or
   layout scroll cannot reuse stale authority — this is what prevents the
   false re-pins that earlier per-callback-height versions reintroduced.
3. **Arrive → re-pin.** If a downward scroll carries the viewport to within
   80px of the INPUT-CAPTURED tail, that arrival is decisive re-pin intent: the
   reader re-pins immediately (no debounce — at fast stream rates a second
   qualifying event may never come) and the tail snaps to bottom.
4. **Resume auto-follow.** Re-pinning clears `_messageUserUnpinned` and sets
   `_scrollPinned=true`, so the next streamed write follows again. A reader who
   does not reach the captured tail stays unpinned; proximity inside the
   ~250px near-bottom band alone never re-pins (the #4295 invariant).
5. **Reader input wins over queued restores.** A queued live-render restore
   (rAF after an activity-scene rebuild) re-checks the input generation at rAF
   time. If the reader provided input between capture and frame, the restore
   abandons the stale snapshot instead of writing it — real input outranks the
   captured tail position.

The companion recovery rule: while pinned, a rebuild restore targets the
POST-rebuild tail exactly (the pre-rebuild gap is stale once content grew), so
the restore is idempotent with the follow writer instead of racing it across
paint frames.

## Why the transcript scroller suppresses elastic overscroll

`.messages` sets `overscroll-behavior-y: none` rather than `contain`:

- `contain` prevents scroll chaining but deliberately keeps the platform's
  elastic overscroll effect at the boundary.
- While live-tail follow writes the hard bottom on every streamed update, that
  elastic transform is repeatedly cancelled and re-started, which reads as a
  bottom-edge vibration on touch devices during streaming.
- `none` preserves the isolated scroll surface (gestures still do not chain
  into the app body) while also suppressing the boundary effect, so the follow
  writer and the platform rubber-band never fight for the same pixels.

This only applies to the transcript scroller. Other scroll surfaces (session
list, clarify card, kanban columns) keep `contain`/default because nothing
writes to them continuously.

## Manual verification checklist

Desktop, narrow/mobile width, and long streaming content per the UI/UX guide:

1. Start a long streaming response on desktop: the tail stays glued; wheel up
   (any amount, even a small trackpad gesture) releases follow immediately.
2. While unpinned mid-stream, wheel down toward the tail: arriving within a
   small distance of the tail you were aiming at re-pins and follows again.
3. Same on a narrow/mobile viewport with touch: swipe up releases, swipe down
   to the tail re-pins, and there is no bottom-edge vibration while pinned
   during streaming (the overscroll suppression above).
4. During a live stream, open a jump/queue/compression card that shrinks or
   grows the transcript while pinned: no up-then-snap bounce.

Regression suites: run the files listed at the top with `./scripts/test.sh`.
