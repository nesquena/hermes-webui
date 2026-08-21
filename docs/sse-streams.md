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

## Browser background-stream connection budget

The browser keeps the selected conversation's `EventSource` open and may retain
recent background session streams in a bounded least-recently-used pool. Pool
eviction always calls the normal `closeLiveStream` teardown so inflight snapshot
and reattach bookkeeping remain intact.

The connection budget is fail-closed:

- HTTP/1.1, unknown, blank, cross-origin, or stale timing evidence allows at
  most **3** simultaneous chat/session streams, leaving headroom for fetch/XHR
  inside the browser's per-origin connection limit.
- A stable HTTP/2 or HTTP/3 page may retain up to **30** streams because the
  connection is multiplexed. The frontend prefers the newest same-origin
  Resource Timing entry and uses navigation timing only when no such entry
  exists.
- The protocol of a replacement `EventSource` cannot be known before opening.
  Therefore any explicit reconnect, SSE error, `online` transition, or BFCache
  restore permanently locks the current page to the 3-stream budget and prunes
  excess streams immediately. A reload is required before transport evidence
  may widen the pool again.

These rules are client-side safety limits; they do not alter the server SSE
endpoint contracts or heartbeat cadence.

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
