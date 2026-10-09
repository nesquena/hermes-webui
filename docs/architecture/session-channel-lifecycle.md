# Session channel and stream lifecycle contract

Current contract for the two independent registries that decide whether a WebUI session may
admit a new turn — the per-session SSE channel (`SESSION_CHANNELS`) and the per-turn stream
(`STREAMS` + `ACTIVE_RUNS` + the launch-phase claim) — plus the collection and reclaim rules
that keep a dead consumer or a dead worker from blocking a session indefinitely.

Start here before changing subscriber liveness, channel collection, stream registration,
`chat/start` admission, or the reaper (#7302).

## State layers and ownership

| Layer | Key | Authoritative writer | Released by |
|---|---|---|---|
| `SESSION_CHANNELS` (under `SESSION_CHANNELS_LOCK`) | `session_id` | `api/background_process.py` | channel close + `_reaper_loop` collection |
| `SessionChannel._subscribers` | subscriber queue | the SSE handler (`api/routes.py::_handle_session_sse_stream`) subscribes; the channel owns the list | handler `finally` (unsubscribe) |
| `SessionChannel._last_write_ok_at` | subscriber queue | the SSE writer, via `note_subscriber_write_ok()` after every COMPLETED write | `unsubscribe()` |
| `SessionChannel._stalled_since` | subscriber queue | `emit()` records a `queue.Full` run; the collection path clears it | `unsubscribe()` / drain |
| `STREAMS` (under `STREAMS_LOCK`) | `stream_id` | the launch sites in `api/routes.py` and the gateway reattach in `api/gateway_chat.py` | `release_stream_owned_registries()` |
| `ACTIVE_RUNS` (under `ACTIVE_RUNS_LOCK`) | `stream_id` | the worker admission paths (`api/streaming.py`, `api/gateway_chat.py`) | worker `finally`, or Stop |
| `PRE_ADMISSION_CLAIMS` (under `STREAMS_LOCK`) | `stream_id` | the launch site that registered the stream | worker admission, Stop, launch failure, canonical teardown |
| Stream-owned siblings — owner, goal marker, partial/reasoning text, live tool calls, last event id | `stream_id` | the stream's writer/handler | `_release_stream_owned_rows()` |
| Gateway run rows (`api/gateway_chat.py`) | `stream_id` | the gateway run | `release_gateway_stream_state()` |

`stream_owned_registries()` resolves that set on EVERY call (module attribute lookup, not a frozen
tuple), so a rebound registry is never leaked by a teardown holding the old object.

## Launch phase: registration → admission

1. **Register.** Create the channel, `register_stream_owner()`, and inside ONE `STREAMS_LOCK`
   critical section publish `STREAMS[stream_id]` **and** `publish_pre_admission_claim()` — on
   **every** launch edge: ordinary start, regeneration, `/btw` and `/api/background`. All four are
   load-bearing since the reaper sweeps orphans (a claimed stream whose worker has not been
   admitted yet is launching, not dead).
2. **Launch.** The worker is scheduled but not yet admitted. The regeneration path holds it at
   `release_worker.wait()` while `s.save()` runs, so this window is unbounded in practice.
3. **Admit.** The worker registers in `ACTIVE_RUNS` and retires the claim **on the same lock
   edge, after the registration** — so there is never a moment with neither blocker.
4. **Tear down.** `retire_pre_admission_claim_if_owned()` retires by identity: in Stop (after the
   cancellation is published, before ownership is detached), on launch failure, and
   `_release_stream_owned_rows()` pops it in the canonical funnel.

## Subscriber liveness and channel collection

`reaper_should_collect(now)` is true when any of these holds:

- no subscribers, and `last_subscriber_drop_at` is older than
  `SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS` (60 s) — normal teardown;
- **no subscribers** AND the channel has outlived `SESSION_CHANNEL_IDLE_TTL_SECS` (14400 s) — a
  hard lifetime cap that also sweeps a channel whose subscribers oscillated. With a subscriber
  attached the only admissible evidence is the dead-subscriber signal below: **age alone never
  collects a channel that still has one**;
- **every** attached subscriber is dead, where a subscriber is dead when EITHER
  - its queue rejected broadcasts (`queue.Full`) continuously for
    `SESSION_CHANNEL_SUBSCRIBER_STALL_SECS` (300 s) — cleared as soon as the queue has capacity
    again, because a dequeue is progress; OR
  - no write to it COMPLETED for `max(STALL_SECS, 3 × SESSION_CHANNEL_KEEPALIVE_SECS)`.

A single live subscriber protects the channel.

Writer-side liveness exists because queue pressure is blind to an idle session: nothing is
emitted, so a 64-slot queue never fills, so a half-open socket (client gone, no FIN ever
reaching us) looks exactly like a quiet healthy one. The keepalive write is the only thing a
healthy idle subscriber completes, which is what makes "no completed write for a whole window"
positive evidence of death rather than evidence of quiet.

⚠️ **Coupling.** `SESSION_CHANNEL_KEEPALIVE_SECS` (`api/background_process.py`) mirrors
`_SSE_HEARTBEAT_INTERVAL_SECONDS` (`api/routes.py`) as a plain number because `routes` imports
`background_process` — importing back would be circular. Keep them in step: a real interval at
or above the staleness window would let the signal collect a healthy idle subscriber.
`tests/test_7302_writer_liveness.py` pins the mirror.

## The orphan definition (one predicate, three readers)

`api/config.py::is_orphaned_stream(stream_id, *, pending_turn_in_window=False, streams_lock_held=False)`
is orphaned only when all of these hold: registered in `STREAMS`; no row in `ACTIVE_RUNS` (fail
closed if that registry cannot be read); no `PRE_ADMISSION_CLAIMS` entry; and the caller's
`pending_turn_in_window` is false.

- `_active_stream_blocks_chat_start` — keeps a non-orphan; a confirmed orphan is cleared from the
  whole stream-owned set so the next `chat/start` is admitted.
- `_session_has_active_turn` — an orphaned stream must not keep reporting the session as busy.
- `_reaper_loop` — reclaims orphaned streams through **`release_orphaned_stream_if_still_orphaned()`**,
  which re-decides *and* releases on one `STREAMS_LOCK` edge (a decision followed by a separate
  release would let a worker admit itself in the gap and lose a live stream), plus
  `release_gateway_stream_state()` outside the lock.

The claim check is mandatory. Without it a stream that is still launching is classified as
orphaned and reaped, and its first worker exits without running its turn (#7302, maintainer
finding 5).

## Lock discipline

- `STREAMS_LOCK → ACTIVE_RUNS_LOCK` is the documented order (Stop/Steer and both admission
  paths). Never take them the other way.
- Snapshot one lock's contents, release it, then take the other — as `_emit_to_session_streams`
  and the reaper's orphan sweep do.
- `threading.Lock` is not reentrant: helpers reachable with `STREAMS_LOCK` already held take
  `streams_lock_held=True`.
- Settle the decision and the mutation on the same lock edge: emit, stall record, claim, close.

## Invariants

| # | Invariant | Enforced in | Pinned by |
|---|---|---|---|
| I1 | A registered stream whose launch-phase claim is not retired keeps blocking `chat/start`, whatever the pending age. | `_active_stream_blocks_chat_start` | `tests/test_7302_pre_admission_claim_barrier.py` |
| I2 | The claim is published in the same critical section as the `STREAMS` entry **on every launch edge** (ordinary start, regeneration, `/btw`, background), and retired by identity at admission, Stop, launch failure, and the canonical teardown. | `api/config.py`, all four launch sites, `cancel_stream` | `tests/test_7302_pre_admission_claim_barrier.py` |
| I3 | A stream is an orphan under exactly one definition, and the launch claim is part of it. | `is_orphaned_stream()` | `tests/test_7302_orphan_stream_predicate.py` |
| I4 | A healthy idle subscriber can never be collected: it proves itself on every keepalive. | writer-liveness signal | `tests/test_7302_writer_liveness.py` (negative control) |
| I5 | A subscriber that just proved liveness is never closed by a decision taken on stale state. | revalidation under the channel lock | same file |
| I6 | Only the exact stream id is cleared, and the whole stream-owned set goes together. | `release_stream_owned_registries()` | `tests/test_reaper_drain_progress.py`, `tests/test_session_channel_explicit_close.py` |
| I7 | An orphan is reaped only while it is still an orphan: the decision and the release share one `STREAMS_LOCK` edge. | `release_orphaned_stream_if_still_orphaned()` | `tests/test_7302_orphan_stream_predicate.py` |

## What not to do

- Do not infer worker liveness from `STREAMS` membership alone.
- Do not publish an `ACTIVE_RUNS` row to cover the launch window — that is the phantom-row class
  finding 3 retired; use the launch-phase claim.
- Do not add a second definition of "orphan".
- Do not collect a channel on queue pressure alone: it is blind to idle sessions.
- Do not nest `STREAMS_LOCK` inside `ACTIVE_RUNS_LOCK`.
