# Preserve loaded transcript history on updates

Classification: upstream-candidate
Implementation base: 40d99676f2c78f12686e479d5c9c32caa29a9bfa (reviewed fork).
Included upstream base: be5c07175049fc32e093231a8d7fc4b7127c0f0e.
Reconstruction against newer upstream is required before contribution.
Maintenance owner: fork maintainers until upstream adoption.
Upstream status: not-filed.
Private details removed: yes.

## Problem and contract

A paginated conversation implicitly expands to the entire transcript when a full
completion payload arrives, or a same-session force reload falls back to a full
request. With virtualization disabled, this can synchronously mount thousands
of historical messages the reader never requested.

Preserve the existing server-indexed start of loaded history, including every
new message after it. Explicit older-history loading remains unchanged. This is
pagination, not DOM virtualization: projection does not narrow the captured
loaded window, and loaded content remains available to browser Find. No backend
history is changed. Existing canonical bounded-fetch races are not changed.

## Change and boundary

One shared projection helper is used before completion adoption and after a
same-session messages fetch. The existing reload hint carries the boundary over
the asynchronous fetch. Session identity, regeneration revision, nonshrinking
history and matching first-message content at the server offset gate slicing.
Changed or unprovable boundaries retain the original canonical response.
If loaded messages or pagination state change while a reload awaits its response,
the captured boundary is discarded to avoid slicing with stale state. A bounded
canonical refresh response can still re-hide rows loaded while it was in flight;
that pre-existing race is outside this full-snapshot projection fix.

Regeneration-revision changes deliberately fall back to full adoption. Error and
recovery adoption paths are unchanged. The browser refresh fixture returns full
snapshots; partial-response arithmetic is covered by the Node test, not a real
server pagination response. Session metadata (including authoritative todo_state)
is preserved. The deployed backend derives todo_state from the full history
before projection; older backends relying solely on legacy tool-message scanning
can see only the loaded window and need separate compatibility evaluation.

The helper returns a new session object; it does not mutate message objects or
add a persistent cache, setting, timer, dependency or rendering implementation.
It deliberately does not restrict an explicit load-earlier or load-all action.
Already-full transcripts remain full. Transport JSON size is unchanged. Long
individual active turns and animations are separate performance problems.

## Reproduction and verification

Run `tests/browser_completion_loaded_window.py` with the repository browser-test
interpreter. With virtualization explicitly off, seed 50 loaded messages from a
3,300-message transcript, then emit a full canonical completion through the real
stream listener. Before this change, 3,302 messages are mounted and the retained
boundary assertion fails. Afterward 52 messages remain loaded, the canonical
Markdown is visible, busy clears and the older-history affordance survives.
The test invokes real force reload and explicit older loading with synthetic
transport, and simulates completed older loading during an outstanding refresh.
Compact Worklog and Transparent Stream, at desktop/mobile widths, run in
Chromium/WebKit. This fixture disables fades; neighboring completion tests
separately cover fade on/off.

`./scripts/test.sh -q tests/test_completion_loaded_window.py` exercises projection,
identity/revision/boundary/shrink fallbacks, partial payloads and invalid offsets.
The neighboring pagination, cross-session, refresh, carry-forward and fade gates
pass (118 tests); existing 16 completion and 12 reconnect browser cases pass.
No physical iPad battery saving or entire-suite pass is claimed.

## Maintenance and rollback

Product surface: session-window helpers and two adoption call sites in
sessions.js/messages.js. Update hazard: changes to server message offsets,
regeneration revision semantics or same-session reload ownership. Re-run the
browser regression and pagination/isolation tests when rebasing those paths.
Rollback: revert this logical commit; no state migration or config rollback.
Retire when upstream preserves the same loaded boundary through both paths.
