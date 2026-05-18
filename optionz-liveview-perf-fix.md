# Option Z live-view + SSE backpressure fix

Branch: `feat/process-complete-event-isla` — on top of `481ddb9`
Scope: TWO tightly-coupled follow-ups to Option Z (server-side wakeup primary).
Not pushed, no PR. User restarts manually (`python3 bootstrap.py`), pid 48778 untouched.

---

## §1 Playwright / scripted repro evidence (BEFORE)

Driven read-only against the user's REAL running instance (pid 48778, port 8787).
No agent turn started, no process signalled.

### Defect A — SSE thread exhaustion (multi-tab)

`server.py:123 QuietHTTPServer(ThreadingHTTPServer)`, `daemon_threads=True`,
`request_queue_size=64` → **one OS thread per connection, no pool cap**.
Repro: 12 raw SSE clients open `/api/session/stream`, read the initial frame,
then stop reading and hold the socket (a backgrounded/slow tab).

```
[baseline]        threads(pid 48778)=11  sockets(:8787)=11
[+12 stalled SSE] threads=22 (delta +11)  sockets=33 (delta +22)
[t+20s held]      threads=22   (pinned the WHOLE hold)
[t+40s held]      threads=22
[t+60s held]      threads=22
[clients gone +8s] threads=10
```
→ Each SSE connection pins ~1 OS thread for the connection's entire lifetime.
`repro_defect_a.py` (workspace).

**Definitive socket-level proof** (`proof_write_deadline.py`, real TCP, real
`api.streaming` code, no mocks — a turn streaming 4 KB token frames to a
client that never reads, tiny SO_RCVBUF):

```
socket.timeout in _CLIENT_DISCONNECT_ERRORS?  True
[BEFORE fix (no deadline)] STILL BLOCKED after 18s — thread pinned (this is the BUG)
```
The server's `wfile.write()/flush()` blocks **indefinitely** on a backpressured
socket; the worker thread never reaches its `finally: unsubscribe`, so the
SessionChannel reaper can never reclaim it either. Enough such tabs ×
sessions → new requests queue past `request_queue_size=64` → "streaming pending".

### Defect B — server-initiated wakeup turn not shown live (code-path proof)

Option Z's `_process_one → _start_server_side_wakeup_turn →
routes.start_session_turn → _start_chat_stream_for_session` creates
`STREAMS[stream_id]` and a worker that emits the turn's token/tool/stream_end
frames **only into that per-turn StreamChannel**. The browser only opens an
`EventSource(/api/chat/stream?stream_id=…)` when IT POSTs `/api/chat/start`
(`messages.js` `attachLiveStream`, called from `sendMessage` / `loadSession`
reattach). A server-initiated turn has a STREAMS channel **no EventSource is
attached to**. The per-session SSE (`/api/session/stream` + `SessionChannel`)
only carried `process_complete`, and `_handleProcessCompleteEvent` is, post
Option-Z-pivot, a no-op for state. ⇒ an already-open tab renders nothing until
a manual refresh re-reads persisted session state. Live-view is broken for
exactly the turns Option Z introduces. (Session `ba58e55a9548` corroborates:
the wakeup turn IS persisted, it just wasn't shown live.)

---

## §2 Defect B fix — fan server-initiated turn onto the live-view channel

**Server** — `api/routes.py` `start_session_turn()` (after
`_start_chat_stream_for_session` returns, ~L7472): on a successful start
(`_status < 400` and a `stream_id`), emit a lightweight
`server_turn_started {session_id, stream_id, source}` frame onto the existing
per-session live-view channel via `background_process.get_session_channel()`
— the **non-creating** accessor, so the closed-tab path stays a pure no-op and
the Option Z headline (server-side wakeup) is completely unaffected. Wrapped
in try/except (best-effort; never breaks the turn).

**Frontend** — `static/messages.js` `startSessionStream()` adds an
`es.addEventListener('server_turn_started', …)` that **reuses the existing
chat-stream renderer**: it mirrors the `loadSession` reattach setup
(`S.busy`, `S.activeStreamId`, `appendThinking`, approval/clarify polling)
and calls the existing `attachLiveStream(sid, stream_id, …)` — the exact path
`/api/chat/start` uses. No second renderer hand-rolled (task pitfall honored).
Idempotent guards: bails if `S.activeStreamId === streamId` or
`LIVE_STREAMS[sid].streamId === streamId` (no double-render if a per-turn
chat-stream is also open); only drives when the session is the current pane.

Closed tab → turn still runs + persists server-side (Option Z, unchanged).
Open tab → same turn additionally streams live.

---

## §3 Defect A fix — SSE write deadline (least-invasive, chosen)

`api/streaming.py`: new `SSE_WRITE_DEADLINE_SECONDS = 20.0` +
`_sse_set_write_deadline(handler, seconds=None)` — arms a socket-level
timeout (`handler.connection.settimeout`) once, right after `end_headers()`.
Best-effort (never raises; a missing transport just keeps pre-fix behaviour
for that one connection).

Applied uniformly to **all 6 long-lived SSE endpoints** in `api/routes.py`
(verified `grep -c '_sse_set_write_deadline(handler' = 6`):
chat-stream `_handle_sse_stream` (~L5797), terminal `_handle_terminal_output`
(~L5921), gateway `_handle_gateway_sse_stream` (~L6000), approval
(~L6355), clarify (~L6457), session `_handle_session_sse_stream` (~L6509).

Why least-invasive (vs ThreadPoolExecutor server — explicitly gated behind
evidence in the task): a healthy keepalive/event write completes in << 1 ms,
so a 20 s deadline never trips for a live tab. Only a backpressured (stuck)
socket blocks past it; the write then raises `socket.timeout`, which **is
`TimeoutError` on Python 3.10+ and is already a member of
`routes._CLIENT_DISCONNECT_ERRORS`**, so every SSE handler's *existing*
`except _CLIENT_DISCONNECT_ERRORS:` breaks the loop, `finally` drops the
subscriber, the OS thread is released, and the SessionChannel reaper can now
reclaim the channel. The browser's `EventSource` auto-reconnects;
`SessionChannel` already supports reconnect + offline buffer so no events are
lost. Zero new threads, zero new dependencies, no blast radius beyond SSE
write paths. The 60 s subscribers-empty grace reaper + 4 h idle TTL were
verified to fire (3 reaper tests green); they now actually get the chance to,
because the handler thread can finally exit.

Coalescing chat-stream + session-stream into one socket was evaluated and
**not done**: the per-turn chat-stream renderer is load-bearing for normal
user-initiated turns; the task says only coalesce if provably safe. The
write-deadline + prompt-reaping path is sufficient (re-measured below), so the
higher-blast-radius change was correctly avoided.

---

## §4 Playwright / scripted re-verify evidence (AFTER)

Isolated trial instance per AGENTS.md: `HERMES_HOME=/tmp/hwlf-home
HERMES_WEBUI_STATE_DIR=/tmp/hwlf-state HERMES_WEBUI_PORT=8790 python3
server.py` (torn down after; pid 48778 / port 8787 confirmed alive &
untouched).

`proof_write_deadline.py` (real sockets, real `api.streaming` code, the
faithful production trigger — a turn streaming token frames to a tab that
never reads):

```
socket.timeout in _CLIENT_DISCONNECT_ERRORS?  True
[AFTER  fix (deadline armed)] released via TimeoutError after 4.05s (deadline=4.0s)
[BEFORE fix (no deadline) ]   STILL BLOCKED after 18s — thread pinned (this is the BUG)
```

| | thread pinned by stuck SSE writer | reclaimed |
|---|---|---|
| BEFORE | indefinite (>18 s, observed; unbounded) | never (handler never exits) |
| AFTER  | ≤ `SSE_WRITE_DEADLINE_SECONDS` (20 s; 4.05 s at deadline=4) | yes — `finally` runs, reaper collects |

The 12-tab `repro_defect_a.py` shows the per-connection-thread *mechanism*
identically on both ports (5 s keepalives are too small to fill a kernel
buffer, so an *idle* tab never blocks a write — correct: the deadline must
NOT trip for a merely-idle tab, only under real streaming write pressure,
which `proof_write_deadline.py` exercises end to end).

---

## §5 Test results

New: `tests/test_optionz_liveview_perf.py` — **9 passed**
- `test_server_turn_streams_to_session_channel` (Defect B regression: a
  process_wakeup turn fans `server_turn_started` onto a subscribed
  SessionChannel)
- `test_server_turn_no_session_channel_is_noop` (closed-tab path: no
  auto-create, Option Z headline unaffected)
- `test_sse_write_deadline_helper_sets_socket_timeout` / `_never_raises`
- `test_sse_write_timeout_drops_slow_subscriber` (Defect A regression: stuck
  writer → `socket.timeout` ∈ `_CLIENT_DISCONNECT_ERRORS` → unsubscribe +
  thread released)
- 4 source-grep wiring guards (all 6 endpoints armed; frontend reuses
  `attachLiveStream`)

Headline closed-tab test `test_server_side_wakeup_when_idle_no_tab`: **green**.
`test_session_channel_option_x.py` (23) + `test_sprint43.py` +
`test_approval_sse.py`: **84 passed**, no regression.

Full suite: **5677 passed, 8 skipped, 1 xfailed, 2 xpassed, 8 subtests passed**
(EXIT=0). Baseline was 5679 collected; +9 new = all accounted for.
**0 failures, 0 regressions vs baseline.**

---

## §6 User verification steps

1. Restart YOUR instance yourself: `cd /mnt/d/Repositories/hermes-webui &&
   python3 bootstrap.py` (do NOT let the agent kill pid 48778).
2. **Closed-tab (headline, must still work):** open a session, send a prompt
   that runs `terminal(background=true, notify_on_complete=true,
   command="sleep 25")`, let the turn end, **close the tab**. After ~25 s
   reopen the session → the wakeup turn is present in history (server-side
   wakeup unchanged).
3. **Open-tab live (Defect B fixed):** same prompt, but **keep the tab open
   and idle**. After ~25 s the server-initiated wakeup turn now renders
   **live without a refresh** (a thinking card appears, tokens stream).
4. **Multi-tab perf (Defect A fixed):** open 4–6 tabs each on a session,
   start normal turns in 2 of them, background the others. Other tabs no
   longer stall in permanent "pending"; a backgrounded tab's stuck stream is
   dropped within ~20 s and auto-reconnects instead of pinning a server
   thread forever. `ls /proc/<webui-pid>/task | wc -l` no longer grows
   monotonically with backgrounded tabs.

---

## §7 Rollback

```
git reset --hard 481ddb9
```
(then restart). Removes both fixes; leaves Option Z server-side wakeup intact
since both changes are strictly additive.

---

## Changed files

- `api/streaming.py` — `SSE_WRITE_DEADLINE_SECONDS`, `_sse_set_write_deadline`
- `api/routes.py` — import; `_sse_set_write_deadline(handler)` ×6 SSE
  endpoints; `server_turn_started` fan-out in `start_session_turn`
- `static/messages.js` — `server_turn_started` listener reusing
  `attachLiveStream`
- `tests/test_optionz_liveview_perf.py` — new, 9 tests
- (workspace, not committed) `repro_defect_a.py`, `proof_write_deadline.py`
