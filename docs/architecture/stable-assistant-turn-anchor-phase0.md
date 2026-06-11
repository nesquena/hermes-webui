# Stable Assistant Turn Anchors Phase 0 Inventory

This inventory implements the first non-visual slice of
[`stable-assistant-turn-anchors.md`](../rfcs/stable-assistant-turn-anchors.md).
It documents the current per-turn state layers and the event-shape contract that
future anchor phases must consume. It does not claim that anchors are wired into
streaming or rendering yet.

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
settled assistant message into `content.final_answer`. It rejects mismatched
session or turn identity and skips duplicate live + replay observations by the
same dedupe key.

This slice deliberately keeps the ownership boundary inert: `send()`,
`attachLiveStream()`, replay hydration, `renderMessages()`, `S.messages`,
`INFLIGHT`, and DOM continuity still do not consume the registry. Later slices
can replace local renderer-owned state with this owner instead of adding another
parallel source of truth.

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
3. `session_id + local_id` as a browser fallback

This mirrors the RuntimeAdapter Event Envelope and keeps the browser aligned
with run-journal replay while the anchor registry is still unwired.
