# Incremental live activity projection

The live stream attachment owns one opt-in activity projector in
`static/assistant_turn_anchors.js`. It feeds the existing scene renderer through
its `LIVE_STREAMS` record. The public `projectAssistantTurnAnchorActivityScene`
remains a full, uncached projection for arbitrary mutable inputs, historical
scenes and callers without a current attachment owner.

## State and ownership

The projector retains only the current source references, projected rows, row-ID
index and coalesced dirty indices. Nothing is added to a registry, anchor, event,
scene or snapshot: their own keys, JSON serialization and freeze behavior remain
unchanged. Scene envelopes are recreated from authoritative identity, lifecycle,
content, artifacts and side effects, even when no activity rows changed. Earlier
published row arrays are never mutated by the projector. The opt-in projector's
rows are borrowed read-only by the live renderer: nested payload objects retain
the existing shallow-freeze contract, so consumers must not mutate them. A caller
that needs mutable output must use the public full projector, which always returns
fresh sanitized payloads. Deep-freezing public objects or inserting mutation
proxies would change reflection semantics and is deliberately not done.

Successful live appends and exact-index replacements notify the attachment before
painting. Reasoning-echo removal invalidates the whole projection because it can
change index-derived identities and grouping. Replay/hydration can use the full
path; changed array identity or an unnotified length change forces a reseed.

Both live consumers resolve the same attachment-local projector. The UI requires
matching stream and registry identities. Disposal releases the retained rows and
makes the old projector inert; it cannot clear a replacement projector. The
existing stream disposal hook releases it on detach, supersession and reconnect.
Rewiring a transport in the same closure creates a fresh projector after disposing
the previous one. A stale disposal callback must still own the current source
before it may flush or cancel anything. Existing terminal prose cleanup releases
the projector. Transport disposal does not revoke authoritative recovery writes:
failed recovery (including its timeout) may settle the retained registry after
closing the source, provided no replacement attachment or registry owns it.
There is
no additional timer, scheduler, DOM owner, fade owner or scroll writer.

The registry's existing delayed cleanup remains separate. If its map entry no
longer matches, cached live projection is not used. Registry retention does not
retain a disposed projector. All private maps are bounded by the current turn's
rows and dirty indices, not by the number of past turns or sessions.

## Fast path and fallback contract

Tracked append/replacement work reads and builds only dirty rows. Multiple writes
to one row coalesce. Initialization, display-mode changes, owner/array identity
changes, unexpected length, row identity changes, text shrink and explicit
structural invalidation use full projection. Duplicate row IDs disable incremental
reuse until unambiguous input is reseeded. Tool metadata and activity groups still
come from the existing row builder; tool completion does not gain a separate
render path.

This is an explicit mutation protocol, not a transparent cache for arbitrary
mutable JavaScript objects. Same-length unnotified reorder or nested payload edits
cannot be detected in constant time without intercepting public objects or scanning
history. Such callers must invalidate the private projector or use the public full
projector. Every existing live source writer is covered; public projection remains
correct without any notification. New live mutation sites must join that protocol.

Projection source inspections and row builds are bounded by dirty rows, but total
rendering is **not** constant time. Publishing an updated immutable row array copies
row references; the existing #7495 renderer traverses projected rows/signatures.
Existing event-search helpers also remain outside these projection counters.
No claim is made that all source searches, string processing, DOM work or layout
is O(delta). Stage 1 semantic parser counters remain a separate gate.

## Validation

- `tests/test_pr7478_stage2_projection.py`: source-executing differential full-oracle
  checks for dirty rows, multiple writes, appends, tool metadata, reasoning, identity,
  reorder, shrink, duplicate IDs, reflection, snapshot stability and disposal.
- `node tests/fixtures/pr7478_projection_benchmark.cjs [checkout-root]`: comparable
  1,000-row/100-update historical workload, 10,000-row history and mixed dirty rows
  in both activity modes. Reports initialization separately, source rows inspected,
  projected rows built, full fallbacks and timing. The full oracle comparison is
  outside measured work. This is not a DOM benchmark.
- `.venv/bin/python tests/browser_pr7478_dirty_projection.py`: real Chromium with
  synthetic session/SSE data, row-builder and DOM-mutation observations, tool DOM
  reuse, reconnect, stale disposal, terminal release and full-scene comparison.
  Mutation counts are added/removed nodes, not physical layout or battery metrics.
- Existing #7495 energy, reconnect, progress/ring, settlement, historical hydration,
  viewport/scroll and complete Stage 1 semantic gates remain required.

Transport-generation fencing of arbitrary stale SSE callbacks is not implemented
here. Projection cleanup/identity is not proof of that separate transport guarantee.
