# Todo State Contract: Realtime Todos Panel

- **Status:** Implemented (PR #3065)
- **Modules:** `api/todo_state.py`, `api/streaming.py`, `api/routes.py`, `static/ui.js`, `static/messages.js`, `static/sessions.js`, `static/panels.js`

## Purpose

The Todos panel mirrors the agent's `todo` tool state into the browser. This
document pins the wire format, the two delivery channels, the reconciliation
rule the frontend uses to pick a snapshot, and how the contract sits alongside
the existing run-journal / partial-output recovery machinery.

It is descriptive of shipped behavior, not a proposal. If the implementation and
this document disagree, that is a bug in one of them — fix both together.

## Ownership: the agent writes, the WebUI reflects

There is no parallel todo store in the WebUI. The source of truth lives in the
agent process:

- `tools/todo_tool.py` defines `TodoStore` — a per-`AIAgent`, in-memory list of
  `{id, content, status}` items.
- `run_agent.py:_hydrate_todo_store()` rebuilds that store after a context reset
  by reverse-scanning history for the most recent `role='tool'` message whose
  JSON content carries a `todos` list.

The WebUI pipeline is a **mirror** of that store, derived from the same signal
(`role='tool'` + JSON `todos` list). `api/todo_state.derive_todo_state()` uses
the same detector as the agent's `_hydrate_todo_store()`, and the module
docstring pins that symmetry so a future change to one detector has to land in
both. The WebUI never invents or mutates todo state — it only reflects what the
agent already wrote.

## Wire format

Both channels normalize through `_normalize_snapshot()` so the frontend has a
single decoder. `VERSION = 1` is reserved for future non-additive changes.

### Cold-load (session GET payload)

`GET /api/session` attaches the derived snapshot under the `todo_state` key when
`load_messages` is set:

```json
{
  "todos":   [{"id": "1", "content": "...", "status": "in_progress"}, ...],
  "summary": {"total": 5, "pending": 2, "in_progress": 1,
              "completed": 2, "cancelled": 0},
  "version": 1,
  "ts": 1780122043.53
}
```

### Live (SSE event)

`api/streaming.py` emits a dedicated `todo_state` SSE event whenever the `todo`
tool completes, from both the legacy `tool_progress_callback`
(`event_type='tool.completed'`) and the modern `on_tool_complete` path. The live
payload adds routing/ordering metadata on top of the snapshot:

```json
{
  "session_id": "...",
  "stream_id":  "...",
  "source":     "tool",
  "ts":         1780122043.53,
  "todos":      [...],
  "summary":    {...},
  "version":    1
}
```

`EVENT_NAME` and `PAYLOAD_KEY` are defined once in `api/todo_state.py` so a
rename stays single-source and grep-able from the frontend.

### The `ts` field — recency axis

`ts` is the timestamp of the source `tool` message. It exists so the frontend
can compare the cold-load snapshot (server's settled view) against an INFLIGHT
snapshot (locally persisted, possibly fresher) on the same axis.

**Recency floor.** A context compression/rebuild can strip the `timestamp` off a
settled message (it ends up `None` on disk). `derive_todo_state` still finds the
correct latest todos by position, but if it emitted a snapshot with no `ts`, the
frontend would read `coldTs = 0` and a stale-but-timestamped INFLIGHT snapshot
would win the recency comparison — rendering a *historical* todo list. To
prevent that, when the latest todo message has no usable timestamp,
`derive_todo_state` floors `ts` to the maximum valid timestamp at or before that
message's position. This guarantees the latest-by-position snapshot can never
lose recency to an earlier todo write. The floor scans only up to the todo's
position, so it never borrows a timestamp from a later message.

## Frontend state model

`static/ui.js` holds two globals as the single source of truth:

- `S.todos` — the current snapshot's `todos` array.
- `S.todoStateMeta` — `{ts, source, version}`, or `null`.

`null` is a **sentinel**, distinct from "signal seen, list is empty". When
`S.todoStateMeta` is `null`, `loadTodos()` falls through to
`_legacyTodosFromMessages()` (reverse-scan over tool messages in `S.messages`).
This keeps the panel working against pre-Phase-1 backends that emit no
`todo_state`, and during the upgrade window.

### Three feed channels

`_hydrateTodosFromSession()` runs at every `S.session =` settle point in
`messages.js` (3 sites) and `sessions.js` (5 sites, including delete-session
paths that pass `null` to clear). It reconciles three inputs:

1. **`todo_state` SSE event** (live) — listener in `messages.js`. Full-snapshot
   replace, never merge. Dropped if `payload.session_id !== activeSid` or
   `S.session.session_id !== activeSid` (cross-session double gate), or if
   `incomingTs < currentTs` (strictly-older drop; equal-ts allowed so a
   compression-source refresh can land on the same wall-clock second).
2. **session GET `.todo_state`** (cold-load) — the server's settled view.
3. **`INFLIGHT[sid]` snapshot** (reload recovery) — persisted into the
   `localStorage` snapshot and restored on tab return / hard reload so a
   mid-stream reload does not flicker the panel to empty.

### Reconciliation rule (cold-load vs INFLIGHT)

When both a cold-load snapshot and an INFLIGHT snapshot are present:

| Condition | Winner | Rationale |
|---|---|---|
| `coldTs === 0` (cold-load carries no usable ts) | **cold-load** | The cold-load is the server's authoritative latest-by-position view; a missing ts means the source message lost its timestamp to compression, not that the snapshot is old. |
| `coldTs > inflightTs` | **cold-load** | Server view is strictly newer. |
| otherwise (cold-load older, or ts tie) | **INFLIGHT** | Do not regress a still-running stream with a stale cached cold-load; on a tie prefer the freshest in-tab edits. |

When `coldTs === 0` and the stream is still live, the next `todo_state` SSE event
reconciles forward — its drop guard is `incomingTs && currentTs && incomingTs <
currentTs`, and with `currentTs = 0` that guard short-circuits to false, so the
event is always applied. The transient is self-healing.

## Render path

Two cheap stages in `static/panels.js`:

- `scheduleTodosRefresh()` — RAF-coalesces bursty live updates into one paint per
  frame; skips entirely when the panel is not active (`_todosPanelIsActive()`).
- `loadTodos()` — prefers `S.todos` when `S.todoStateMeta` is set; otherwise
  falls through to `_legacyTodosFromMessages()`.

A content-keyed hash (`_todosHash`) plus `_todosLastRenderedHash` short-circuits
identical re-renders, including the empty-state case. All user-controlled
strings go through `esc()` before `innerHTML`.

## Interaction with run-journal / partial-output recovery

The realtime Todos panel is layered on top of the existing recovery machinery;
it does not change it.

- **Run-journal whitelist.** `todo_state` is added to the SSE run-journal cursor
  whitelist so a reconnect's `Last-Event-ID` advances past prior snapshots
  instead of replaying every one. Replay remains correct because snapshots are
  full and idempotent — re-applying the same snapshot is a no-op (hash
  short-circuit). The whitelist entry just avoids pointless re-render work.
- **INFLIGHT persistence.** The todo snapshot rides inside the existing INFLIGHT
  `localStorage` bucket (`_compactInflightState`), under the same LRU budget and
  staleness eviction (>10 min) as the rest of the recovery tail. It is not a new
  persistence layer.
- **Stream identity.** `loadInflightState` returns `null` when the reconnecting
  stream has a different `stream_id`, so a new run cannot inherit a stale todo
  snapshot from a previous one — the same guard the rest of recovery uses.
- **Cold-load on reopen.** Opening any session from the sidebar hydrates the
  panel from `attach_todo_state` without waiting for a new tool call, mirroring
  how the agent's `_hydrate_todo_store` rebuilds its store on context reset.

## Error isolation

Every boundary swallows-and-degrades so todo mirroring never breaks tool
delivery or the session GET response:

| Boundary | Behavior |
|---|---|
| `parse_todo_tool_result` gets non-string / non-JSON / wrong shape | returns `None`; emit and attach skip |
| `emit_todo_state` raises in `put` | swallowed, debug-logged, returns `False`; tool delivery unaffected |
| `attach_todo_state` errors during message iteration | swallowed, returns `False`; session GET responds normally without the field |
| frontend `JSON.parse` failure | listener returns; `S.todos` unchanged |
| `localStorage` corrupted | `_readInflightStateMap` returns `{}`; next save rebuilds |
| `localStorage` quota exceeded | falls back to active-session-only; if still over, clears the bucket |

## Out of scope

- No upstream Hermes Agent protocol change; `todo` tool semantics are unchanged.
- No SSE infrastructure change beyond one new event name plus one journal
  whitelist entry.
- No persistence-layer change (still `localStorage`, not IndexedDB).
