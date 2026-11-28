# SSE streams and capability signaling

Cross-client reference for the server-sent events (SSE) endpoints Hermes WebUI
exposes. Browser and non-browser clients (Android wrapper, CLI observers)
should integrate against this page so every client describes the same
behavior.

All endpoints below are served by the WebUI origin and sit behind the same
authentication as every other `/api/*` route: when a WebUI password or OIDC
is configured, clients must authenticate before opening any stream.

## Endpoint inventory

| Endpoint | Availability | Purpose |
|---|---|---|
| `GET /api/chat/stream?stream_id=<id>` | Always on | Live agent-turn relay (tokens, tool calls, approvals, `done`, `stream_end`). Falls back to run-journal replay when the in-memory stream is gone. Resume cursors: `after_event_id` / `after_seq` query params, with the standard `Last-Event-ID` header as fallback (events carry `id: <stream_id>:<seq>`). An invalid/foreign/ahead-of-stream cursor is honored as replay-from-start rather than silently skipping events. |
| `GET /api/session/stream?session_id=<id>` | Always on | Persistent per-session channel that survives across agent turns (`initial`, `server_turn_started`, `session-updated`, `bg_task_complete`). This is the stream the WebUI frontend keeps open per session and the stream non-browser clients should prefer for background session updates. |
| `GET /api/sessions/events` | Always on | Global session-list invalidation (`sessions_changed` + keepalives). A signal to re-read `/api/sessions`, not a per-session lifecycle feed. |
| `GET /api/sessions/{session_id}/events` | Always on | Per-session run-journal relay with `Last-Event-ID` / `after_event_id` resume and snapshot fallback. See `docs/rfcs/session-sse-contract-v1.md` for the contract and its proof gates. |
| `GET /api/sessions/gateway/stream` | Optional | Real-time updates for CLI/TUI/messaging (agent) sessions merged into the sidebar. Only streams when the **Agent sessions** setting (`show_cli_sessions`) is enabled and the gateway watcher thread is running. |

The authoritative `event:` names on `/api/chat/stream` are listed in the
**Authoritative emitted events** table of
[`docs/rfcs/session-sse-contract-v1.md`](rfcs/session-sse-contract-v1.md).

## Gateway probe scope (important for non-browser clients)

`GET /api/sessions/gateway/stream?probe=1` returns a JSON capability payload
for the **optional gateway stream only** instead of holding an SSE connection:

```json
{
  "enabled": false,
  "ok": false,
  "watcher_running": false,
  "fallback_poll_ms": 30000,
  "error": "agent sessions not enabled",
  "scope": "gateway_sessions",
  "session_stream_available": true,
  "session_stream_path": "/api/session/stream"
}
```

- `404` + `error: "agent sessions not enabled"` means only that the optional
  gateway/agent-sessions stream is disabled on this server.
- `503` + `error: "watcher not started"` means the setting is on but the
  gateway watcher thread is not running.
- `200` with `ok: true` means gateway SSE is usable.

**A negative gateway probe result must not be treated as "SSE unavailable".**
The persistent per-session stream (`/api/session/stream`) and the chat-turn
relay (`/api/chat/stream`) are always on and are not gated by the Agent
sessions setting. The `scope`, `session_stream_available`, and
`session_stream_path` fields make this explicit so clients do not need to
infer it from the status code. Clients that only need session updates should
use `/api/session/stream` directly; the gateway probe is only relevant for
clients that display CLI/TUI/messaging sessions.

## Gateway watcher projection lifecycle

The optional gateway stream's watcher owns a cheap database fingerprint and a
timestamp for its last full session projection. While clients are subscribed,
an unchanged fingerprint can skip the expensive read until the bounded parity
interval. With no subscribers the poll loop parks instead of reading the DB.
When the final subscriber leaves (including eviction of a full or failed
consumer queue), the watcher invalidates both cache fields. A reconnect must
therefore trigger a fresh projection even when the database fingerprint has not
changed and the parity interval has not elapsed. Final removal also advances a
subscriber epoch: an older in-flight DB read cannot restore the invalidated
cache or publish its result to a later subscriber cohort. The subscriber lock
protects removal, epoch checks and cache commits, but is not held during DB
reads, so disconnect is not blocked by a slow projection.

## Reasoning settlement

When a `/api/chat/stream` turn settles, `_settle_turn_reasoning()` in
`api/streaming.py` persists a reasoning trace on each of the turn's new
assistant messages before the session is saved. Assistant messages from
earlier turns are never modified.

- **Non-empty agent `reasoning` wins.** The agent writes the key on every
  assistant message it builds, but `None` only means the provider did not
  return final reasoning. It is not evidence the step had no thinking, so
  stream-only providers fall through to the stream.
- **Stream segments are bound to the step that owns them.** When a tool
  starts, `on_tool_start()` binds its `tool_call_id` to the segment streamed
  since the previous tool, or to `None` if that step streamed no thinking.
  Each start is bound separately: an id repeated across steps (agents derive
  ids deterministically when providers omit them) keeps one binding per start,
  and settlement consumes them in step order.
  A visible interim message (`on_interim_assistant()`) also closes its step:
  the open segment is bound to that message's text, so the next tool call
  cannot claim it. Settlement resolves a tool-call step through its call ids:
  from `tool_calls`, else from the following tool results' `tool_call_id`,
  else from the next unclaimed live tool starts in order. A step with
  commentary can also resolve through its interim binding. Other
  steps are matched to interim bindings by their content, in order. The final
  step takes the segment still open at settlement. The positional index is
  used only when there are no bindings, as with older agents that lack
  `tool_start_callback`. It is not authoritative on its own: it advances
  only at a tool boundary that already holds reasoning, so with
  adaptive-thinking models segment `k` can hold step `k+1`'s trace.
- **Inline `<think>` blocks** in the content are always split into `reasoning`
  and merged with the value chosen above.

Regression coverage: `tests/test_settlement_agent_reasoning_authoritative.py`.

## Terminal progress notifications

`terminal(background=true, heartbeat=60, notify=true)` can wake the owning
conversation repeatedly while the process is running. These are Agent process
notifications, not SSE keepalives. The Agent queues a heartbeat only when there
is new output; WebUI uses the Agent's formatter for elapsed time, the next
interval, and any output-tail omission marker.

The background dispatcher and next-turn drain share atomic heartbeat claims
keyed by `(session_id, process_id, seq)`. A duplicate tick is delivered once;
distinct sequences, including out-of-order arrivals, remain independent, and
no heartbeat consumes the process's final-completion marker. Claimed sequences
are stored as merged inclusive ranges per owning session and process, so a
contiguous stream needs only one range. Claims remain for the session's
in-process lifetime regardless of unrelated traffic or process completion;
session deletion releases only that session's records. These claims are not
restart-durable. Busy sessions retain each tick under its own delivery identity
and use the existing deferred-wakeup/retry lane; no overlapping turn is started.

Progress uses only the native server-side wakeup path: once the turn starts,
`start_session_turn` emits `server_turn_started` so an open tab can attach to
its stream. Heartbeats never enter the `bg_task_complete`/legacy
`process_complete` publisher, which the browser interprets as a completion
toast. A later final completion still uses that lane with the real process id
as `task_id`. Wakeups do not require an open browser tab. See
`tests/test_process_heartbeat_wakeup.py` for both queue lanes, the completion
publisher boundary, native live-view publication, duplicate/race handling,
busy-turn deferral and later completion.

## Heartbeats and proxy behavior

- All long-lived streams emit SSE keepalive comment lines on the
  `_SSE_HEARTBEAT_INTERVAL_SECONDS` cadence (currently 5 seconds), which is
  short enough to survive typical reverse-proxy idle timeouts.
- Handlers send `X-Accel-Buffering: no` so nginx-style proxies pass events
  through unbuffered.
- Deployments behind buffering proxies that read-until-close (notably
  Tornado-based `jupyter-server-proxy`) can set `HERMES_WEBUI_SSE_CHUNKED=1`
  to frame each event as an HTTP/1.1 chunk. The default wire format is
  unchanged when the flag is unset.
