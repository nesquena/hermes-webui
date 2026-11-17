# Kanban board status flow

The Kanban panel (`static/panels.js`) is a view over the Hermes Agent's
`hermes_cli.kanban_db`; `api/kanban_bridge.py` handles WebUI task writes.
Structured transitions use the Agent's own verbs; the claim-aware direct
write `_set_status_direct()` (`api/kanban_bridge.py:281`) still covers `triage`,
`todo` and some moves to `ready`. Scheduled (#7900) is covered in detail below.

## Columns

`BOARD_COLUMNS` (`api/kanban_bridge.py:32`) fixes the board order:

```
triage → todo → scheduled → ready → running → blocked → done
```

`archived` is also accepted as a status by `_validate_status`
(`api/kanban_bridge.py:272`) but is rendered as a terminal flag rather than a
column.

`running` is never entered from the UI: the bridge rejects
`PATCH {"status": "running"}` with HTTP 400 (`api/kanban_bridge.py:431`)
because the dispatcher/`claim_task` protocol owns `claim_lock`,
`claim_expires` and `worker_pid`.

## Where status can be changed

| Surface | Location | Statuses offered |
| --- | --- | --- |
| Drag a card between columns (desktop only — HTML5 drag, no touch drag) | `static/panels.js` | any column |
| Detail-view status buttons (the touch path: phone/tablet) | `static/panels.js:3868` | triage, todo, scheduled, ready, blocked, done, archived, plus Block / Unblock |
| Sidebar bulk status select | `static/index.html:237` | scheduled, ready, blocked, done, archived |
| Task editor modal | `static/panels.js:3484` | triage, todo, ready |
| Dispatcher (claim) | Hermes Agent | ready → running |

All of those funnel into `_patch_task()` (`api/kanban_bridge.py:380`), which
switches on the target status and calls the matching Agent verb. The bulk
endpoint does the same per id (`api/kanban_bridge.py:719`), so a multi-select
move to Scheduled is refused exactly like a single-card move would be.

## Scheduled

A Scheduled card is a time-delayed task: it is visible in its column but the
dispatcher does not claim it while it stays there.

### Entering Scheduled

`PATCH /api/kanban/tasks/<id>` with `{"status": "scheduled"}` — what the
detail-view button, a drag onto the Scheduled column and the bulk select all
send. The `scheduled` branch (`api/kanban_bridge.py:458`) then:

1. raises `scheduling requires a newer Hermes Agent` when the installed Agent
   exposes no `schedule_task` — HTTP 409, with **no** mutation, so an Agent
   that cannot schedule never ends up with a raw status write;
2. delegates to `kb.schedule_task(conn, task_id, reason=...)`, the Agent's own
   verb, which accepts `todo` / `ready` / `running` / `blocked` → `scheduled`.
   A Done → Scheduled move is refused by the Agent and surfaces here as HTTP
   400 `cannot schedule task from status: done`, and an active run is settled
   by the Agent with `outcome="scheduled"` rather than left orphaned;
3. passes `reason` only when the caller actually sent one, so a plain status
   move never synthesizes a run record that never happened and never
   overwrites the task's latest worker summary.

### Leaving Scheduled

- **→ ready** (`api/kanban_bridge.py:443`): when the current status is
  `blocked` **or** `scheduled`, the bridge calls `kb.unblock_task()` instead of
  a direct write. `unblock_task` re-gates on parent completion — a child whose
  parent is still unfinished lands in `todo` (not `ready`) and emits an
  `unblocked` event, while a child with all parents complete lands in `ready`.
- **→ triage / todo**: `_set_status_direct()` (`api/kanban_bridge.py:281`),
  the claim-aware direct write.
- **→ done / blocked / archived**: the Agent's `complete_task` / `block_task`
  / `archive_task`.
- **Unblock button** on the detail view (`api/kanban_bridge.py:768`): also
  calls `kb.unblock_task()`, so it applies the same parent re-gating.

### The task editor follows the Blocked precedent

`scheduled` is deliberately **not** in the modal's status `<select>`.
`_kanbanEditableStatusFor()` (`static/panels.js:3484`) maps it to `triage` for
display exactly like `blocked` / `running` / `done` / `archived`, so opening a
Scheduled card shows Triage together with the
`Actual status: Scheduled` hint (`static/panels.js:3541`), and an untouched
save omits `status` from the PATCH payload entirely
(`static/panels.js:3711`) — saving without touching the field keeps the card
Scheduled instead of silently demoting it to Triage.

Changing status on a Scheduled card from the modal is therefore explicit: the
payload carries `status` only when the user picked a value different from the
displayed default, and that value still goes through `_patch_task()` and the
Agent's verb for the target status.
