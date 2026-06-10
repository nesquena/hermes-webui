# Stable Assistant Turn Anchors for Live-to-Final Rendering

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-06-10
- **Tracking issue:** [#3926](https://github.com/nesquena/hermes-webui/issues/3926)
- **Parent contract:** [Live-to-Final Assistant Replies](./live-to-final-assistant-replies.md) ([#3400](https://github.com/nesquena/hermes-webui/issues/3400))
- **Related RFCs:** [Transparent Stream](./transparent-stream-activity-mode.md), [Hermes Run Adapter Contract](./hermes-run-adapter-contract.md), [WebUI Run State Consistency Contract](./webui-run-state-consistency-contract.md), [Turn Journal](./turn-journal.md), [Pending Intent Controls](./webui-pending-intent-controls.md)

## Problem

The Live-to-Final redesign made the user-facing product model clearer:

- a live assistant turn shows ongoing work,
- supporting activity belongs in a Worklog,
- the final answer becomes the primary settled result,
- terminal states should be explicit when a normal final answer was not
  produced.

That product model should remain. The remaining problem is architectural: the
live assistant turn and the settled transcript are still represented by
different layers.

In the current browser path, live output is assembled from stream-local
variables, `INFLIGHT` snapshots, live DOM nodes, and SSE callbacks. When the
stream completes, the browser replaces or reconciles `S.messages` from the
server session and calls `renderMessages()`. The settled turn is then rebuilt
from transcript messages, tool metadata, reasoning metadata, artifact helpers,
and recovery helpers.

That means the UI has to cross this boundary at settle time:

```text
live stream state + DOM + INFLIGHT snapshot
  !=
settled transcript messages
```

This split has already required several narrow repairs around replay,
reconnect, session switching, blank recovered turns, thinking placeholders,
stream-end recovery, and mid-stream transcript rebuilds. Those fixes were
useful, but they point at a reusable missing primitive: a stable assistant-turn
anchor that exists before the final answer and can own live activity,
settlement, replay, recovery, and display-mode rendering.

The primary seam is rendering-side ownership. The recurring failures above are
not mainly identity or transport bugs — durable event identity and cursor-based
replay already exist (see the adapter Event Envelope under Background). They are
bugs about which layer *owns* a tool card, a thinking row, or a settled answer
when live state and settled transcript meet. The anchor is therefore first a
presentation ownership primitive, and only secondarily an identity or replay
one.

This is also the structural follow-through on the
[run-state consistency contract](./webui-run-state-consistency-contract.md).
That contract already enumerated the overlapping per-turn state layers and wrote
down invariants — ownership, ordered reattach, idempotent replay,
maintenance-is-not-activity — plus a reviewer checklist to keep those layers
coherent. A checklist keeps N layers from drifting; it does not reduce N. This
RFC operationalizes those invariants into one owning object, so they have a home
instead of being re-proven one fix at a time.

A risk follows directly from that goal. The anchor only pays off if it demotes
the other per-turn stores (`S.messages`, `INFLIGHT`, stream-local closure state,
live DOM) to caches and renderers. If it is added *beside* them without taking
ownership, it becomes an N+1th store and makes the seam worse, not better. Every
slice below should be judged on whether it moves ownership, not on whether it
adds a new structure.

This RFC proposes that primitive.

## Goals

- Define a frontend presentation/reconciliation model for one assistant turn.
- Create a stable assistant-turn anchor early enough that live events can attach
  to it before the final answer exists.
- Normalize live SSE events, run-journal replay, restored inflight state, and
  settled transcript metadata into one assistant-turn model.
- Let Compact Worklog and Transparent Stream render from the same model.
- Make stream `done` a settlement update to the existing turn, not a wholesale
  switch from one representation to another.
- Preserve the RuntimeAdapter boundary: WebUI owns presentation and browser
  reconciliation, not long-lived execution ownership.
- Give artifacts, side effects, usage metadata, and terminal states a stable
  owner even when they do not belong in the visible activity timeline.
- Provide a migration path that can land in small PRs without changing the
  visible UI first.
- Operationalize the run-state consistency contract's per-turn invariants by
  giving them one owning object instead of a per-fix checklist.
- Insulate the presentation layer from the in-progress RuntimeAdapter substrate
  migration (#1925): the anchor normalizes whatever identity and events the
  current substrate emits, so moving from the WebUI-owned run path to a runner or
  Hermes runtime changes the anchor's input source, not the UI.

## Non-goals

- Do not replace the Live-to-Final product model from #3400.
- Do not implement Transparent Stream in this RFC.
- Do not implement a RuntimeAdapter, runner process, sidecar, or Hermes runtime
  API.
- Do not require a backend schema migration before the frontend model can start.
- Do not redefine Queue, Steer, Stop-and-send, or Interrupt product semantics.
  Those belong to the pending-intent controls RFC. This RFC only gives their
  resulting visible boundaries a stable presentation owner when needed.
- Do not introduce transcript virtualization, rich artifact rendering, or a new
  tool-card visual design.
- Do not use visible text, timestamps, or fuzzy matching as the primary identity
  model for live/replay deduplication.

## Background And Evidence

### Parent product model

`live-to-final-assistant-replies.md` defines the product contract:

- long-running sessions are the anchor of the experience,
- process prose is the main live reading surface,
- Thinking, tools, lifecycle status, terminal outcomes, and artifacts are
  supporting activity,
- the final answer remains distinct from the live Worklog,
- replay/reconnect/session switch should rebuild the same lifecycle or show a
  clear degraded/restoring state.

This RFC does not redefine that product model. It defines a lower-level
presentation model that can make the product contract easier to preserve.

### Transparent Stream

`transparent-stream-activity-mode.md` defines an opt-in display mode where power
users can see chronological activity:

```text
progress -> Thinking -> tool -> result -> progress -> ...
```

Transparent Stream should not require a separate live engine. It should be a
different renderer over the same assistant-turn activity model that Compact
Worklog uses.

That RFC is already Accepted (#3862 / #3864) and specifies its own normalization
seam:

```text
SSE live ──┐
journal ───┼─► normalizeToEvents() ─► renderActivityEvent() ─► DOM
settled ───┘
```

This is the same seam this RFC calls `anchor.activity_events`, approached from
the renderer side. The two must not become two independent normalizers over the
same three paths. The reconciliation is explicit: Transparent Stream's
`normalizeToEvents()` is the renderer-facing view of the anchor's activity-event
model, and `renderActivityEvent()` is one of the anchor's renderers. Whichever
side ships first defines the shared event shape; the other consumes it. Because
#3820 is an active regression a maintainer wants restored soon, Transparent
Stream may land its `normalizeToEvents()` first — but it should be written as the
anchor's event normalizer (turn-owned, identity-keyed), not as a renderer-local
helper the anchor later has to reabsorb. See the Open Question on dependency
order.

### RuntimeAdapter

`hermes-run-adapter-contract.md` draws the runtime ownership boundary:

- WebUI should be thin in execution ownership.
- WebUI should not be thin in product scope.
- The adapter should be a protocol translator, not a runtime surrogate.

This RFC stays on the WebUI side of that boundary. The assistant-turn anchor is
a browser presentation and reconciliation primitive. It must be able to consume
current WebUI events and future adapter events, but it must not become a second
runtime.

Two facts from that contract matter for identity here. First, the migration is
already in progress, not hypothetical: the journal/replay layer, the adapter
seam, control routing, and default-off runner-selection/client seams have all
started landing. Second, the contract's **Artifact 1 Event Envelope**
already defines the durable event identity this anchor needs —
`event_id = "run_id:seq"`, monotonic `seq`, `run_id`, `Last-Event-ID` /
`after_seq` reconnect, and dedupe by `run_id + seq` or `event_id` — and the WebUI
run journal already emits exactly that envelope today. So the anchor does not
invent identity. It consumes the Artifact 1 envelope, which the current substrate
emits now and a runner or Hermes runtime will emit later. `run_id` is the durable
key; `stream_id` is the legacy transport key the contract already lets `run_id`
outlive.

### Current recurring symptoms

The following shipped or discussed fixes are not the same bug, but they expose
the same seam:

- #3401 / #3741 landed the main Live-to-Final redesign.
- #3707 / #3763 restored live tool cards on reconnect.
- #3869 / #3876 fixed stale thinking-dot placeholders.
- #3875 remains open, although v0.51.347 shipped recovered-turn /
  blank-transcript fixes under that issue; keep it as current evidence that
  recovery/replay and settled rendering share the same ownership seam.
- #3877 fixed mid-stream content flicker caused by `renderMessages()` detaching
  the live turn DOM node.
- #3885 hardened `stream_end` recovery so a transport close does not prematurely
  settle a still-active server run.
- #3820 / #3862 / #3864 accepted Transparent Stream as an opt-in display-mode
  split.

The common pattern is not just "the UI needs one more card." The common pattern
is that live, replay, and settled paths do not share a durable presentation
anchor.

## Current Architecture Shape

Today, the browser has several overlapping stores for one active turn:

| Layer | Current role | Problem for Live-to-Final |
| --- | --- | --- |
| `S.messages` | Canonical visible transcript for settled render | The live assistant reply is not inserted as a stable canonical anchor at turn start. |
| `INFLIGHT` | Browser snapshot for active work and recovery | Useful recovery cache, but not a settled transcript model. |
| stream-local closure state | Tracks `assistantText`, `reasoningText`, live tool cards, segment counters, and parser targets | Good for hot-path streaming, but cannot be the only ordering source. |
| live DOM | Holds current live assistant turn, Worklog rows, parser target, and transient cards | Fast for rendering, but fragile when `renderMessages()` rebuilds the transcript. |
| run journal | Durable emitted-event source for replay | Useful, but the frontend still needs a normalized presentation model over it. |
| settled session messages | Server-returned final transcript | Arrives late and currently forces a bridge from live representation to settled representation. |

The most fragile moment is terminal settlement:

1. the stream emits `done`,
2. the browser receives a settled session payload,
3. `S.messages` is replaced or reconciled from server truth,
4. `renderMessages()` rebuilds the transcript,
5. helper code tries to carry forward live-only fields, tool metadata, scroll
   state, Thinking metadata, and DOM continuity.

`renderMessages()` is not only a final-stage function. It is the current
transcript-pane renderer used for session load, session switch, user-message
echoes, command output, error/cancel recovery, and final settlement. During hot
streaming, token/reasoning/tool listeners mostly update live DOM incrementally
instead of calling it per token. But any mid-stream `renderMessages()` rebuild
can still wipe and recreate the message pane, which is why current code has
special protection for the live assistant DOM node.

This works increasingly well, but the complexity is a symptom: live rendering
and settled rendering still do not share a first-class turn object. The goal is
not to delete `renderMessages()`. The goal is to let it become a renderer over
stable presentation state, not the semantic boundary between live and settled
turns.

## Source Of Truth Layers

The assistant-turn anchor must sit in the correct layer:

| Layer | Authority |
| --- | --- |
| Server session and settled transcript | Durable final messages, final answer, persisted metadata, and cumulative usage. |
| Run journal / replayable events | Durable evidence of live stream events and their ordering, emitting the Artifact 1 envelope (`run_id:seq`). Distinct from the turn journal, which records submitted-turn lifecycle for crash recovery. |
| SSE live stream | Lowest-latency observation path for active work. |
| Assistant Turn Anchor | Frontend presentation/reconciliation owner for one assistant turn. |
| `INFLIGHT`, localStorage, and HTML snapshots | Browser recovery acceleration and crash tolerance only. |
| DOM | Disposable rendered output. |
| RuntimeAdapter | Future execution/event source boundary, not owned by this RFC. |

The anchor is not the durable truth by itself. It is the frontend object that
keeps durable truth, live observations, recovery cache, and renderer output from
disagreeing about which assistant turn they describe.

The DOM must not be treated as semantic truth. A live DOM node can be preserved
for continuity, but facts should come from SSE, journal events, settled
transcript payloads, and anchor state. `INFLIGHT` can speed up recovery, but it
must not outrank journal or settled transcript evidence.

## Proposal

Introduce a stable assistant-turn anchor as an internal WebUI presentation
primitive.

Conceptually:

```text
AssistantTurnAnchor
  identity
    session_id
    turn_id
    run_id
    stream_id
    source_message_refs
  lifecycle
    status
    terminal_state
    started_at
    completed_at
  content
    final_answer
    final_message_ref
  activity_events[]
    process_prose
    reasoning
    tool_started
    tool_updated
    tool_completed
    lifecycle_status
    control_boundary
    artifact_reference
    terminal_status
  artifacts[]
  side_effects[]
  usage
  presentation_state
    compact_worklog expansion state
    transparent_stream expansion state
    scroll/follow hints
```

The anchor is not a backend schema requirement. It is the frontend model that
lets the browser answer:

- Which assistant turn does this live event belong to?
- Where should this event appear?
- Has this event already been replayed?
- What is the final answer for this turn?
- What terminal state did this turn reach?
- Which artifacts, side effects, and usage metadata belong to this turn?
- Which display mode should render this same event sequence?

Current:

```text
live DOM != settled data model
```

Target:

```text
live events attach to an assistant-turn anchor
settlement updates that same anchor
renderers consume the anchor
```

In the target model, stream `done` should not need to "make a different turn."
It should complete the existing turn.

## Anchor Creation And Identity

The normal creation path is:

1. the user submits a message,
2. `/api/chat/start` succeeds,
3. the response returns a `stream_id`,
4. WebUI creates an assistant-turn anchor bound to the active `session_id`,
   submitted user turn, and `stream_id`.

This is earlier than the first assistant-owned SSE event. The first event might
be a token, interim progress, reasoning, a tool start, a compression lifecycle
row, or a control boundary. Waiting for one of those event types would make
early ordering and ownership conditional on transport timing.

The reconstruction path is separate. On reload, reconnect, session switch, or a
lost local anchor, WebUI may reconstruct an anchor from run journal events,
settled transcript messages, and `INFLIGHT` snapshots. That path should not be
described as the normal creation strategy.

The anchor consumes the adapter Event Envelope (Artifact 1 of
`hermes-run-adapter-contract.md`) as its identity source, not a new scheme. That
envelope — `event_id = "run_id:seq"`, monotonic `seq`, `run_id` — is already
emitted by the WebUI run journal today and will be emitted by a runner or Hermes
runtime later. A future durable `run.started` only upgrades the *source* of those
keys; the anchor's identity model does not change.

Identity preference:

1. `event_id` / `run_id + seq` from the Event Envelope (today: run journal;
   later: runner/runtime),
2. `turn_id` when a durable turn key is available,
3. `session_id + stream_id + local turn sequence` as the transport-level
   fallback, where `stream_id` is the legacy key `run_id` is expected to outlive,
4. settled assistant message reference when reconstructing a completed turn,
5. browser-local fallback ID when old data has no stronger identity.

The anchor must not hard-bind its identity to `stream_id` alone, because the
adapter migration replaces the WebUI-owned stream path while keeping the Event
Envelope stable. Visible text and timestamps are not identity sources. They can
be payload and diagnostic data, but they must not be the primary dedupe key.

## Live Attachment

Every live signal that belongs to the assistant turn should either append to the
anchor, update an existing anchor-owned record, or be explicitly classified as
metadata/side effect/transport state.

Examples:

- visible token or interim assistant prose appends `process_prose`,
- provider reasoning appends or updates `reasoning`,
- tool start/update/complete updates the matching tool event by `tid` or tool
  call ID when available,
- compression and restoring status appends `lifecycle_status` if user-visible,
- approval, clarify, steer, interrupt, or leftover delivery can append
  `control_boundary`,
- produced files or durable output references attach as artifacts,
- usage, title, context status, and side-panel snapshots update metadata rather
  than the readable activity timeline by default.

The DOM becomes a rendering target for the anchor, not the source of truth for
the event order.

## Activity Event Model

The normalized event model should be small and stable. It does not need to
mirror every backend field, but it must keep a source payload or source metadata
for debugging and future migration.

```json
{
  "event_id": "optional stable event id",
  "local_id": "browser fallback id",
  "session_id": "session id",
  "turn_id": "assistant turn id",
  "run_id": "optional runtime run id",
  "stream_id": "optional stream id",
  "seq": 12,
  "kind": "tool_started",
  "source_event_type": "tool",
  "created_at": 1778750000.0,
  "status": "running",
  "payload": {}
}
```

Required semantics:

- `event_id` or `run_id + seq` is the preferred dedupe key when available.
- `local_id` may exist only as a browser fallback.
- `turn_id` must tie the event to one assistant turn.
- `kind` controls rendering strategy, not runtime execution.
- `source_event_type` preserves the transport or derivation source.
- `payload` must be sanitized before rendering.

### Event kinds

| Kind | Meaning | Default Compact Worklog rendering | Transparent Stream rendering |
| --- | --- | --- | --- |
| `process_prose` | User-visible assistant progress text | Main Worklog prose item | Chronological transcript item |
| `reasoning` | Provider reasoning/thinking payload | Collapsed Thinking card | Chronological Thinking event |
| `tool_started` | Tool call began | Tool card or grouped tool row | First-class tool row |
| `tool_updated` | Tool output/progress updated | Update existing card/group | Update same tool row |
| `tool_completed` | Tool call finished | Finalize card/group | Finalize same tool row |
| `lifecycle_status` | Compression, reconnect, restoring, degraded, warnings, etc. | Quiet lifecycle row if user-visible | Chronological lifecycle row |
| `control_boundary` | Stop, interrupt, queue/steer, clarify/approval, or continuation boundary | Dedicated control/status row if visible | Chronological control row |
| `artifact_reference` | Produced file, workspace mutation, saved output, or handoff reference | Artifact/reference entry when implemented | Chronological artifact event |
| `terminal_status` | Completed, cancelled, interrupted, no-response, limit, connection lost, or error | Terminal card or final status | Chronological terminal row |

### Current source mapping

Current event names should map into the normalized model instead of becoming the
long-term model themselves.

| Current source | Anchor ownership |
| --- | --- |
| `token` | `process_prose` activity event. |
| `interim_assistant` | `process_prose` activity event, often with an activity boundary. |
| `reasoning` | `reasoning` activity event. |
| `tool` with `event_type=tool.started` | `tool_started` activity event. |
| `tool_complete` | `tool_completed` activity event. |
| future partial tool output | `tool_updated` activity event when a stable source exists. |
| `compressing` | `lifecycle_status` activity event while live. |
| `compressed` | `lifecycle_status` activity event while live; settled rendering may drop or fold it. |
| `approval` | `control_boundary` when visible in the turn; approval UI remains a control surface. |
| `clarify` | `control_boundary` when visible in the turn; clarify UI remains a control surface. |
| `pending_steer_leftover` | `control_boundary` and/or next-turn queue metadata. |
| `goal_continue` | `control_boundary` and next-turn queue metadata when it schedules a continuation. |
| `done` | Settlement trigger plus `terminal_status` / usage / final transcript merge. |
| `stream_end` | Transport close and recovery trigger; not equivalent to `completed`. |
| `cancel` | `terminal_status` with `cancelled` or `interrupted` semantics. |
| `error` / `apperror` | `terminal_status` with error metadata. |
| `warning` | `lifecycle_status` if user-visible, otherwise diagnostic metadata. |
| `state_saved` | `artifact_reference` or side effect depending on whether it is user-meaningful durable output. |
| `bg_task_complete` | session/background-task side effect; may become `control_boundary` only when it starts or explains a visible assistant turn. |

### Current non-activity sources

Not every source event should become a readable activity event.

| Current source | Default classification |
| --- | --- |
| `metering` | Usage/live-throughput metadata on the anchor or session. |
| `todo_state` | Side-panel state snapshot owned by session/turn; not a Worklog event by default. |
| `title` | Session metadata. |
| `title_status` | Diagnostic/session metadata. |
| `context_status` | Composer/session context metadata. |
| `goal` | Composer/status metadata unless it creates a visible turn boundary. |
| session list refreshes | Session metadata, not assistant activity. |
| live DOM snapshots | Recovery cache, not source truth. |

This classification is part of the model. Adding a new SSE or replay event
should require classifying it as activity, artifact, side effect, metadata,
transport, or explicitly excluded from turn ownership.

### Identity rules

Identity is the adapter Event Envelope applied at event granularity. Current
code already emits run-journal `event_id` values, SSE `id:` frames, and
`after_seq` / `after_event_id` replay cursors, and it also has scoped
`(session_id, event_id)` TTL dedupe for `bg_task_complete` dual-delivery. The
anchor should build on those existing identity/cursor surfaces instead of
inventing a second, renderer-local dedupe model.

Identity must be conservative:

- Prefer stable event IDs from run journal or runtime events.
- Prefer `run_id + seq` when both are available.
- Prefer `tool_call_id` / `tid` for updates to the same tool card.
- Do not dedupe by visible text alone.
- Do not dedupe by timestamp alone.
- Do not use fuzzy or semantic similarity to drop events.
- If identity is missing, prefer appending a clearly-owned event over dropping a
  potentially real repeated event.

This matters because real agents can emit repeated text, repeated tool calls,
or repeated status messages. A robust presentation layer should not treat that
as accidental duplication unless identity proves it.

## Settlement

Settlement is the point where the running assistant turn gains durable final
truth. It is not a second assistant turn.

When `done` arrives, WebUI should reconcile the final session payload into the
existing anchor:

1. verify that `session_id`, `run_id`, or `stream_id` still belongs to the
   anchor,
2. write the final assistant answer into `anchor.final_answer` or bind
   `anchor.final_message_ref`,
3. attach terminal state and usage metadata,
4. merge settled reasoning/tool/artifact metadata into existing activity events,
5. mark live-only events as settled or drop them if they were purely transient,
6. render the selected display mode from the anchor.

`renderMessages()` may still perform a full transcript rebuild in early
implementation phases. The important semantic change is that the rebuild should
render from an anchor that already owns the live activity and settled answer.
`done` should not depend on creating a separate settled-only Worklog to explain
the same turn.

Terminal state is separate from final answer text. A turn can have partial
process prose, tool cards, reasoning, artifacts, or usage metadata and still
fail to produce a normal final answer.

## Replay, Reload, And Reconstruction

Replay/reload should reconstruct the same Assistant Turn Anchor from durable
evidence, not create a separate replay-only UI path.

Source priority:

1. run journal / replay events for live activity ordering,
2. settled transcript for final answer and persisted message metadata,
3. `INFLIGHT` / local recovery snapshots for fast local recovery and fallback.

Conceptual reconstruction steps:

1. determine whether the session has an active or recently terminal stream,
2. find an existing anchor by `session_id` and stream/turn identity or create a
   reconstruction anchor,
3. replay durable events into the anchor,
4. merge settled transcript metadata,
5. fill compatible gaps from `INFLIGHT`,
6. render Compact Worklog or Transparent Stream from the reconstructed anchor,
7. show `restoring` or `degraded` when evidence is incomplete.

Reconstruction is not the normal creation path. The normal path creates an
anchor after `/api/chat/start` succeeds and returns `stream_id`. Reconstruction
is for reload, reconnect, session switch, SSE replay, and recovery cases where
the local anchor is missing or incomplete.

`stream_end` deserves special care. It is a transport close signal and may
trigger recovery. It must not be treated as proof that the turn completed.

## Rendering Strategies

The anchor separates event storage from display strategy:

```text
AssistantTurnAnchor
  -> Compact Worklog renderer
  -> Transparent Stream renderer
```

### Compact Worklog

Compact Worklog remains the default WebUI mode.

Expected behavior:

- process prose is the main live reading surface,
- Thinking and tools are supporting Worklog items,
- tool runs may be grouped and summarized,
- settled Worklog details are folded by default or follow user preference,
- live-only lifecycle rows do not pollute the final answer,
- final answer stays visually primary.

### Transparent Stream

Transparent Stream is opt-in.

Expected behavior:

- process prose, Thinking, tools, results, lifecycle rows, control boundaries,
  and terminal rows are chronological first-class activity events,
- tool calls are not hidden behind aggregate summaries,
- final answer remains distinct from the execution trace,
- live, settled, reload, and replay use the same event order.

Transparent Stream does not replace Compact Worklog. It is another projection of
the same anchor and activity events.

### Shared requirements

Both renderers must preserve:

- assistant turn ownership,
- event order,
- terminal honesty,
- replay idempotency,
- final answer separation,
- user expansion state where practical,
- stable behavior after reload or session switch.

Switching display modes should not trigger backend replay, rerun the agent, or
change which facts belong to the turn.

## Terminal States

The anchor should explicitly carry terminal state:

| Terminal state | Meaning |
| --- | --- |
| `completed` | A final assistant answer was produced and settled normally. |
| `cancelled` | The user stopped the turn. |
| `interrupted` | Runtime/control flow interrupted the turn before normal completion. |
| `no_response` | No usable assistant final content was produced. |
| `tool_limit_reached` | A tool/retry/iteration ceiling ended the turn. |
| `compression_exhausted` | Compression could not make enough room to continue safely. |
| `connection_lost` | Browser transport was lost and WebUI cannot confirm settled state. |
| `degraded` | WebUI has partial evidence but cannot fully reconstruct the turn. |
| `error` | Provider, backend, or fallback failure state. |

Terminal state is separate from final answer text. A `tool_limit_reached` turn
must not be represented by rendering a synthetic control prompt as a user
message. A cancelled turn is not the same as a provider error. A `stream_end`
frame is not the same as `completed`.

Compact Worklog may show terminal state as a status card or final-answer
replacement when no final answer exists. Transparent Stream should show the same
terminal truth as the final chronological event. The two display modes must not
disagree about the outcome.

## Artifacts And Side Effects

Agent turns produce more than prose and tool rows. They can create files, mutate
the workspace, save durable state, update side panels, change usage metadata, or
schedule a continuation. Those outcomes need ownership even when they should not
all appear in the readable activity timeline.

The principle:

> Not every side effect is an activity event, but every side effect must have an
> Assistant Turn Anchor owner or a deliberate session-level owner.

### Artifact and output references

Generated files, workspace mutations, exports, screenshots, reports, saved
state references, and handoff outputs should attach to the assistant turn that
produced them when that ownership is known.

Compact Worklog can summarize these as artifact/reference rows. Transparent
Stream can show them chronologically. If rich artifact metadata is missing, the
renderer should degrade to a link, file path reference, or workspace reference
instead of blank output.

### Side-panel and session state

Some events are important but are not assistant activity:

- `todo_state` updates the Todos panel and should remain a side-panel state
  snapshot by default,
- `metering` updates usage and live throughput,
- `title` updates session metadata,
- `context_status` updates composer/context state.

These should not be shoved into Worklog just to prove completeness. They should
be owned as side effects or metadata and restored through their natural UI
surface.

### Control and safety boundaries

Approval, clarify, steer, interrupt, Stop-and-send, and continuation delivery
can affect how the user interprets the turn. When visible, they may attach as
`control_boundary` events. The pending-intent controls RFC still owns what
those user intents mean; this RFC owns where their visible boundaries attach,
settle, and replay.

## Relationship To RuntimeAdapter

The assistant-turn anchor is not a runner, adapter, or execution-state owner.

RuntimeAdapter answers:

> Who owns active execution, controls, durable runtime status, and replayable
> runtime events?

This RFC answers:

> Once WebUI observes or derives assistant activity, how does the browser attach
> it to one assistant turn, render it, settle it, replay it, and switch display
> modes without changing the product model?

Future adapter-backed events should be easier to consume if this anchor exists.
The anchor should therefore align with the adapter event vocabulary where it is
already clear: run IDs, event IDs, sequence numbers, token/progress/reasoning,
tool lifecycle, controls, usage, errors, and done.

Adapter events can strengthen identity, but they do not replace the anchor. The
anchor must also not become runtime ownership. It cannot decide whether the
agent continues running, cannot become approval/cancel authority, and cannot
invent runtime state. It only expresses facts WebUI has observed, derived, can
show, and can reconstruct.

This insulation is a positive reason to build the anchor now, not just a boundary
to respect. The substrate under live rendering is actively migrating through
default-off adapter and runner seams. If the presentation layer keeps reading the
substrate directly, every migration step risks another live/settled repair. If it
reads the anchor, and the anchor reads the Artifact 1 envelope, then switching
from the WebUI run path to a runner or Hermes runtime changes the anchor's input
source and leaves the renderers untouched. The anchor is the buffer that lets
#1925 land under the UI without rewriting it.

## Rollout Plan

These phases are sequencing constraints, not a required one-phase-per-PR plan.
Implementation PRs may combine adjacent low-risk phases when they preserve
behavior and include coverage. Settlement, reconstruction, and display-mode
changes should remain independently reviewable.

### Phase 0: RFC and inventory

- Land this RFC as design guidance.
- Inventory current SSE event shapes consumed by `static/messages.js`.
- Inventory settled message metadata consumed by `static/ui.js`.
- Inventory run journal replay data and the existing `event_id` / `seq` envelope
  (distinct from the turn journal's submitted-turn lifecycle records).
- Classify every source event as activity, artifact, side effect, metadata,
  transport, or excluded.

### Phase 1: Internal anchor scaffold

- Add an internal assistant-turn anchor registry for the active session.
- Create anchors after `/api/chat/start` succeeds and returns `stream_id`.
- Hydrate anchors from current stream/session signals.
- Keep the visible UI unchanged.
- Add tests for identity, ownership, stale stream ignore, and no duplicate
  anchor on reconnect.

### Phase 2: Normalize activity events

- Add a normalization helper for live events.
- Add a derivation helper for settled message metadata.
- Add replay hydration into the same anchor model.
- Keep Compact Worklog as the only visible renderer.
- Add coverage proving all current event names have an explicit classification.

### Phase 3: Settlement through anchor

- Reconcile `done` into the existing anchor.
- Preserve final answer, reasoning, tool metadata, terminal state, and usage on
  the anchor.
- Reduce dependence on full transcript rebuild as the semantic boundary for the
  active turn.
- Keep a fallback full render path for safety.

### Phase 4: Replay/reload reconstruction

- Rebuild anchors from run journal, settled transcript, and `INFLIGHT`.
- Make session switch, hard refresh, and reconnect converge on the same turn
  model.
- Show `restoring` or `degraded` instead of an empty running shell when evidence
  is incomplete.

### Phase 5: Shared render strategy seam

- Render Compact Worklog from normalized activity events.
- Render Transparent Stream from the same activity events.
- Ensure live, settled, reload, and replay paths use the same event order.

### Phase 6: Artifacts and side effects

- Attach artifact references to the anchor.
- Give `state_saved`, workspace mutation, todo, and usage metadata deliberate
  ownership.
- Harden terminal states that currently require special-case rendering.
- Consider performance work for very long transparent streams only after the
  anchor/event model is stable.

## Acceptance Criteria

This RFC direction should be considered ready for implementation when it
defines:

- when an assistant-turn anchor is created,
- how it is identified across send, live stream, replay, session switch, and
  settlement,
- which event kinds attach to it,
- how live SSE, run journal replay, settled transcript messages, and `INFLIGHT`
  recovery map into the event model,
- how side effects and metadata are owned without polluting Worklog,
- how `done` reconciles final content and terminal state into the same anchor,
- how Compact Worklog and Transparent Stream render from the same events,
- how event dedupe works without relying on visible text or timestamps,
- how degraded/restoring states appear when reconstruction is incomplete,
- how this model stays within the RuntimeAdapter boundary,
- which implementation slices are small enough to review independently.

An implementation should eventually satisfy:

- the same assistant turn has the same ownership across live, settled, reload,
  session switch, and reconnect,
- `done` updates an existing turn instead of creating a separate settled-only
  turn,
- Compact Worklog and Transparent Stream read the same normalized input,
- final answer and terminal state are separate,
- live-only lifecycle rows do not pollute settled final answer,
- tool/reasoning/control/artifact ownership does not depend on DOM survival,
- incomplete reconstruction shows `restoring` or `degraded`, not an empty
  running shell,
- every new event is classified as activity, artifact, side effect, metadata,
  transport, or explicitly excluded,
- replay/reconnect of one run is idempotent at the existing
  `(session_id, event_id)` dedupe ring: no activity event is dropped or
  duplicated when the same run is observed live and then replayed.

## Review Checklist For Implementation PRs

Any implementation PR against this RFC should answer:

- Which state layer does this PR mutate: event source, anchor registry,
  activity event list, `S.messages`, `INFLIGHT`, live DOM, run journal, settled
  session metadata, side panel, or renderer?
- What is the source of truth after the change?
- Does the PR introduce or consume a new event? If so, how is it classified?
- Should that event enter activity timeline, artifact ownership, side effect
  ownership, metadata, transport handling, or explicit exclusion?
- Can reload/reconnect/session switch rebuild the same assistant turn?
- Can replay duplicate or drop a real event?
- Can a stale stream update a newer visible turn?
- Does the change rely on DOM presence, visible text, timestamp, or fuzzy
  similarity as semantic truth?
- Does `done` merge final answer, usage, and terminal state without duplicating
  Worklog/tool rows?
- Do Compact Worklog and Transparent Stream still agree on final answer,
  terminal state, tool results, and control boundaries?
- Do artifacts and side effects have an owner even when they do not appear in
  Worklog?
- Does the change affect RuntimeAdapter ownership?
- What test or manual invariant proves the behavior?

## Open Questions

### First implementation slice size

Should the first implementation PR stop at hidden anchor scaffolding, or should
it also normalize a narrow event subset?

Default recommendation: combine hidden scaffold with narrow normalization only
when the default UI is unchanged and tests can prove equivalence.

### Event identity fallback depth

When journal event IDs, runtime IDs, sequence numbers, and tool IDs are missing,
how far should browser-local fallback identity go?

Default recommendation: use local monotonic sequence within a clearly owned
anchor. Do not use visible text or timestamp fuzzy dedupe. If identity is weak,
prefer appending over deleting a potentially real repeated event.

### Incremental settlement depth

Should the first settlement implementation fully avoid `renderMessages()` whole
rebuilds?

Default recommendation: no. First make settlement update the existing anchor
semantically, while allowing `renderMessages()` to remain the DOM renderer.
Later slices can reduce active-turn rebuilds.

### Artifact ownership granularity

Which side effects should attach to the assistant turn versus the session?

Default recommendation: generated output and workspace mutations attach to the
turn when known. Title, context status, and broad usage metadata remain
session-level unless a clear turn-level ownership signal exists.

### Transparent Stream dependency order

Should Transparent Stream wait for the full anchor model?

This is now a reconciliation, not an open choice: the Transparent Stream RFC's
`normalizeToEvents()` and this RFC's `anchor.activity_events` are the same
normalization seam over the same three paths (live, journal replay, settled).
They must not diverge into two normalizers.

Default recommendation: because #3820 is an active regression, Transparent Stream
may ship its normalization first — but it must be written as the anchor's event
normalizer (turn-owned, keyed by the Artifact 1 envelope), so the anchor later
*adopts* it rather than reabsorbing a renderer-local helper. Whichever side lands
first owns the shared event shape; the other consumes it. A second independent
normalizer is the failure mode to avoid.

## Out Of Scope But Related

- #3820 Transparent Stream implementation.
- #3058 / pending-intent control semantics for Queue, Steer, Stop-and-send, and
  Interrupt.
- #1925 RuntimeAdapter implementation.
- Full transcript virtualization for very long streams.
- New tool-card visual design.
- Rich artifact browser, preview, or attachment redesign.
- Backend schema migration for persisted `turn_id`, `run_id`, or normalized
  events.
- Model prompting or visible-progress prompt policy.
