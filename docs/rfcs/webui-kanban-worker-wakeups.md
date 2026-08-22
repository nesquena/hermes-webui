# WebUI Kanban worker wakeups

- **Status:** Proposed
- **Author:** @kopamed
- **Created:** 2026-07-15

## Problem

Hermes Agent already creates a `kanban_notify_subs` row when a Kanban task is
created from a session. Messaging gateway adapters consume those subscriptions,
notify the user, and inject an internal event that wakes the originating agent.

WebUI-originated subscriptions use `platform = 'webui'`, but the gateway has no
WebUI messaging adapter. The rows are therefore never consumed: their
`last_event_id` remains unchanged, the browser receives no notification, and the
originating WebUI agent never resumes after the worker completes or blocks.

That breaks the orchestration loop:

```text
WebUI agent creates worker
→ worker completes or blocks
→ nothing wakes the originating WebUI session
→ user must manually inspect Kanban and prompt the agent again
```

This is a server-side delivery gap. It is not primarily a frontend-notification
problem. A toast without an agent turn would leave the workflow broken.

## Current source inventory

The required producer and consumer primitives already exist:

- Hermes Agent owns the Kanban schema and writes `tasks`, `task_events`, and
  `kanban_notify_subs`.
- `api/kanban_bridge.py` already uses `hermes_cli.kanban_db` as the sole Kanban
  source of truth and knows how to enumerate/open boards without copying their
  data into WebUI storage.
- `api/routes.py::start_session_turn()` starts a durable, server-side agent turn
  for an existing WebUI session without a browser request.
- `api/routes.py::_start_chat_stream_for_session()` serializes turns per session
  and rejects a racing turn with HTTP-style status `409`.
- `api/background_process.py` already implements automatic background-process
  wakeups, including persistent session SSE live-view, busy-turn deferral,
  process-wakeup transcript presentation, and closed-tab persistence.
- `static/messages.js` already listens for `server_turn_started` and attaches the
  normal live stream renderer. A closed tab is not required for the turn to run.

The missing primitive is a WebUI-owned consumer for `platform = 'webui'` Kanban
subscriptions.

## Decision

Add a WebUI server background service that consumes WebUI Kanban subscriptions
and invokes the existing durable session-turn path.

```text
all Hermes Kanban boards
  → kanban_notify_subs(platform='webui')
  → new terminal task_events
  → WebUI Kanban notification watcher
  → validate subscription.chat_id against a WebUI session
  → start_session_turn(chat_id, batch_prompt, source='process_wakeup')
  → existing server_turn_started/session SSE/recovery machinery
  → advance subscription cursor after the turn is accepted
```

The browser is an observer, never part of the delivery path. Closing the tab
must not prevent the originating agent from waking.

## Ownership and repository scope

### Hermes Agent owns

- Kanban database location and schema
- task and event writes
- automatic subscription creation
- messaging-platform notification delivery

No Hermes Agent code is required for this implementation.

### Hermes WebUI owns

- consumption of subscriptions whose platform is exactly `webui`
- resolution of `chat_id` to a persisted WebUI session
- profile/session isolation checks
- starting and rendering the server-initiated WebUI turn
- lifecycle of the watcher thread
- first-rollout protection against historical ghost subscriptions

The WebUI must not consume `telegram`, `discord`, `slack`, `tui`, or unknown
platform rows.

## Goals

1. Wake the exact WebUI session that created a subscribed worker task when that
   task reaches `done` or `blocked`.
2. Work while the browser is visible, hidden, disconnected, or closed.
3. Let the resulting assistant response persist in the normal WebUI transcript.
4. Never run two turns concurrently in one session.
5. Never route a completion across profile/session boundaries.
6. Preserve Kanban as the single source of truth. Do not copy task state into a
   second database.
7. Consume all existing and archived boards, not only the board currently open
   in the UI.
8. Avoid any LLM/API work while idle. The watcher performs only bounded local
   SQLite reads until a real terminal event exists.
9. Avoid a first-upgrade wakeup storm from historical `webui` subscriptions
   whose cursors were never consumed before this feature existed.
10. Add behavioral tests first and prove that they fail on the pre-feature code.

## Non-goals

- Intermediate progress, heartbeat, comment, or worker-log notifications
- TUI notification repair
- A new generic Hermes Agent adapter
- Polling Kanban from an LLM turn or cron job
- A browser-mediated `/api/chat/start` callback
- OS/browser push notifications
- A new WebSocket or SSE protocol
- Changes to generic OpenAI-compatible API async-delivery semantics
- Multi-process/multi-instance exactly-once delivery
- Opening or pushing a pull request before the local branch is manually tested

The first implementation wakes on terminal task outcomes only. A task's final
summary/result is the "output" delivered to the originating agent.

## Required implementation

### 1. New module: `api/kanban_notifications.py`

Create a focused module with no HTTP route responsibilities.

It owns:

- board discovery
- first-rollout baseline state
- candidate reads
- terminal-event classification
- session/profile validation
- per-session batching
- dispatch to `start_session_turn`
- cursor advancement
- watcher thread lifecycle

Do not put this loop in `api/kanban_bridge.py`; that module is request/response
CRUD + SSE. Do not put Kanban SQL in `api/background_process.py`; that module
owns process-registry completions.

### 2. Thread lifecycle

Expose:

```python
start_kanban_notification_watcher() -> bool
stop_kanban_notification_watcher(timeout: float = 2.0) -> None
```

Requirements:

- one daemon thread per WebUI process
- idempotent start/stop
- a dedicated lifecycle lock around check-then-start
- a `threading.Event` stop signal
- wait on the stop event for backoff; never use a tight retry loop
- default local-DB poll interval: 1 second
- no model/API calls when no terminal candidates exist
- ImportError, missing schema, archived-board race, and SQLite contention are
  logged and retried with bounded backoff; they must not crash WebUI startup
- every SQLite connection is short-lived and closed on every exit

This local SQLite watcher is not the rejected "poll Kanban with an agent/cron"
approach. It costs no model tokens and exists only because SQLite has no
cross-process event callback.

### 3. Server startup and shutdown wiring

Modify `server.py`:

- start the watcher after Hermes imports are verified and runtime directories
  exist
- print one concise success line only when a new thread actually starts
- treat startup failure as a warning, not a server-fatal error
- stop/join the watcher in the existing `serve_forever()` `finally` block
- preserve the existing background-process and SessionChannel lifecycle order
- do not add an `atexit`-only cleanup path; managed SIGTERM already unwinds the
  server `finally`

### 4. Board discovery

Use `hermes_cli.kanban_db`; do not derive raw board paths manually.

On every discovery refresh:

1. include `DEFAULT_BOARD`
2. include every row from `list_boards(include_archived=True)`
3. include the current board if it is not already present
4. normalize and deduplicate slugs
5. do not materialize a missing/archived board merely by probing it

Archived boards remain observable because a board can be archived while one of
its workers is still running.

Refresh board discovery periodically, not only at watcher startup, so boards
created after WebUI starts become observable.

### 5. Subscription schema contract

The current Agent schema provides the equivalent of:

```text
kanban_notify_subs:
  task_id
  platform
  chat_id
  notifier_profile   # older compatible builds may call this `profile`
  last_event_id
  created_at
  updated_at
```

Production code must inspect `PRAGMA table_info(kanban_notify_subs)` once per
board/schema generation and:

- require `task_id`, `platform`, `chat_id`, and `last_event_id`
- use `notifier_profile` when present
- accept legacy `profile` only when `notifier_profile` is absent
- treat a missing profile column as legacy/unknown, not as the active profile
- fail closed for a missing required column
- never create, migrate, or replace the Agent-owned table

Only rows with `platform = 'webui'` are candidates.

### 6. First-rollout baseline

A new consumer must not replay every historical ghost row on first upgrade.
Existing installations can contain many completed `webui` subscriptions with
`last_event_id = 0` because there was no consumer.

Persist one atomic marker under the WebUI `STATE_DIR`, for example:

```text
kanban_notification_consumer_v1.json
```

Shape:

```json
{
  "schema_version": 1,
  "created_at": 0,
  "board_event_baselines": {
    "default": 123,
    "other-board": 456
  }
}
```

First initialization:

1. discover all existing boards, including archived boards
2. read `MAX(task_events.id)` for each board
3. atomically write the complete marker via temporary file + `os.replace`
4. only after the marker is durable, allow dispatch
5. for each existing `webui` subscription, treat events at or below that board's
   recorded baseline as already observed and advance its cursor accordingly

Consequences:

- old ghost completions do not start dozens of historical agent turns
- a task already running at cutover still notifies when its later terminal event
  receives an ID above the baseline
- events created after a recorded baseline survive WebUI restarts
- a board first created after initialization has baseline `0`, so its real events
  are not silently discarded

If the marker cannot be parsed or durably written, fail closed: log a warning and
do not dispatch. Never silently regenerate a malformed marker and risk replaying
old rows.

Tests must use a temporary `STATE_DIR`. Never baseline or mutate the user's live
Kanban database during tests.

### 7. Event scanning and terminal classification

For each subscription, read task events with:

```sql
WHERE task_id = ? AND id > last_event_id
ORDER BY id ASC
```

Parse `payload` as JSON best-effort. Malformed payload cannot crash the loop.

A wake-worthy event is one of:

- a canonical completion event (`completed`, `complete`, or `done`)
- a canonical blocked event (`blocked`)
- a status event whose parsed payload has `status = 'done'` or
  `status = 'blocked'`
- a version-compatible terminal event whose parsed payload reports one of those
  terminal statuses

Use the current task row as additional context, not as the only transition
signal. A later comment on an already-completed task must not create another
terminal wakeup.

Non-terminal events are consumed without starting a turn, so progress/comment
rows are not reread forever. A future terminal event has a greater event ID and
will still be seen.

For a terminal candidate, read the authoritative task and handoff fields that
exist in the current schema, including:

- task ID
- title
- current status
- result/summary when available
- block kind/reason when available
- event kind, event ID, and board slug

Missing optional columns are omitted, never fabricated. Raw worker logs and
secrets are never copied into the prompt.

### 8. Routing identity and profile isolation

The wake target is:

```text
subscription.chat_id
```

It is not `tasks.session_id`. The task's internal worker transcript can rotate or
belong to the worker; the subscription's `chat_id` is the originating WebUI
conversation.

Before dispatch:

1. load `get_session(chat_id)`
2. reject a missing/non-writable target as stale
3. if the subscription has a non-empty profile, compare it with the session's
   persisted profile using the existing profile-alias matcher (`default` and the
   root alias must compare the same way the rest of WebUI does)
4. on a positive mismatch, fail closed, log an error without prompt content, and
   consume/quarantine the terminal event rather than waking another profile
5. when the subscription profile is absent (legacy row), trust the resolved
   session's persisted profile; never substitute the currently selected UI
   profile

The watcher must wake the originating session even if the user has since
switched the active WebUI profile or opened another chat.

### 9. Per-session batching

One scan can find multiple terminal tasks for one session. Group candidates by
originating `chat_id` across all boards and start one turn per session.

Requirements:

- stable order: board slug, event ID, task ID
- maximum 20 task updates per wake turn
- maximum prompt size 12,000 characters
- truncate each optional summary/result field independently with a visible
  `…(truncated)` marker
- never start N simultaneous turns for N tasks in one session
- leave overflow candidates unconsumed for the next serialized wake turn

Prompt contract:

```text
[IMPORTANT: KANBAN WORKER UPDATE — server-generated, not a human message]

The following subscribed Kanban task(s) reached a terminal state:

- Board `<board>` · `<task_id>` · `<title>` · status `<status>`
  Summary: <summary when present>
  Result: <result when present>
  Blocker: <reason when present>
  Delivery event: <event_id>

Read the relevant task handoff with kanban_show if more context is needed, then
continue the originating workflow. Do not ask the user to repeat work already
present in the task handoff.
```

Escape/control-normalize database text before formatting. Do not let task titles
or summaries impersonate the server header or inject extra pseudo-instructions.
Task text is untrusted data and must be clearly delimited.

### 10. Turn dispatch and concurrency

Reuse:

```python
api.routes.start_session_turn(
    chat_id,
    prompt,
    source="process_wakeup",
)
```

Use the existing `process_wakeup` source for the first implementation because it
already provides:

- non-human transcript rendering (`Background wakeup`)
- server-initiated stream live-view
- closed-tab persistence
- automatic-wakeup provider-pause handling
- cancellation/recovery semantics

Do not add a parallel `kanban_wakeup` source unless every existing
`process_wakeup` branch in routes, streaming, gateway chat, cancellation,
recovery, rendering, and tests is generalized in the same change. A half-added
source would silently lose recovery behavior.

Dispatch state machine:

| Result | Cursor action | Retry behavior |
|---|---|---|
| status `< 400` with stream ID | advance delivered cursors | none |
| `409` active stream | do not advance | retry after session becomes idle |
| `409 process_wakeup_paused` | do not advance | bounded backoff, no hot loop |
| transient exception / SQLite contention | do not advance | bounded backoff |
| missing session / positive profile mismatch | consume/quarantine and log | no infinite retry |
| other persistent 4xx | do not spin; log and back off | bounded retry |

`start_session_turn()` is the authoritative race backstop. A pre-check may reduce
noise but cannot replace handling its `409` response.

Do not hold a Kanban SQLite connection or transaction while resolving a model or
starting an agent turn.

### 11. Cursor semantics and delivery guarantee

After `start_session_turn()` accepts a batch, update each included subscription
with a monotonic conditional write:

```sql
UPDATE kanban_notify_subs
SET last_event_id = ?, updated_at = ?
WHERE task_id = ?
  AND platform = 'webui'
  AND chat_id = ?
  AND last_event_id < ?
```

Include the profile discriminator when the schema and row provide one.

Never move a cursor backwards. Never advance a terminal candidate before the
turn is accepted.

The first implementation is **at-least-once**, not mathematically exactly-once:
a process crash after a turn is accepted but before the cursor commit can replay
the same delivery. That rare duplicate is preferable to silently losing a worker
completion. The prompt's board/task/event identity makes replay recognizable and
idempotent for the agent.

Do not claim exactly-once delivery without a durable cross-database transaction
or delivery ledger. That is outside this change.

### 12. Browser behavior

No new browser request is required for agent wakeup.

The existing `start_session_turn()` path emits `server_turn_started`; the
existing session-scoped SSE client attaches the normal stream renderer when the
originating session is visible. If the tab is hidden/disconnected/closed, the
turn still runs and persists; existing reconciliation shows it when the session
is opened again.

No new frontend poll, WebSocket, or custom renderer should be added unless a
behavioral test proves the existing process-wakeup renderer cannot represent the
Kanban prompt.

## Expected file changes

Required:

- `api/kanban_notifications.py` — new watcher and pure helpers
- `server.py` — startup/shutdown lifecycle wiring
- `tests/test_kanban_notifications.py` — unit/integration-style behavioral tests
- `tests/test_server_kanban_notification_wiring.py` or equivalent — lifecycle
  wiring regression coverage
- this RFC and `docs/rfcs/README.md`

Modify only if a failing test proves necessary:

- `api/background_process.py`
- `api/routes.py`
- `static/messages.js`
- `static/ui.js`
- `static/i18n.js`

Forbidden scope creep:

- Hermes Agent repository changes
- `CHANGELOG.md`
- new dependencies
- a new framework or scheduler
- Kanban schema migration
- unrelated refactors
- PR creation/push

## Test-first acceptance matrix

Tests must be written and run red before production code. Use
`./scripts/test.sh`, never bare pytest.

| Requirement | Required test |
|---|---|
| WebUI-only ownership | `telegram`/`discord`/`tui` rows are untouched |
| Correct target | `subscription.chat_id` is passed to `start_session_turn`; `task.session_id` is not |
| Terminal completion | `done` transition starts one wake turn with title + summary/result |
| Blocked task | `blocked` transition starts one wake turn with blocker context |
| Non-terminal noise | progress/comment/heartbeat events advance without a wake turn |
| Later comment | comment after an already-consumed completion does not wake again |
| Multi-board | subscriptions on default, named, and archived boards are discovered |
| New board | a board created after watcher start is discovered on refresh |
| Multi-task batching | several task completions for one session create one ordered turn |
| Cross-session isolation | completions for two sessions create separate turns |
| Profile isolation | positive subscription/session profile mismatch never wakes target |
| Legacy profile | absent profile column uses persisted session profile safely |
| Busy race | `409` leaves cursor untouched and later idle scan delivers once |
| Provider pause | paused automatic wakeup backs off without cursor advance or hot loop |
| Successful cursor | accepted turn monotonically advances each delivered subscription |
| Cursor failure | failed dispatch leaves cursor untouched |
| Malformed payload | malformed JSON cannot kill the watcher |
| Missing schema | required-column absence fails closed without migration |
| Legacy ghosts | first-run baseline suppresses old cursor-0 terminal events |
| In-flight at cutover | event with ID above recorded baseline still wakes |
| Restart durability | existing baseline marker preserves post-cutover undelivered events |
| Marker corruption | malformed marker fails closed; no wakeup storm |
| Thread idempotency | concurrent start calls create exactly one watcher thread |
| Clean shutdown | stop signal joins the watcher without hanging |
| Closed tab | dispatch does not depend on an SSE subscriber/browser callback |
| Active tab | existing `server_turn_started` path remains the only live renderer path |
| Prompt safety | control chars/untrusted task content cannot forge the server header |
| Prompt bounds | >20 tasks and oversized summaries are bounded; overflow remains pending |

Tests use temporary SQLite databases and temporary WebUI state. They must never
open or mutate the user's live `~/.hermes` state.

## Required verification

Run at minimum:

```bash
./scripts/test.sh tests/test_kanban_notifications.py -v
./scripts/test.sh tests/test_server_kanban_notification_wiring.py -v
./scripts/test.sh tests/test_kanban_bridge.py -v
./scripts/test.sh tests/test_process_wakeup_rendering.py -v
./scripts/test.sh tests/test_start_session_turn_runtime_adapter.py -v
```

Then run the full suite or the repository's accepted sharded equivalent:

```bash
./scripts/test.sh
```

Also run the repository lint command documented by current project tooling.

## Manual end-to-end acceptance test

The feature is not ready for a PR until all steps pass on the local feature
branch:

1. Start WebUI from this branch with normal user state, after automated tests
   have passed against isolated state.
2. Open a WebUI session and ask the agent to create a real Kanban worker via the
   in-process `kanban_create` tool.
3. Send no additional user message.
4. Let the worker complete.
5. Confirm within one watcher interval that the originating agent starts a new
   turn in the same session.
6. Confirm the agent reads the handoff and continues the workflow rather than
   merely displaying a toast.
7. Refresh and confirm the wakeup notice + assistant response remain in history.
8. Close the browser, create/allow another subscribed worker completion, reopen
   WebUI, and confirm the autonomous response was persisted.
9. Complete a worker while a user turn is active; confirm the completion waits
   and runs only after the active turn ends.
10. Complete at least two workers together; confirm one bounded batch turn, not
    concurrent duplicate turns.
11. Switch the active profile before completion; confirm the original session is
    woken and no other profile receives the event.
12. Restart WebUI with an undelivered post-cutover completion and confirm it is
    delivered once after restart.

Record exact task IDs, branch SHA, watcher log lines, and observed session ID in
local test notes. Do not put private paths, prompts, secrets, or user data in the
public PR body.

## Rollout and PR gate

1. Implement on `feat/webui-kanban-worker-wakeups`.
2. Keep commits local while the user performs the manual acceptance test.
3. Do not push or open a PR automatically.
4. After user approval, rebase/fast-forward against current `origin/master`, rerun
   affected and full tests, independently review the diff, then open a focused PR.
5. The PR must state the at-least-once guarantee honestly and call out the
   first-rollout baseline behavior.

## Rejected alternatives

### Frontend-only Kanban toast

Rejected because it does not wake the agent and fails with a closed tab.

### Browser POST to `/api/chat/start`

Rejected because the browser becomes a required delivery hop. Background work
must complete when the browser is closed.

### Agent/cron polling

Rejected because it burns model invocations or scheduler work while idle and is
not tied durably to the originating session.

### Mark the generic API server async-delivery capable

Rejected because stateless OpenAI-compatible clients have no durable receive
channel. WebUI's persisted session is the relevant delivery primitive.

### Hermes Agent WebUI adapter

Rejected for this slice because WebUI already owns session persistence,
server-side turns, live rendering, and recovery. Adding a fake messaging adapter
would duplicate those mechanisms and expand the change across repositories
without solving a missing WebUI primitive.
