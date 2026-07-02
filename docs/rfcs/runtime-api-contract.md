# Runtime API Contract

- **Status:** Proposed
- **Created:** 2026-07-02
- **Phase:** 1 — WebUI runtime contract

## Purpose

This document defines the stable event and status contract that WebUI, Hermes
Agent, and Hermex (mobile) clients can share across runtime boundaries. It is
the canonical serialization reference for `api/runtime_contract.py`.

The contract is a dependency-light layer. It does not import `api/streaming.py`,
`api/routes.py`, `server.py`, or any live runtime globals. Journal, route, and
adapter wiring are deferred to later phases.

## Event envelope

Every runtime event serializes to the following shape:

```json
{
  "event_id": "run_1:42",
  "seq": 42,
  "run_id": "run_1",
  "session_id": "20260702_abcdef123456",
  "type": "tool.updated",
  "created_at": 1778750000.0,
  "terminal": false,
  "payload": {}
}
```

### Required fields

| Field | Type | Description |
|---|---|---|
| `event_id` | `string` | Stable id formed as `{run_id}:{seq}`. Clients must deduplicate by this key. |
| `seq` | `int` | Monotonic sequence number per run. Clients may resume with `after_seq`. |
| `run_id` | `string` | Unique run identifier. |
| `session_id` | `string` | Session this run belongs to. |
| `type` | `string` | One of the supported event types below. |
| `created_at` | `float` | Unix timestamp (UTC) when the event was produced. |
| `terminal` | `bool` | Whether this event terminates the run (e.g. `done`, `error`, `cancelled`). |
| `payload` | `dict` | Event-type-specific payload. Secret-bearing keys are redacted on serialization. |

### Supported event types

| Type | Terminal | Typical payload |
|---|---|---|
| `run.started` | No | Run lifecycle metadata, controls available |
| `run.status` | No | Lifecycle state transition |
| `token.delta` | No | `{"text": "..."}` |
| `reasoning.delta` | No | `{"text": "..."}` |
| `reasoning.done` | No | `{"final_text": "..."}` |
| `progress` | No | `{"status": "..."}` |
| `tool.started` | No | `{"tool_call_id": "...", "name": "...", "args": {...}}` |
| `tool.updated` | No | `{"stdout": "...", "stderr": "..."}` |
| `tool.done` | No | `{"result": "...", "exit_code": 0, "duration": 1.2}` |
| `approval.requested` | No | `{"approval_id": "...", "command": "...", "choices": [...]}` |
| `approval.resolved` | No | `{"approval_id": "...", "choice": "once"}` |
| `clarify.requested` | No | `{"clarify_id": "...", "question": "..."}` |
| `clarify.resolved` | No | `{"clarify_id": "...", "response": "..."}` |
| `title.updated` | No | `{"title": "..."}` |
| `usage.updated` | No | `{"tokens": ..., "cost": ...}` |
| `usage.final` | No | `{"tokens": ..., "cost": ...}` |
| `error` | Yes | `{"code": "...", "message": "..."}` |
| `done` | Yes | `{"final_response": "...", "usage": {...}}` |

Events of types not listed above should be treated as opaque by clients and
consumed but not acted upon.

## Run status shape

```json
{
  "run_id": "run_1",
  "session_id": "20260702_abcdef123456",
  "status": "running",
  "last_event_id": "run_1:7",
  "last_seq": 7,
  "terminal": false,
  "controls": ["cancel"],
  "pending_approval_ids": [],
  "pending_clarify_ids": [],
  "error": null,
  "result": null
}
```

### Supported statuses

| Status | Meaning |
|---|---|
| `queued` | Run created but not yet executing |
| `running` | Run is actively executing |
| `awaiting_approval` | Run is paused, waiting for approval response |
| `awaiting_clarify` | Run is paused, waiting for clarify response |
| `paused` | Run is paused (e.g. goal pause) |
| `cancelling` | Cancel request received, terminal event pending |
| `cancelled` | Run was cancelled by user |
| `failed` | Run terminated with an error |
| `completed` | Run finished successfully |
| `expired` | Run exceeded its time budget |

Clients should treat unrecognized status values as a transient state and poll
again.

### Controls

| Control | Semantics |
|---|---|
| `cancel` | Client may request graceful cancellation |
| `queue` | Client may queue a follow-up message |
| `approval` | Client may respond to pending approval |
| `clarify` | Client may respond to pending clarify |

The `controls` list advertises which actions are currently available for the run.

## Reconnect behavior

1. **Cursor-based resume.** Clients store `last_seq` or `last_event_id` from
   the most recent event. On reconnect they request events with `after_seq`
   to avoid duplicates.

2. **Deduplication.** Clients must deduplicate by `event_id`. Events are
   at-least-once; replay is safe but may redeliver already-seen events.

3. **Terminal durability.** Once a run reaches a terminal state (`completed`,
   `cancelled`, `failed`, `expired`), its terminal event is durable and
   clients should not expect further events for that run.

4. **Stale runs.** If a run has no terminal event and the observed worker
   is no longer reachable, clients should surface a stale/interrupted diagnostic
   rather than an infinite spinner.

## Hermex / mobile usage pattern

Hermex clients consume this contract through the same `api/runtime_contract.py`
module. The recommended mobile usage pattern is:

1. **Attach to a run.** Call the observe endpoint (deferred to future route
   phase) with a `cursor` (last known `event_id` or `after_seq`).

2. **Process events.** Deserialize each `RuntimeEvent` from JSON, deduplicate
   by `event_id`, and render according to `type`.

3. **Poll status.** Use `RuntimeStatus` serialization to display lifecycle
   state and available controls.

4. **Submit controls.** Send control actions (cancel, approval, clarify) to
   the adapter-backed control endpoints. Response shapes match the `ControlResult`
   contract from `api/runtime_adapter.py`.

## Compatibility expectation

- Clients deduplicate by `event_id`.
- Clients resume with `after_seq` or `Last-Event-ID`.
- Terminal events are durable (once recorded, the run will not produce further
  non-terminal events).
- Payloads must not contain secrets. The `RuntimeEvent.to_dict()` serializer
  redacts known secret-bearing keys. Any new serialization path must apply the
  same redaction policy.
- The `event_id`, `seq`, `run_id`, `session_id`, `type`, `created_at`, and
  `terminal` fields are required in every event. Missing fields should be
  treated as a protocol error by clients.
- Unknown `type` values should be consumed but not interpreted; clients must
  not fail on unrecognized event types.

## Related documents

- `docs/rfcs/hermes-run-adapter-contract.md` — parent RFC for the adapter
  migration, state inventory, and slice plan.
- `api/runtime_adapter.py` — `RuntimeAdapter` protocol, `ControlResult`, and
  adapter seam.
- `api/run_journal.py` — Slice 1 append-only journal (uses a compatible event
  shape internally).
- `api/runtime_contract.py` — canonical Python implementation of this contract.
