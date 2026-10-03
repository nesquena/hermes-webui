# WebUI Run State Consistency Contract

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-05-16
- **Updated:** 2026-09-26
- **Tracking issue:** [#2361](https://github.com/nesquena/hermes-webui/issues/2361)
- **Related architecture:** [#1925](https://github.com/nesquena/hermes-webui/issues/1925), [`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md), [`stable-assistant-turn-anchors.md`](stable-assistant-turn-anchors.md)

## Problem

A single WebUI agent turn is represented by several overlapping state layers:

- the visible transcript the user can read,
- the model context / `context_messages` the agent actually receives,
- `pending_user_message` and active stream metadata,
- live SSE events and in-memory stream state,
- durable run journal / replay state,
- automatic compression summaries and active-task handoff text,
- the browser's live timeline DOM/cache,
- sidebar ordering, unread state, and `updated_at` metadata,
- derived model/context metadata the UI reads (model catalog caches,
  context-window limits) without re-syncing its source.

Those layers are not independent. When they drift apart, the user sees failures
that look unrelated: a prompt is visible but missing from recovered model
context, a live run loses or reorders thinking/tool cards after switching
sessions, cleanup makes old sessions look newly active, replay duplicates content,
or automatic compression reference material appears inside the active turn.

This RFC defines a consistency contract for those layers. It complements the
larger run adapter direction in #1925 by documenting what must remain coherent
while WebUI still has multiple overlapping state stores.

## Agent registration after cancellation

Initial and credential self-heal Agent construction use the same registration
boundary. Under `STREAMS_LOCK`, both the worker-retained cancellation event and
live stream membership must permit registration. A removed `CANCEL_FLAGS` entry
is not permission to restart. If Stop won during initial or self-heal
construction, the candidate must not enter the reusable cache or call
`run_conversation`. Stream registration, reusable-cache publication and the
in-memory lifecycle handle share one atomic Stop admission, using lock order
`STREAMS_LOCK` then `SESSION_AGENT_CACHE_LOCK`. Merely moving a cache write
after a cancellation check does not protect that check-to-publication gap.

After prompt preparation and immediately before each initial/self-heal invocation,
revalidate the retained cancel event, stream membership and exact registered
Agent. If Stop already won, retire a matching reusable entry only while the
existing `SESSION_WRITEBACK_OWNERS` record still equals this exact stream.
Hold that ownership lock through cache/lifecycle retirement. Object identity is
insufficient because successors can reuse the same Agent; absent ownership is
not permission either, since a completed successor clears its record. Never
clear a successor's cache or lifecycle handle, and do not issue another Agent
interrupt for an invocation that never started. Rejected cache-hit registration
likewise must not interrupt the borrowed Agent; only a never-published newly
constructed candidate may receive construction-cancellation cleanup. Drop the
old worker's local borrowed handle as well, so final pending-Steer drain cannot
reach a successor through an object-reference fallback.
Stop after invocation admission uses the existing Agent interrupt mechanism;
registry locks must not span provider or tool execution. LRU eviction/close stays
outside the stream lock and retains the existing active-worker policy.

Interrupt and cancellation finalization occur outside the stream registry lock.
Session finalization still owns the session lock: the returned-error path already
holds it, while initial registration and the exception path acquire it. Do not
reacquire this non-reentrant lock from a branch that already owns it.

## Run-journal sequence publication

Within one WebUI process, auto-numbered appends to the same journal allocate
sequence numbers and write their rows under the same per-path lock.
`RunJournalWriter` delegates both operations to `append_run_event`; it must not
reserve a sequence and release the lock before the physical append. Otherwise
individually valid rows can reach disk out of order and the session replay
reader must reject them as noncontiguous. This does not change caller-supplied
sequence semantics, cross-process ownership, or failed-write recovery.

## Inactive compression continuation recovery

The Agent profile's SQLite compression lineage owns the canonical continuation,
including when Desktop/CLI compressed a session without updating WebUI's
`pre_compression_snapshot` sidecar flag. `GET /api/session` may expose the
existing `continuation_session_id` hint from that read-only lineage. Automatic
`idle_timeout` closure does not hide the continuation; explicit/unknown terminal
reasons, foreign-profile rows and delegated/tool children do not authorize it.
This read does not reopen sessions or copy ancestor display history into context.

A stale `POST /api/chat/start` returns HTTP 409 with `code=session_rotated`
and the continuation hint before workspace, model, pending-turn or worker
mutation. The browser loads the continuation through normal session access
checks and restores the rejected text and attachments as a draft. The user
sends again explicitly; there is no automatic POST replay or migration of the
parent's workspace binding. Clients without this handling must reload the
session before retrying. Server wakeups, regeneration semantics and Gateway
routing are not silently retargeted by this recovery path.

## Goals

- Define the state layers involved in active and recovered WebUI turns.
- Make the source-of-truth expectations explicit for each layer.
- Give reviewers a checklist for streaming, replay, compression, recovery,
  model-context, and sidebar changes.
- Map recent real issues to reusable invariants so future fixes do not solve the
  same class of bug one symptom at a time.

## Non-goals

- Do not implement a runner process, sidecar, or new runtime boundary here.
- Do not replace #1925 or the run adapter contract.
- Do not rewrite the streaming protocol in this RFC.
- Do not reopen already-fixed narrow bugs.
- Do not make this a catch-all for unrelated UI polish.

## Current implementation relationship

Stable Assistant Turn Anchors now implement the presentation/reconciliation
portion of this contract for one assistant turn. The run journal and settled
transcript provide durable observations; the Anchor registry and
`activity_scene_v1` reconcile those observations into Compact Worklog,
Transparent Stream, or Final answer only; `S.messages`, `INFLIGHT`, renderer
caches, and DOM remain projections or recovery caches rather than independent
semantic owners.

This RFC remains `Proposed` because its broader cross-layer contract also covers
model-context reconstruction, compression handoff, session metadata, and future
runtime-adapter migration. Shipped Anchor coverage strengthens invariants 2, 3,
and 5; it does not mark every run-state boundary implemented.

## State Layers

| Layer | Purpose | Source-of-truth expectation | Must not do |
|---|---|---|---|
| Visible transcript | Shows what the user and assistant said | Session transcript plus live replay should produce one chronological user-visible story | Hide the user turn that started active work, or show internal recovery text as current user intent |
| Model context / `context_messages` | Supplies conversation state to the agent | Must include the current visible user turn unless deliberately excluded with a user-visible reason | Let the agent resume from context that contradicts what the user can see |
| Pending turn metadata | Bridges submitted-but-not-yet-finalized user input | Must identify the user turn and stream that own active work | Become a permanent duplicate transcript row after recovery |
| Live stream / SSE | Delivers active runtime events to the browser | Must remain an observation path, not the only durable truth for already-emitted events | Lose the visible scene on refresh, reconnect, or session switch |
| Worker lifecycle registry (`ACTIVE_RUNS`) | Tracks whether a worker still occupies the session, so a successor turn cannot start on top of it | Broader than "attachable UI work": a cancelled worker stays registered while it unwinds | Be read directly as the set of runs a browser may attach to |
| Run journal / replay | Rebuilds emitted runtime events after reconnect or restart | Must be cursor-safe and idempotent | Duplicate assistant text, thinking text, tool cards, or compression cards |
| Compression summary / handoff | Gives the agent recovery context after automatic compression | Must remain agent-facing recovery material unless explicitly rendered as history | Pollute the active turn or become implicit current user intent |
| Live UI scene/cache | Preserves expanded rows, in-progress cards, local scroll, and transient grouping | May optimize presentation but must be rebuildable or degradable from transcript/replay | Become the only place where chronological ordering exists |
| Sidebar/session metadata | Helps the user find active and recent sessions | Must reflect meaningful user or assistant activity | Treat background cleanup as a fresh user-facing update |
| Client-side unread stores (`localStorage`) | Backs the sidebar unread dot for every client on the origin | Converges counts/markers across clients and stores clear ordering independently per session | Let one client's stale cache lower a count or resurrect a cleared marker |
| Derived model/context metadata | Projects the model catalog, plus context-window and threshold limits, into pickers, context bars, and compression thresholds | Must re-sync from its own source — config/`/api/models` for the catalog, session/stream usage for window and threshold — before it is rendered or acted on | Outlive its source (stale TTL cache, stale usage after resume or model switch) and drive thresholds silently |

### Authority matrix

The table above says what each layer is for. This matrix is the reviewable
form of that contract: for every layer, who owns it, how long it persists, what
divergence is tolerated, and how replay/recovery must treat it. Anchors are
symbol names, never line numbers, so the matrix stays true across source-layout
shifts (#5513, #5542).

| Layer | Authority | Persistence lifetime | Allowed divergence | Replay / recovery rule |
|---|---|---|---|---|
| Visible transcript | Settled sidecar session file (`SESSION_DIR`, `Session.messages`); while a turn runs, the streamed scene feeding it | Durable on disk until the session is deleted; `.json.bak` retained for recovery | May trail the live stream by in-flight events; may downgrade to labeled structured replay, never to silently reordered rows | Rebuild chronologically from sidecar rows plus run journal events, letting `recover_session()` restore a larger `.json.bak` first; never from the browser cache |
| Model context (`context_messages`) | Server-side reconstruction over `Session.context_messages` at handoff time | Rebuilt per turn; persisted only as far as the sidecar persists it | May differ from the visible transcript only for deliberately excluded turns, with the reason shown to the user | Recovery must re-include the visible or pending user turn (invariant 1) before any continuation is requested |
| Pending turn metadata | `pending_user_message` with `pending_started_at` / `pending_user_source` on the session record | From submit until the turn is checkpointed into `Session.messages` and the field is cleared | Metadata only; must never become a second transcript row | Turn journal (`TURN_JOURNAL_DIR_NAME`) re-derives state; `_latest_user_matches_pending_text` decides whether a recovered pending turn is already checkpointed |
| Live stream / SSE | Observation path only: `STREAMS` channels and the session events routes | Process memory, per stream; gone on restart | May lose events on disconnect; anything already emitted must remain recoverable elsewhere | Replay from `RUN_JOURNAL_DIR_NAME` with a cursor; live and replayed events share one renderer |
| Worker lifecycle registry (`ACTIVE_RUNS`) | Occupancy — whether a worker still owns the session | Process memory; cancelling rows reclaimed after the bounded unwind window once they own no live `STREAMS` channel | Broader than attachable UI work (invariant 9) | Never replayed; re-derived empty at startup and repopulated by live work |
| Run journal / replay | Emitted runtime events: ordering, seq cursors, terminal states | `_run_journal` JSONL under `SESSION_DIR`, append-only, bounded snapshot args | Snapshot argument values may be truncated; event identity and `seq` must not change | Cursor-safe and idempotent: a resumed cursor never re-delivers settled events or duplicates cards |
| Compression summary / handoff | `compression_anchor_*` session fields produced by `is_context_compression_marker()` | Retained as anchor/recovery metadata; live-only divider rows are omitted from settled history | Agent-facing recovery material may exist with no matching user-visible row | Render as a quiet non-interactive divider only; later tool, reasoning, or interim events prove the barrier passed |
| Live UI scene/cache | None — presentation only: `INFLIGHT`, `INFLIGHT_STATE_*`, renderer caches, DOM | Tab-local; localStorage snapshots are best-effort and cleared on teardown | May be stale, degraded, or partially rebuilt | Rebuildable from transcript plus replay; if it cannot be, downgrade to explicit structured replay (invariant 3) |
| Sidebar/session metadata | Projection: `SESSION_INDEX_FILE` (`_index.json`) and the session list cache; counts come from the session store | Durable but derived; pruned and rebuilt by recovery (`_rebuild_recovery_session_index`) | May lag counts briefly; must never be refreshed by maintenance as if it were activity (invariant 4) | Rebuilt after recovery or repair so restored rows appear immediately |
| Client-side unread stores (`localStorage`) | Projection written by the sidebar layer in `static/sessions.js` — viewed counts, completion markers, and per-session clear records (`SESSION_VIEWED_COUNTS_KEY`, `SESSION_COMPLETION_UNREAD_KEY`) | Browser `localStorage` under the origin, shared by every client on that origin; clear/tombstone records age out on the documented retention window | Concurrent clients may transiently lose a whole-map entry; merges and storage-event repair must re-assert held facts | Fold stored markers and clear records per session; never lower a count across a transcript generation or resurrect a cleared marker |
| Derived model/context metadata | Two sources: the model catalog — source config (`config.yaml`, `_PROVIDER_MODELS`) and the `/api/models` response — and the window/threshold values the indicator reads from session and stream usage (`context_length`, `threshold_tokens`) | Catalog cache only: `STATE_DIR/models_cache.json` via `_get_models_cache_path`, plus in-memory `_available_models_cache` / `_available_models_cache_ts` TTL; usage values live with the session/stream payload and persist only as far as the session store does | Catalog may lag its source only within the TTL and must be invalidated when the source changes (#2443); usage may lag until the next payload, never across a resume or model switch | Catalog: re-read from `/api/models` after a source change. Usage: re-sync from the session/stream payload after a resume or model switch, before the UI renders context windows or compression thresholds (#2442) |

## Core Invariants

1. **Visible current turns enter model context.** If the user can see a current
   prompt and WebUI asks the model to continue that work, the prompt must be in
   the reconstructed model context unless WebUI shows an explicit reason it was
   excluded.
2. **Active turn UI keeps its owner.** The user turn that started active work
   must remain visible before assistant text, thinking cards, tool cards, or
   activity groups that belong to that work.
3. **Reattach preserves order or degrades clearly.** Refresh, reconnect, and
   session switch must preserve chronological live-scene order. If WebUI cannot
   restore the exact live scene, it should downgrade to an explicit structured
   replay state instead of silently reordering content.
4. **Maintenance is not activity.** Runtime maintenance such as stale-stream
   cleanup, orphan repair, or background compression must not refresh sidebar
   ordering, unread markers, or active-session affordances as if the user or
   assistant just acted.
5. **Replay is idempotent.** Replaying a run from a cursor must not duplicate
   transcript rows, thinking content, interim assistant text, tool cards, or
   compression cards. Replayed long-task events should enter the same
   browser-facing timeline renderer as live SSE events so recovery does not
   downgrade a structured Thinking / progress / tool / compression turn into a
   separate flattened presentation.
   When session loading combines a WebUI sidecar with Hermes Agent `state.db`, a
   native-image user turn may appear as both rich multipart content and scalar
   text that replaces each image part with `[screenshot]`. Reconciliation may
   treat those rows as one turn only when the multipart value contains text and
   recognized native-image parts, its exact scalar projection matches, role and
   tool shape match, timestamps match exactly, stable IDs and provider metadata
   do not conflict, and the pairing is unambiguous. Keep the rich sidecar row;
   if any requirement is missing or contradictory, preserve both rows rather
   than deduplicating. Literal scalar `[screenshot]` text alone is not identity
   evidence. A WebUI-submitted native-image turn has a separate display owner:
   keep its exact submitted text and attachment in the visible session row,
   while the Agent's expanded multipart row remains in `context_messages` for
   model replay. While the turn is active, the WebUI may hide an Agent user row
   from display only after its worker confirms that the active stream's exact
   `pending_started_at` value was passed to the Agent as
   `persist_user_timestamp`; persist that private proof with the session and
   validate it against the pending stream, source, and timestamp after reload.
   Never include the proof in public session payloads. This applies to
   native-image and scalar text-attachment rows, and never changes model
   context. If multiple user rows share that timestamp, omit the whole
   ambiguous display bucket until the turn settles; keep all rows in model
   context. Do not identify the row by its text. If a stream dies before
   settlement, state.db self-heal must save the submitted prompt and attachments
   as a visible sidecar row before clearing pending metadata only when there is
   genuine state.db output beyond that submitted turn. Otherwise, leave pending
   state intact for journaled partial-output and interruption-marker recovery.
   The Agent row remains available in model context. A partial continuation
   must use one consistent parent snapshot when projecting a conflicting
   provider payload onto its sidecar-owned display row.
   Match settled native-image scalar projections only with trusted turn and
   durable-row identity, never the marker alone. A durable row ID proves row
   identity, not provider-payload freshness: when the sidecar and state.db have
   conflicting nonempty `api_content`, preserve both versions for model-context
   replay without mutating either. For visible display, a marked mirror may
   share the existing sidecar bubble only when its valid durable row ID, exact
   timestamp, and exact visible user content match; keep the sidecar-owned row
   and its display metadata. Distinct row IDs, ambiguous or invalid identities,
   and different visible user text remain separate only while eligible under
   the existing edit/undo truncation watermark and checkpoint-order rules;
   removed rows must not reappear in display or model replay. Fill a missing
   payload from the other copy; repeated reconciliation must remain bounded
   and idempotent.
   Agent state.db alone cannot restore the original attachment if the WebUI
   sidecar is lost.
   Visible interim assistant progress must remain visible timeline content; a
   compact Activity disclosure may summarize adjacent tool/debug detail, but it
   must not be the only place where the user can see emitted progress text.
   Interim assistant text that duplicates the tail of the accumulated reasoning
   transcript is stripped from the reasoning copy so the restored snapshot
   shows the content once. That echo match is whitespace-insensitive and
   carries no fixed search window: a compact-equivalent suffix is recognized
   however much interior whitespace stretches its raw span. Both consumers
   (the live-stream echo path and the journal replay in `api/routes.py`)
   match through an incremental folded index (`_CompactEchoIndex` in
   `api/streaming.py`): the folded view and its raw cut offsets are built as
   each chunk is appended, so a probe costs O(len(candidate)) and never
   rescans the transcript's whitespace. The retired windowed variants could
   drop the strip when the span exceeded the window, duplicating the interim
   text; the retired raw backward walk was correct but re-walked the span per
   interim event, quadratic on whitespace-heavy transcripts. Regressions:
   `tests/test_live_snapshot_echo_dedup.py` pins the single-occurrence
   result, `tests/test_live_snapshot_echo_scan_scaling.py` pins the scaling
   property (a fixed-size fixture cannot catch a per-interim rescan), and
   `tests/test_compact_echo_index.py` pins index/oracle equivalence.
6. **Compression is not current intent.** Automatic compression summaries and
   reference cards are recovery/handoff material. They must not be treated as a
   new user request, active-turn content, or the default visible explanation for
   the current answer.
   Automatic compression may appear during a live turn only as a quiet,
   non-interactive context divider in the Worklog timeline, not as a clickable
   tool row. It should use action wording: `Compressing context` while active
   and `Context auto-compressed` when the agent has continued past the
   compression barrier or when a completion event arrives. The timer is
   diagnostic detail, not the source of truth for the divider's running state.
   Later tool, reasoning, or interim assistant events prove the compression
   barrier has passed even if no explicit completion event was delivered.
   Settled final history should omit live-only automatic-compression rows unless
   there is a user-visible recovery or error state to explain.
7. **Observation has a degraded path.** Long-running or many-session observation
   should expose enough heartbeat/degraded status that the UI does not appear
   silent and ordinary APIs do not stall behind active streams.
8. **Every mutation names its layer.** A PR touching streaming, recovery,
   context reconstruction, compression, replay, or sidebar metadata should state
   which layer it changes and what regression proves the invariant still holds.
9. **Lifecycle-busy is not client-attachable.** `ACTIVE_RUNS` answers "may a new
   turn start?", not "may a browser attach a renderer?". Cancellation splits the
   two: `cancel_stream()` keeps the row as `phase="cancelling"` so a successor
   cannot overlap the unwinding worker, but the client has already reached a
   terminal state for that stream because its run journal ends in a terminal
   event. Recovery paths that hand a stream id to a renderer — session SSE
   recovery and hidden-tab status polling — must therefore exclude cancelling
   rows, while busy/admission checks must keep counting them. Reading the
   registry with a single meaning resurrects a cancelled run on every fresh
   subscription: the client attaches, consumes the terminal event, tears the
   renderer down, resubscribes, and the loop repeats indefinitely.

   Because a cancelling row can otherwise persist forever, cancellation unwind is
   bounded: a cancelling row older than that window **and** owning no live
   `STREAMS` channel is reclaimed from `ACTIVE_RUNS` along with its stream-owner
   entry, so a wedged worker cannot suppress background wakeups permanently.
   Reclamation requires both conditions — age alone must not evict a row that
   still owns a live channel. Staleness is measured from the cancellation
   timestamp (falling back to run start), so a long-running turn cancelled
   moments ago is never mistaken for an orphan.

10. **Derived state is subordinate to its source.** Persisted caches,
    in-memory TTL caches, optimistic client flags, display counts, and sidebar
    rows project state they do not own. When a projection and its source
    disagree, the source wins, and a change at the source must invalidate or
    re-sync every downstream projection before it is rendered or acted on: a
    model catalog cache must not outlive a provider config change (#2443),
    context-window metadata — the session/stream usage `context_length` and
    `threshold_tokens` the indicator reads — must be re-synced after a session
    resume or model switch before the UI computes compression thresholds
    (#2442), and a stale client-side busy or optimistic flag must never block a
    new turn or override canonical idle server rows (#2796).
11. **Recovery leaves provenance, not only content.** Startup or repair that
    restores state from a backup or `state.db` (`recover_session()`,
    `recover_missing_sidecars_from_state_db()`) must also persist content-free
    metadata — recovered_from, recovered_at, recovery_reason, before/after
    message counts — that downstream consumers and audit or health endpoints
    can use to stay idempotent, and must rebuild derived indexes
    (`SESSION_INDEX_FILE`) so projections reflect restored state immediately.
    That provenance is maintenance, never user activity (invariant 4), and an
    intentional delete must not be resurrected by orphan-backup recovery.

## Client-side unread persistence (sidebar layer)

The sidebar unread dot is backed by two client-side stores in `static/sessions.js`.
Both live in `localStorage` under the origin, so every WebUI client on the same
origin/profile (a PWA window and a browser tab, for example) shares them while each
client also caches them in module state. They are projections of the sidebar layer
above; the rules below describe what stays coherent when more than one client
writes.

| Store | Key | Semantics |
|---|---|---|
| Viewed counts | `hermes-session-viewed-counts` | `sid -> {message_count, transcript_generation}`, meaning "seen up to N messages in this transcript generation" |
| Completion markers | `hermes-session-completion-unread` | `sid -> {message_count, completed_at, ...}` behind the visible dot |

- **Viewed counts are generation-scoped.** Session mutation routes increment the
  persisted `transcript_generation` whenever edit, regenerate, retry, undo, clear,
  or truncate reduces the visible transcript, and record the retained count as
  `transcript_generation_baseline`. Both fields survive the bounded `/api/sessions`
  projection for visible and sidebar-reference rows, including cached responses.
  A newer generation replaces an older one even
  when its count is lower; counts are monotonic only within one generation and
  merge by maximum there. A client first observing a newer generation acknowledges
  only that retained baseline, so messages added after the shrink remain unread
  even when the shrink and later growth arrive in one coalesced sidebar refresh.
  Legacy numeric records are generation zero and migrate to the structured
  representation on their next save. This prevents an old pre-truncate high-water
  mark from masking messages added after a transcript reset. A deletion records
  its own key under
  `hermes-session-viewed-counts:deleted:v1:<encoded-sid>`; merges drop any count
  whose session has a live deletion record, so a client that still caches the
  acknowledgement prunes it instead of writing it back. List membership cannot
  decide this, because the sidebar filters by profile, project, and source, so an
  absent row is not evidence of deletion. Deletion records expire on the same
  7-day policy as clear records, so a tab left open longer than that can re-add a
  count for a session deleted more than seven days earlier. That consequence is
  bounded and invisible: the session is no longer listed, so the retained entry
  produces no indicator, and it is re-examined only on the next deletion or clear.
- **Completion markers are ordered by logical stamps, not wall clock.**
  Markers are add/remove and cannot be max-ordered, so each clear records a stamp
  under `hermes-session-completion-unread-cleared:v1:<encoded-sid>:<stamp>`.
  Independent immutable keys mean clients clearing different sessions—or clearing
  the same session in an interleaved operation—cannot replace newer ordering
  facts, and a reader folds the maximum per session. Markers carry
  `unread_order`: the greatest stamp the marker's creator had observed (its own
  clear state, its cached and stored markers) plus one. A marker whose stamp does
  not exceed its session's clear stamp loses, so a clear wins the tie when a
  marker was prepared before it but written after; a completion that happens after
  the clear observes it and stamps higher, so it still wins. Milliseconds are not
  used for ordering: a clear and a genuine later completion can share one tick, and
  a single observed clock cannot order them.
- **The previous clear representation is migrated once, then dropped.** The
  unsuffixed `hermes-session-completion-unread-cleared` whole map is read, its
  facts are imported as independent records, and the key is removed. Keeping it
  would retain both of the defects it caused: concurrent clears could replace one
  another in the shared blob, and the blob grew with every session ever cleared.
  A client still running the previous revision therefore does not observe clears
  recorded after the migration; that reload boundary is deliberate, because a
  dual write cannot make the shared blob concurrency-safe. Versioned records are
  pruned by age (7 days), or when a newer ordering fact for that session was
  successfully persisted — never by an in-memory-only superseding clear and never
  by session existence, because the sidebar list is filtered by profile, project,
  and source, so an absent row may simply be hidden. They are never part of the
  marker map consumers read. A clear order is retained in module memory even when
  storage quota prevents allocating its versioned key; the client still attempts
  the smaller write that removes the marker from the existing marker map, so a user
  can dismiss unread state under storage pressure. The failed allocation also
  leaves any older durable clear record in place so reload does not lose the last
  persisted ordering fact. Logical clear order and retention time are separate:
  records compare markers using their order stamp,
  but the 7-day cap uses the wall-clock time at which the record was written, so
  a future logical stamp cannot extend retention indefinitely. The in-memory
  fallback lasts until reload, while successfully persisted records retain the
  same 7-day policy.
- **Cross-client repair, not cache invalidation.** The `storage` listener routes a
  changed unread key (including any per-session clear key) back through the same
  merge instead of only dropping the local cache. A client that still holds an
  acknowledgement re-asserts it after the other client's write, the loser sees a
  value it cannot beat and stops, and repair converges instead of ping-ponging
  storage events.
- **Whole-map facts converge; clear ordering does not share a map.** Viewed counts
  and completion markers still use read-modify-write maps, so interleaved writes
  can transiently lose an entry. Their monotonic cache merge and storage-event
  repair re-assert held facts. Clear ordering is different: each session has its
  own atomic `localStorage` write, so concurrent clears of different sessions
  cannot clobber each other. A viewed-count advance records its clear even when a
  competing client has prepared but not yet persisted the older completion
  marker; repeated observations at the same count do not refresh the tombstone.

## Review Checklist

Use this checklist for PRs that touch run state, streaming, replay, compression,
context reconstruction, or session metadata:

- Which state layers does this PR read or write?
- Which layer is the source of truth after this change?
- Can the visible transcript and model context diverge? If yes, is that
  deliberate and user-visible?
- What happens after browser refresh, session switch, SSE reconnect, and WebUI
  restart?
- Does replay rebuild the same scene without duplicates?
- Does replay use the same timeline-rendering path as live SSE for thinking,
  interim assistant text, tool cards, compression cards, and terminal states?
- Can this change move a session in the sidebar without meaningful user or
  assistant activity?
- Does this change read `ACTIVE_RUNS` for admission ("may a turn start?") or for
  attachment ("may a browser render this?"), and does it use the matching
  predicate for that question?
- If it introduces or changes a reclamation window, what proves an in-flight
  cancellation is not evicted early, and that a wedged one is eventually freed?
- Can automatic compression or recovery text become visible active-turn content?
- Does this change write one of the client-side unread stores
  (`hermes-session-viewed-counts`, `hermes-session-completion-unread`,
  `hermes-session-completion-unread-cleared`), and does it keep the merge and
  tombstone rules in the client-side unread persistence section?
- Which derived caches or client projections does this read or write, and what
  invalidates them when their source changes (invariant 10)?
- After a session resume or model switch, which metadata must be re-synced
  before the UI renders context windows, thresholds, or counts?
- Does any client-side optimistic or busy flag override canonical server state?
- If this restores state from a backup or `state.db`, what recovery provenance
  is persisted, which derived indexes are rebuilt, and how is an intentional
  delete told apart from an orphaned backup (invariant 11)?
- What test or manual evidence proves the invariant?

## Existing Issue Map

| Example | State boundary exposed | Layer | Relevant invariant |
|---|---|---|---|
| [#2341](https://github.com/nesquena/hermes-webui/issues/2341) / [#2342](https://github.com/nesquena/hermes-webui/pull/2342) | Active reattach could show agent activity without the pending user turn that started it | Pending turn metadata, Live UI scene/cache | 2 |
| [#2344](https://github.com/nesquena/hermes-webui/issues/2344) / [#2347](https://github.com/nesquena/hermes-webui/pull/2347) | Session switching could lose or reorder the live thinking/tool/interim timeline | Live stream / SSE, Live UI scene/cache | 3, 5 |
| [#2345](https://github.com/nesquena/hermes-webui/issues/2345) / [#2349](https://github.com/nesquena/hermes-webui/pull/2349) | Stale stream cleanup could mutate `updated_at` and resurface old sessions | Sidebar/session metadata | 4 |
| [#2346](https://github.com/nesquena/hermes-webui/issues/2346) / [#2348](https://github.com/nesquena/hermes-webui/pull/2348) | Thinking cards could repeat interim assistant progress text | Live UI scene/cache | 5 |
| [#2353](https://github.com/nesquena/hermes-webui/issues/2353) / [#2354](https://github.com/nesquena/hermes-webui/pull/2354) | Recovered pending user turns could be visible but missing from model context | Model context, Pending turn metadata | 1 |
| [#2355](https://github.com/nesquena/hermes-webui/issues/2355) / [#2357](https://github.com/nesquena/hermes-webui/pull/2357) | Auto-compression rotation could leave reference-only cards in the active conversation tail | Compression summary / handoff, Visible transcript | 3, 6 |
| [#2308](https://github.com/nesquena/hermes-webui/issues/2308) / [#2309](https://github.com/nesquena/hermes-webui/pull/2309) | Compressed sessions could resume stale agent tasks when the user starts an ordinary fresh chat | Compression summary / handoff | 6 |
| [#2283](https://github.com/nesquena/hermes-webui/pull/2283) | Run event journal replay provides the foundation for ordered recovery | Run journal / replay | 5 |
| [#2442](https://github.com/nesquena/hermes-webui/issues/2442) / [#2444](https://github.com/nesquena/hermes-webui/pull/2444) | Context-window metadata could stay stale after a session resume or model switch, driving premature compression | Derived model/context metadata | 10 |
| [#2443](https://github.com/nesquena/hermes-webui/issues/2443) | A persisted model-list cache could outlive the provider config change that should have replaced it | Derived model/context metadata | 10 |
| [#2796](https://github.com/nesquena/hermes-webui/pull/2796) / [#2797](https://github.com/nesquena/hermes-webui/pull/2797) / [#2801](https://github.com/nesquena/hermes-webui/pull/2801) | Stale optimistic busy state, non-deduped display counts, and session-level `tool_calls` overriding settled message metadata | Live UI scene/cache, Sidebar/session metadata | 10 |
| [#4208](https://github.com/nesquena/hermes-webui/pull/4208) / [#4221](https://github.com/nesquena/hermes-webui/pull/4221) | Coarse polling or focus recovery could refresh the active transcript late or without a session id | Live stream / SSE, Sidebar/session metadata | 3, 4 |
| [#4216](https://github.com/nesquena/hermes-webui/pull/4216) | Reconciliation could drop `state.db`-only user prompts that predate a newer sidecar tail | Visible transcript, Model context | 1 |
| [#4213](https://github.com/nesquena/hermes-webui/pull/4213) / [#4218](https://github.com/nesquena/hermes-webui/pull/4218) | Sidebar and lineage projections could hide real TUI-origin or multi-row session history | Sidebar/session metadata, Visible transcript | 10 |

These references are evidence for the contract. This RFC does not make the
linked implementation PRs dependent on this document, and it does not close the
tracking issue by itself.

## Relationship To The Run Adapter RFC

The run adapter RFC defines the longer-term event/control boundary for WebUI and
Hermes runtime ownership. This RFC defines the consistency rules that the current
WebUI and any future adapter-backed implementation must preserve.

The two documents should be read together:

- The adapter contract answers: "Where should execution ownership live?"
- This consistency contract answers: "How do transcript, context, streams,
  replay, compression, and UI metadata stay coherent while execution is active
  or being recovered?"

## Rollout Plan

1. Land this RFC as a reviewable draft and refine it through PR discussion.
2. Link future streaming/recovery/compression/sidebar PRs back to the invariant
   they intentionally preserve or change.
3. Convert recurring checklist items into focused regression tests where
   practical.
4. If #1925 introduces a new adapter-backed runtime layer, update this RFC or
   replace it with the accepted implementation contract so these invariants do
   not live only in historical discussion.
