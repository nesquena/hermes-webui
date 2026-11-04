# Runtime contracts — Steer/Stop lock ordering and inactive-session recovery

Moved verbatim from `AGENTS.md` (headroom pointer pass, 2026-11-04). These contracts
bind every change to runtime, streaming, recovery, replay, compression, or sidebar
metadata: name the state layer being mutated and prove the relevant invariant.

## Active-run Steer

- Active-run Steer resolves the stream-bound agent with explicit stream and worker
  ownership before consulting the reusable session cache. Compression may rotate the
  agent identity; steering must never evict or close an agent.
- Keep HTTP response writes outside runtime registry locks. A Gateway-owned active run
  must resolve to the Gateway outcome before any local cache fallback, even when no
  in-process worker is registered for that stream.
- Stop publishes cancellation and detaches stream/agent entries under the same stream
  lock (STREAMS_LOCK -> ACTIVE_RUNS_LOCK); interrupt and session persistence remain
  outside it. The retained cache-only path (no registered worker) revalidates stream
  membership, owner, and active-run session/backend/phase and enqueues `agent.steer()`
  under that same lock edge, so a Stop that claims cancellation never strands guidance
  an earlier Steer response reported as accepted.
- Test both Stop/Steer orderings — registered and cache-only — with deterministic
  barriers. Initial active-run publication and its cancel flag must share that lock edge
  with Stop; do not recreate the flag after journal setup. Worker registration must also
  check its retained cancel event and live stream membership: Stop can remove
  CANCEL_FLAGS during initialization. Do not re-register a cancelled worker; finalize
  outside the stream lock.
- Local Steer accepts only explicit starting/running phases. Close admission by
  publishing finalizing under STREAMS_LOCK before the last pending-steer drain; earlier
  accepted guidance is drained, later guidance is rejected with `not_running` while the
  owned stream is still live (not `stream_dead`).
- Use one idempotent terminal settlement before done/error/end and final cleanup,
  covering returned errors, exceptions and self-heal, not only the success path.
- Merge Agent-returned `pending_steer` with the registered worker's final slot drain and
  emit leftovers before terminal events, outside registry locks. Test both drain/Steer
  orderings, including compression-rotated identities.

## Inactive-session recovery

- Inactive-session recovery is separate from live Steer. Resolve durable compression
  lineage in the session's profile database, read-only, even when the WebUI sidecar has
  no snapshot flag. Never reopen a sealed parent.
- Reject stale chat POSTs before workspace/model/pending-state mutation; the browser
  loads the continuation and preserves the draft without automatic replay. Explicit
  closures and unknown terminal reasons do not authorize a redirect.
