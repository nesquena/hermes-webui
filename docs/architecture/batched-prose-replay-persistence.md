# Batched live prose and replay persistence

The live stream's transport cursor advances when a journal event is received.
Assistant prose is consumed incrementally on receipt, but its publication into
`INFLIGHT.messages` and `lastAssistantText` is normally batched for 32 ms. The
independent persistence timer can fire during that interval.

`persistInflightState()` must drain pending semantic publication before building
its snapshot. Otherwise it can save a cursor that excludes journal bytes absent
from the saved assistant prefix. Reattachment then requests only the suffix.

The persistence boundary reuses `_drainSemanticProse('semantic')`:

- publish pending incremental state before copying primitive body/cursor fields;
- retain partial delimiter lookahead rather than imposing terminal semantics;
- leave actual painting on the existing scheduler;
- keep token receipt batched (no per-token persistence or forced paint);
- reuse existing stream-generation/disposal guards and terminal ownership.

A separate recovery watermark was not introduced: it would require coordinating
body, reasoning, tool, terminal and cleanup progress despite an existing drain
that can synchronize the snapshot at the persistence boundary. No accepted
upstream primitive replaces that stream-owned drain on this integration base.

Regression coverage in `tests/test_issue7478_replay_coherence.py` executes the
real frontend handlers and localStorage persistence/recovery. It schedules token
206 immediately before the persistence deadline, discards live projection,
reattaches from the stored snapshot, and appends suffix 207. It checks exact body
reconstruction for user-only starting state, historical assistant rows, partial
current-turn publication, fully published prose, reasoning and tool activity,
with transcript virtualization configured off and on.

This repairs the batching-induced snapshot race. It does not redefine the generic
eligibility predicate for arbitrary pre-existing/corrupted recovery entries,
change browser storage truncation policy, repair hidden preserved-node session
reconciliation, or alter backend run-journal/sidecar behavior. A historical
assistant row is not used as evidence for the new snapshot: the active stream
publishes its current accumulator before that snapshot is constructed.
