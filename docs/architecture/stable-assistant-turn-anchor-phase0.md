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
