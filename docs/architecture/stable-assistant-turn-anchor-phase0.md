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
- Slice 8 adds the renderer snapshot adapter that can extract
  `renderer_snapshot_v1` summaries from current Compact Worklog /
  Transparent Stream row hooks and feed them through the reconciler to produce a
  concrete matched / mismatched answer. The adapter remains opt-in and is not
  invoked by `renderMessages()` or the live SSE hot path.
- The first visible-order handoff switches live Compact Worklog rendering from
  legacy DOM mirroring to the per-stream anchor `activity_scene_v1` projection
  for same-browser active streams. Visible process prose, reasoning rows, and
  tool start/complete boundaries are represented as ordered scene rows. The
  existing Compact Worklog writers remain as fallback paths only when no anchor
  scene is available, and settled assistant messages may carry an in-memory
  `_anchor_activity_scene` snapshot so the folded activity summary can appear
  above the final answer. This handoff does not claim Transparent Stream wiring
  or durable hard-reload scene persistence.

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

## Slice 8 Renderer Snapshot Adapter

`HermesAssistantTurnAnchors.createAssistantTurnAnchorRendererSnapshot()` turns
current renderer rows into `renderer_snapshot_v1` summaries. The helper accepts
plain row-like objects or a DOM/root object with the existing renderer hooks:
Transparent Stream rows (`.transparent-event-row` /
`data-transparent-event-row`), Compact Worklog reasoning rows (`.wl-reason`,
`.agent-activity-thinking`, `.thinking-card-row`), and Compact Worklog tool rows
(`.tool-card-row`).

`HermesAssistantTurnAnchors.reconcileAssistantTurnAnchorRendererSnapshot()` is
the first one-call yes/no harness: it builds or accepts a renderer snapshot,
passes its rows into `reconcileAssistantTurnAnchorActivityScene()`, and returns
`renderer_snapshot_reconciliation_v1` with `matched: true` or `matched: false`
plus the underlying mismatch diagnostics.

This slice still does not change visible rendering. It gives later work a
bounded way to ask whether the current renderer output is equivalent to the
anchor-owned activity scene. A `matched: false` result is expected while current
renderers intentionally collapse or omit events, such as representing a tool
start + tool completion as one visible row or omitting terminal status rows.

## Compact Worklog Visible-Order Handoff

The first renderer handoff deliberately targets the live Compact Worklog path
only. `attachLiveStream()` keeps the existing streaming markdown parser as a hot
write buffer, but every visible process-prose segment is upserted into the
per-stream anchor registry as a single `process_prose` row. Reasoning and tool
events enter the same registry before their legacy renderer callbacks run, so
the projected `activity_scene_v1` owns the visible row order.

`renderLiveAnchorActivityScene()` consumes only that projected scene for the
active live turn. Once the live turn is anchor-owned, legacy live
`appendLiveToolCard()`, `appendThinking()`, auto-compression, and Worklog
reason-mirroring paths re-render or exit instead of creating a second activity
rail. On same-browser session switch, `loadSession()` tries the live anchor
scene before falling back to saved live DOM snapshots or persisted `INFLIGHT`
tool replay.

At settle time, the active final assistant message may receive the current
in-memory `_anchor_activity_scene`. `renderMessages()` uses that snapshot to
build a folded activity summary above the final answer and leaves the final
answer as ordinary assistant prose. Successful auto-compression rows remain
live-only in settled history unless they explain a visible error or recovery
state. The original live Compact Worklog handoff did not by itself cover
Transparent Stream or durable reload; those require explicit settled-scene
persistence or read-side hydration slices.

Settled mixed `content[]` assistant messages now bridge into the same
`activity_scene_v1` ownership. When the final assistant message interleaves text
parts with `tool_use` parts, settlement and read-side hydration promote those
ordered parts into anchor activity rows. The final answer remains the text after
the last tool use on the final assistant message, while earlier text, non-final
post-tool process text, thinking rows, and tool rows stay in chronological
activity order for Compact Worklog and Transparent Stream renderers. The
Transparent Stream raw `content[]` helper remains a fallback for settled
messages that do not yet carry `_anchor_activity_scene`.

### Settled Fallback Ownership Matrix

After the mixed `content[]` bridge, `_anchor_activity_scene` is the semantic
owner for settled assistant activity whenever it is present. Legacy raw-message
paths stay as compatibility fallbacks for older or non-anchor transcripts; they
must not compete with an anchor-owned turn.

| Surface | Anchor-present owner | Compatibility fallback | Fallback exit |
| --- | --- | --- | --- |
| Settled Compact Worklog activity | `_renderSettledAnchorSceneForMessage()` renders `activity_scene_v1` rows before the final answer. | Legacy `S.toolCalls`, `tool_calls`, `_partial_tool_calls`, and raw `content[].tool_use` rebuilds. | `anchorOwnedAssistantRawIdxs` excludes the anchor-owned turn from message metadata scans, fallback source collection, tool buckets, thinking buckets, and worklog-source mirroring. |
| Settled Transparent Stream activity | `_renderSettledAnchorSceneTransparentForMessage()` renders the same scene rows as transparent event rows or inline prose before the final answer segment. | `_transparentStreamOrderedParts()` rebuilds raw `content[]` order for historical messages without an anchor scene. | `_transparentStreamOrderedParts()` returns `null` when `message._anchor_activity_scene` exists, leaving the dedicated anchor renderer in charge. |
| Historical / non-anchor transcripts | No anchor owner is available. | Raw `content[]`, persisted `session.tool_calls`, role=`tool` rows, and partial tool-call snapshots continue to recover visible tool/prose history. | None. These paths remain until replay/runtime-journal coverage proves the same transcript shapes hydrate through anchors. |
| Live reattach / session switch | `renderLiveAnchorActivityScene()` consumes the projected live scene, and session switch first attempts runtime-journal anchor scene restore. | Saved live DOM snapshots and `INFLIGHT` tool replay. | Snapshot fallback only runs when no usable live anchor scene can be rendered. |

This matrix is an audit baseline, not permission to delete fallbacks. Fallback
removal should wait for replay/runtime-journal parity tests that prove all
supported settled transcript sources hydrate through anchors.

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
