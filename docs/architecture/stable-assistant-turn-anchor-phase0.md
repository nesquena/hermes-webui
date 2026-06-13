# Stable Assistant Turn Anchors Phase 0 Inventory

This inventory implements the first non-visual slice of
[`stable-assistant-turn-anchors.md`](../rfcs/stable-assistant-turn-anchors.md).
It documents the current per-turn state layers and the event-shape contract that
future anchor phases must consume. It does not claim that anchors are wired into
streaming or rendering yet.

## RFC Phase Progress

- The #3962 Phase 0 scaffold shipped through #3977 / v0.51.359: inventory the
  current state layers, encode the owner seed, and pin the source classification
  contract.
- PR #3980 / v0.51.366 delivered the first RFC Phase 2 foundation: normalize
  current live, replay, and settled source events into anchor-shaped events while
  staying unwired from rendering.
- This slice advances RFC Phase 1 and Phase 2 together: it adds a local registry
  owner plus a shadow source-feed harness that can combine live, replay,
  settled, and in-flight observations into one anchor snapshot.
- It also covers the RFC Phase 2.5 contract-hardening boundary: the semantic
  anchor seed excludes renderer presentation state, terminal states are exposed
  as constants with alias normalization, and replay + settlement ordering is
  pinned by tests before visible wiring begins.
- Slice 4 starts RFC Phase 3 by routing settled assistant final prose through the
  anchor owner before `renderMessages()` renders the final assistant body.
- Slice 5 starts RFC Phase 5 by projecting anchor-owned activity events into a
  renderer-neutral activity scene that Compact Worklog and Transparent Stream
  can later consume from the same ordered rows.
- Slice 6 starts the live shadow-feed boundary: `attachLiveStream()` now creates
  a per-stream anchor registry and feeds non-token live activity events into it
  without changing either current renderer.
- Slice 7 adds the dual-run reconciler for the renderer handoff: it compares a
  current Compact Worklog / Transparent Stream renderer-row snapshot with the
  anchor-owned `activity_scene_v1` rows and reports missing rows, extra rows,
  order changes, and field mismatches before visible renderer replacement.
  `S.messages`, `INFLIGHT`, stream-local state, and DOM nodes remain
  projection/cache layers outside the settled final-prose path and the live
  shadow registry.

## State Layers

| Layer | Current surface | Phase 0 anchor policy |
| --- | --- | --- |
| RuntimeAdapter / run-journal Event Envelope | `event_id`, `run_id`, `seq`, `Last-Event-ID` / `after_seq` | Preferred identity and replay dedupe source. |
| Run journal replay events | `read_run_events()`, `_replay_run_journal`, `runtime_journal_snapshot` | Durable replay hydration source before browser caches. |
| Server settled transcript | `/api/session` messages and metadata | Settlement updates final answer and terminal state on an existing turn. |
| `S.messages` | Browser transcript projection consumed by `renderMessages()` | Projection/cache, not a second semantic owner. |
| `INFLIGHT` | Browser recovery cache and persisted localStorage state | Recovery fallback only; does not outrank journal or settled transcript. |
| Stream closure state | `attachLiveStream()` local assistant text, reasoning text, parser target, tool state | Hot-path write buffer; future phases normalize this into anchor events. |
| Live DOM | `#liveAssistantTurn`, Worklog rows, tool cards, Thinking cards | Renderer output only; DOM survival is not semantic truth. |

The same inventory is encoded in `static/assistant_turn_anchors.js` as
`HermesAssistantTurnAnchors.stateLayers` so tests can pin the current authority
order.

## Slice 2 Normalizer Helper

`HermesAssistantTurnAnchors.normalizeAssistantTurnAnchorSourceEvent()` converts a
single current source event into a normalized anchor event envelope without
registering it, rendering it, or mutating browser state. It accepts live SSE-like
events (`type`, `data`, `lastEventId`), replay/journal-like events (`event`,
`payload`, `event_id`, `seq`), and settled/session payload events such as
`settled_message`.

`HermesAssistantTurnAnchors.normalizeAssistantTurnAnchorSourceEvents()` applies
the same helper to a list and dedupes repeated live + replay observations by the
same event-envelope key. This is still inert: `send()`, `attachLiveStream()`,
`renderMessages()`, settlement restore, `S.messages`, `INFLIGHT`, and the DOM do
not consume the helper yet.

## Slice 3 Registry / Owner Skeleton

`HermesAssistantTurnAnchors.createAssistantTurnAnchorRegistry()` creates a local
owner object for one assistant turn. The registry contains the anchor seed, a
dedupe index, and application stats. It is not a global store and is not wired
into current runtime, session, or renderer code.

`HermesAssistantTurnAnchors.applyAssistantTurnAnchorSourceEvent()` and
`applyAssistantTurnAnchorSourceEvents()` normalize incoming source events, apply
the same event-envelope dedupe rule, and route events into one owner:

- `activity_events` for visible assistant activity such as prose, reasoning,
  tools, control boundaries, and terminal status
- `artifacts` for workspace/file references
- `side_effects` for persisted state side effects
- `metadata_events` for settlement/session metadata such as `settled_message`
- `transport_events` for transport-only signals such as `stream_end`

The registry may fill missing `run_id` / `stream_id` identity from the first
matching normalized event, update lifecycle on terminal status, and copy the
settled assistant message into `content.final_answer` as a derived render
snapshot while keeping `content.final_message_ref` as the settled transcript
reference. It rejects mismatched session or turn identity and skips duplicate
live + replay observations by the same dedupe key.

This slice deliberately keeps the ownership boundary inert: `send()`,
`attachLiveStream()`, replay hydration, `renderMessages()`, `S.messages`,
`INFLIGHT`, and DOM continuity still do not consume the registry. Later slices
can replace local renderer-owned state with this owner instead of adding another
parallel source of truth.

`HermesAssistantTurnAnchors.createAssistantTurnAnchorShadowSnapshot()` is the
shadow wiring harness for this slice. It accepts grouped `live_events`,
`replay_events` / `run_journal_events`, `settled_events`, and `inflight_events`,
feeds them through one local registry, and returns the resulting snapshot plus
per-source apply results. This gives later slices an invariant target without
making the current UI consume the owner yet.

Renderer-only UI state such as Compact Worklog expansion, Transparent Stream
expansion, copy-button visibility, and scroll-follow preference is intentionally
not stored in the anchor seed. Those choices belong in renderer state or a
separate per-session UI preference store so replay and settlement do not carry
historic display preferences as semantic facts.

`HermesAssistantTurnAnchors.terminalStates` exposes the RFC terminal-state enum:
`completed`, `cancelled`, `interrupted`, `no_response`,
`tool_limit_reached`, `compression_exhausted`, `connection_lost`, `degraded`,
and `error`. `normalizeAssistantTurnAnchorTerminalState()` maps current source
aliases such as `done`, `cancel`, `apperror`, `interrupted-by-user`,
`max_iterations`, and `lost_worker_bookkeeping` into that enum.

During the later `INFLIGHT` migration, the registry is the semantic owner for
event identity, lifecycle, final answer reference, and activity events.
`INFLIGHT.lastRunJournalSeq`, `activityBurstAnchors`, `currentLiveSegmentSeq`,
`streamId`, and cached live text/tool state remain recovery or renderer caches
until the matching field is explicitly moved. The fallback order is journal
replay first, settled transcript second, `INFLIGHT` only for gaps.

## Slice 4 Settled Final Projection

`HermesAssistantTurnAnchors.projectAssistantTurnAnchorSettledMessageFinalAnswer()`
projects one settled assistant transcript message through a local anchor
registry. The settled transcript message reference remains the semantic
authority (`content.final_message_ref`); `content.final_answer` is a derived
render snapshot for the existing markdown pipeline.

`renderMessages()` uses that projection only for settled assistant messages
(`!isUser && !m._live`) and only after preserving the current content-array
flattening behavior. It then continues through the existing inline-thinking and
markdown rendering pipeline. If the anchor helper is unavailable or cannot
produce a final answer, `renderMessages()` falls back to the existing message
content path.

This is intentionally narrower than render-scene ownership: live stream tokens,
replay hydration, worklog rows, transparent-stream rows, tool cards, `INFLIGHT`,
and DOM continuity are still not consumed by the anchor registry in this slice.

## Slice 5 Activity Scene Projection

`HermesAssistantTurnAnchors.projectAssistantTurnAnchorActivityScene()` projects
an anchor or registry into `activity_scene_v1`: identity, lifecycle,
`final_answer`, `final_message_ref`, terminal state, and an ordered
`activity_rows` list.

The rows are renderer-neutral. Compact Worklog receives display hints such as
`main_prose`, `collapsed_thinking`, `tool_row`, and `terminal_status_row`.
Transparent Stream receives the same row IDs, order, kinds, roles, text, tool
IDs, and sanitized payloads with a chronological display hint. This pins the
shared input shape before either renderer is rewired.

This slice is still inert. No current UI module consumes the activity scene.
`renderMessages()` and the live streaming hot path were unchanged by Slice 5.

## Slice 6 Live Shadow Feed

`attachLiveStream()` now creates or reuses a per-stream local registry in
`window._liveAnchorRegistries` and feeds current live activity events through
`HermesAssistantTurnAnchors.applyAssistantTurnAnchorSourceEvent()`. This is a
shadow feed only: Compact Worklog, Transparent Stream, `renderMessages()`,
`S.messages`, `INFLIGHT`, and DOM continuity do not read from the registry yet.

The feed intentionally skips `token` events. Token events can arrive at high
frequency and would turn the anchor into a per-token append log before the
renderer reconciliation slice has proven the row model. Reasoning deltas are
also not fed one-by-one; Slice 6 flushes one aggregate reasoning event before a
terminal or settled-restore path. The feed captures the non-token activity
boundaries that define the future scene: interim assistant segments, tool
start/complete, approval, clarify, goal continuation, pending steer leftovers,
compression lifecycle, app errors, cancel, and done.

The SSE `Last-Event-ID` value is copied into the source event before applying it
to the registry, with current event-id fallbacks preserved. Existing registries
are reused by `stream_id` so a reconnect continues the same dedupe ring instead
of starting a parallel owner. Completed, errored, or cancelled streams schedule
registry cleanup after a retention window. Permanently failed network-error
paths schedule a shorter cleanup window after recovery/restore options are
exhausted.

The `done` feed is deliberately slim: status, usage, and creation timestamp are
copied, but the full settled session payload is not duplicated into the live
registry. When the active settled assistant message is available, it is stamped
with `_anchor_stream_id` so later reconciliation can associate the settled
message with the live shadow registry. That field is treated as client-side
ephemeral turn metadata and is carried forward across session refreshes.

EventSource network `error` remains a transport/recovery signal and is not fed
as an anchor terminal event in this slice. Runtime app errors continue through
the existing `apperror` event path and are fed as terminal activity only when
they match the current session.

## Slice 7 Dual-Run Reconciler

`HermesAssistantTurnAnchors.reconcileAssistantTurnAnchorActivityScene()` compares
the anchor-owned `activity_scene_v1` projection against a renderer-derived row
snapshot. It is a shadow harness, not a renderer. Callers pass the current
renderer's observed rows as plain summaries, and the helper returns
`activity_scene_reconciliation_v1` with:

- expected and actual row summaries,
- the comparison fields used,
- row-count, missing-row, unexpected-row, order, and field mismatch diagnostics,
- identity and terminal-state context from the anchor scene.

This slice keeps the comparison renderer-neutral. Compact Worklog and
Transparent Stream can each provide their own row snapshots, while the expected
side always comes from the same anchor scene. Matching rows prove the current
renderer can be replaced by an anchor-backed renderer for that event shape;
mismatches identify the specific event kind, tool identity, status, or ordering
gap that must be fixed before the visible handoff.

No current hot path consumes the reconciler. `renderMessages()`, live SSE
callbacks, `S.messages`, `INFLIGHT`, Compact Worklog, Transparent Stream, and
DOM continuity continue to render exactly as before until a later replacement
slice deliberately switches a renderer to anchor-owned rows.

## Source Event Classification

Phase 0 classifies current sources before changing render behavior:

- activity: `token`, `interim_assistant`, `reasoning`, `tool`,
  `tool_complete`, `tool_update`, `compressing`, `compressed`, `approval`,
  `clarify`, `pending_steer_leftover`, `goal_continue`, `done`, `cancel`,
  `error`, `apperror`
- artifact: `artifact_reference`
- side effect: `state_saved`
- metadata: `usage`, `title`, `settled_message`, `runtime_journal_snapshot`,
  `inflight_snapshot`
- transport: `stream_end`

Future phases may add sources, but every source must choose one of these classes
or explicitly mark itself `excluded`.

## Dedupe Invariant

Anchor event dedupe is intentionally independent of visible text and timestamps.
The Phase 0 helper uses this order:

1. `event_id`
2. `run_id + seq`
3. `session_id + source_event_type + local_id + seq` as a browser fallback only
   when a concrete local `seq` is present

This mirrors the RuntimeAdapter Event Envelope and keeps the browser aligned
with run-journal replay while the anchor registry is still unwired.

The registry tests also pin the reconnect/settlement race shape: if one run is
observed live, replayed, and settled in either order, duplicate event envelopes
are skipped and the resulting anchor has the same activity list, terminal state,
final message reference, final-answer snapshot, and usage metadata.
