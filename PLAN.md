# Phase 1: Real-time Gateway Session Sync

## Overview

Enable the WebUI sidebar to show gateway sessions (telegram, discord, slack, etc.) alongside CLI sessions, controlled by the existing "show_cli_sessions" checkbox (renamed to "Show agent sessions"). Gateway sessions update in real-time via SSE push when new messages arrive.

## Key Design Decisions

1. **Read-only**: Gateway sessions are view-only in the WebUI (no reply/edit/delete)
2. **Same toggle**: Reuse `show_cli_sessions` setting (renamed to `show_agent_sessions`)
3. **No agent changes**: Pure polling of state.db from the WebUI side
4. **Lightweight polling**: Background thread checks state.db every 5 seconds for changes
5. **SSE push**: Frontend subscribes to an SSE endpoint that receives change notifications from the watcher thread
6. **Backward compat**: `show_cli_sessions` key in settings.json is preserved as-is; only the UI label changes

## Backend Changes

### New file: `api/gateway_watcher.py`
- Background daemon thread that polls state.db every 5 seconds
- Tracks the set of known session IDs and their `last_activity` timestamps
- On change detection, notifies all SSE subscribers
- Uses the same `_get_state_db()` pattern as `api/state_sync.py`
- Exports: `start_watcher()`, `stop_watcher()`, `GatewayWatcher` class

### Modified file: `api/models.py`
- Extend `get_cli_sessions()` to accept an optional `sources` parameter
- When called with `sources=None`, filter to `source='cli'` only (backward compat for existing callers)
- Add `get_gateway_sessions(sources=None)` that queries state.db for non-cli, non-webui sources
- Both functions share the same DB resolution pattern
- New function: `get_agent_sessions()` that combines CLI + gateway sessions

### Modified file: `api/routes.py`
- Add GET `/api/sessions/gateway/stream` SSE endpoint
  - When `show_agent_sessions` is enabled, streams change events
  - Events: `{"type": "sessions_changed", "sessions": [...]}`
  - Uses the watcher's subscriber queue
- Modify `/api/sessions` to include gateway sessions when `show_cli_sessions` is enabled
  - Deduplication logic: exclude any session_id that already exists in webui sessions

### Modified file: `server.py`
- Call `start_watcher()` in `main()` after server setup
- Call `stop_watcher()` on shutdown

### Modified file: `api/config.py` (minimal)
- Add `_SETTINGS_BOOL_KEYS` entry if needed for any new settings

## Frontend Changes

### Modified file: `static/index.html`
- Rename checkbox label from "Show CLI sessions in sidebar" to "Show agent sessions in sidebar"

### Modified file: `static/panels.js`
- Update window globals reference: `_showCliSessions` kept for backward compat (it maps to the same setting key)

### Modified file: `static/sessions.js`
- When `_showCliSessions` is true, subscribe to `/api/sessions/gateway/stream` SSE
- On `sessions_changed` event, re-render the session list
- Gateway sessions display with a source badge (e.g., "TG", "DC", "SL")
- Gateway sessions are read-only: no reply, no rename, no delete

## Test Strategy

### New file: `tests/test_gateway_sync.py`
All tests are integration tests against the test server on port 8788:

1. **test_get_sessions_includes_gateway_source**: Create gateway session in state.db, verify `/api/sessions` returns it when setting enabled
2. **test_get_sessions_excludes_gateway_when_disabled**: Verify gateway sessions hidden when setting off
3. **test_gateway_sessions_read_only_fields**: Verify `is_cli_session` and `source_tag` fields present
4. **test_gateway_sse_stream_opens**: Verify SSE endpoint returns 200 and streams events
5. **test_gateway_watcher_detects_new_session**: Insert a row into state.db, verify SSE event fires
6. **test_gateway_sse_respects_setting**: Verify SSE returns empty when setting disabled
7. **test_settings_label_renamed**: Verify the settings panel has the new label

## Polling Mechanism Design

```
gateway_watcher.py
  └── GatewayWatcher (daemon thread)
       ├── Every 5 seconds:
       │   ├── SELECT sessions WHERE source NOT IN ('webui', 'cli')
       │   ├── Compare snapshot with previous
       │   └── If changed: push to subscriber queues
       └── Subscriber management:
           ├── subscribe() -> returns queue.Queue
           └── unsubscribe(queue)
```

## SSE Endpoint Design

```
GET /api/sessions/gateway/stream
  - Response: text/event-stream
  - Events:
    - event: sessions_changed
      data: {"sessions": [...], "added": 3, "removed": 0}
    - Keep-alive: empty comment every 30s
  - Closed when client disconnects
  - Only active when show_agent_sessions setting is true
```

## Implementation Order

1. Write tests first (TDD)
2. Create `api/gateway_watcher.py` 
3. Extend `api/models.py` with gateway session support
4. Add SSE endpoint in `api/routes.py`
5. Wire up watcher in `server.py`
6. Frontend changes (HTML label, SSE subscription, source badges)
7. Run full test suite
8. Commit
