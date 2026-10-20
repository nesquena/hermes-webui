# Sidebar idle / terminal handoff evidence

## Failure and scope

The independent `/api/sessions` response can report idle before the chat SSE's
terminal frame reaches the page. Three sidebar paths were retiring chat state:
idle reconciliation, INFLIGHT purging, and optimistic-row retirement. The idle
path clears the Worklog and active stream ID; the subsequent `done` is rejected
as stale, so no settled Anchor scene is persisted.

A real-HTTP barrier reproduces this on both published PR head
`75c8ff8d2a991f22389e3e4263ee03fb8d8f67a0` and frozen upstream
`b09fded7b8c98b549a49aaaece3721015337d307`. Applying only the `sessions.js`
product fix to the frozen upstream also passes the identical oracle: this is a
shared lifecycle defect, not evidence that artifact replay introduced it. The
historical hosted run lacks this instrumentation, so its exact interleaving
cannot be proved retrospectively; its empty-surface/no-persistence signature
matches the controlled reproduction.

## Reproduction

```sh
LIFECYCLE_SCENARIO=normal LIFECYCLE_TEST_BITE=settle-worklog-frame-proof \
  .venv/bin/python tests/browser_conversation_lifecycle.py
./scripts/test.sh tests/test_sidebar_terminal_handoff.py -q
```

The existing frame proof now uses a test-only server wrapper to hold the actual
`done` HTTP write. The real worker finishes; the real browser receives an idle
sidebar response through `renderSessionList()` while its native EventSource
remains open. The test verifies pane, INFLIGHT and registry ownership, then
releases `done` and retains all existing painted-frame, final-answer, semantic
activity and hard-reload assertions. The fixture always releases/cleans up and
has a bounded wait. Normal production server startup is unchanged.

No renderer, terminal payload or persistence response is fabricated. Runtime
events come from the existing deterministic localhost Gateway fixture, not a
live model/provider. All state and files are disposable. These are browser
viewport checks, not physical-device acceptance.

## Before / after

Each screenshot is taken after the idle sidebar response, before the terminal
HTTP frame is released. The exact published source fails at both widths. The
patched source preserves three live activity rows and then passes completion
and hard reload. `results.json` records the predicate results without machine
paths or credentials.

| Viewport | Before | After |
| --- | --- | --- |
| 1440 x 900 | [Empty surface](before-1440.png) | [Owned handoff retained](after-1440.png) |
| 390 x 844 | [Empty surface](before-390.png) | [Owned handoff retained](after-390.png) |

The fix only defers these sidebar cleanups for the current pane's exact OPEN
chat transport. Missing, CONNECTING, CLOSED and mismatched sources still take
the existing stale recovery path.

## Missing terminal frame: bounded recovery

The OPEN-only guard at `85f1c2d34b62b0ade85f51ed2cf8c1cf56f5ab9e` could wait
forever when the terminal frame never arrived. The follow-up keeps the live
handoff for 1.5 seconds, then probes the exact stream through
`/api/chat/stream/status` with an eight-second timeout and no retries.
Repeated sidebar hints cannot restart the deadline or create concurrent
requests. `active:true` keeps the OPEN owner intact even when the persisted
session fields already look idle; only explicit runtime inactivity permits the
canonical session restore. A failed or malformed runtime probe uses the
existing interrupted-stream path rather than claiming successful settlement.
Normal terminal delivery, transport replacement/close, navigation, and a newer
optimistic turn invalidate the old request. Already-running stream-end recovery
remains the sole recovery owner.

```sh
LIFECYCLE_SCENARIO=normal LIFECYCLE_TEST_BITE=sidebar-idle-missing-terminal \
  .venv/bin/python tests/browser_conversation_lifecycle.py
./scripts/test.sh tests/test_sidebar_idle_stream_recovery.py -q
```

The new browser row keeps the real terminal HTTP barrier closed until cleanup;
the client must recover without receiving `done` or `stream_end`. Published
`85f1c2d34b` fails the 11-second completion bound with `S.busy` still true.
The patched implementation restores the final answer and passes the unchanged
painted-frame, semantic activity, persistence and hard-reload checks before the
barrier is released. The cloud workflow runs both delayed- and missing-terminal
rows. No live provider or production state is used.

A production Gateway regression also blocks post-turn goal evaluation after
the success transcript has already cleared `active_stream_id` and pending
fields. During that interval the worker and exact entry in `STREAMS` remain
live and no `done` event has been emitted, proving why persisted session flags
cannot authorize sidebar settlement on their own.
