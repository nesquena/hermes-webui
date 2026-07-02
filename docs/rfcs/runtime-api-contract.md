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
- `api/runtime_journal.py` — ``RuntimeJournal`` class with durable append-only run event storage, active-session index, and replay support.
- `api/runtime_routes.py` — dispatched route handlers for capabilities, active-run, run status, event replay (JSON + SSE), cancel, approval, and clarify endpoints.

## Route reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/runtime/capabilities` | Return adapter mode and supported features. |
| `GET` | `/api/sessions/{session_id}/active-run` | Return active (non-terminal) run for session. |
| `GET` | `/api/runs/{run_id}` | Return run status from journal. |
| `GET` | `/api/runs/{run_id}/events` | Replay events (JSON or SSE). Query params: `after_seq`, `limit`. |
| `POST` | `/api/runs/{run_id}/cancel` | Cancel an active run (legacy-journal mode). |
| `POST` | `/api/runs/{run_id}/approval` | Respond to approval request (not_supported in legacy-journal). |
| `POST` | `/api/runs/{run_id}/clarify` | Respond to clarify request (not_supported in legacy-journal). |

## Legacy journal mirroring

When ``HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal``, the streaming engine
mirrors SSE events into the runtime journal. The following SSE event types are
mapped to contract event types:

| SSE | Contract |
|---|---|
| ``token``, ``interim_assistant`` | ``token.delta`` |
| ``reasoning`` | ``reasoning.delta`` |
| ``tool`` (with ``event_type=tool.started``) | ``tool.started`` |
| ``tool`` (generic) | ``progress`` |
| ``tool_complete`` | ``tool.done`` |
| ``done`` | ``done`` (terminal) |
| ``apperror``, ``error`` | ``error`` (terminal) |
| ``cancel`` | ``done`` (terminal) |
| ``approval`` | ``approval.requested`` |
| ``clarify`` | ``clarify.requested`` |
| ``metering`` | ``usage.updated`` |
| others | ``run.status`` or ``progress`` |

Events are written via ``RuntimeEvent.to_dict()`` which redacts secret-bearing
keys before hitting disk.

The ``RuntimeJournal`` stores events and run metadata under ``STATE_DIR / "runs" /``:

```
runs/
  run_<id>.jsonl   -- one JSONL file per run (redacted RuntimeEvent dicts)
  _index.json       -- active-session mapping + per-run status snapshots
```

The index shape:

```json
{
  "active_sessions": {"session_abc": "run_def456"},
  "runs": {
    "run_def456": {
      "run_id": "run_def456",
      "session_id": "session_abc",
      "status": "running",
      "last_event_id": "run_def456:7",
      "last_seq": 7,
      "terminal": false,
      "controls": ["cancel"],
      "pending_approval_ids": [],
      "pending_clarify_ids": [],
      "error": null,
      "result": null,
      "created_at": 1778750000.0
    }
  }
}
```

Events are stored via ``RuntimeEvent.to_dict()`` which redacts secret-bearing
keys before writing to disk. ``RuntimeEvent`` objects are reconstructed on read
via ``_dict_to_runtime_event()`` from the already-redacted stored form.

### Journal API

| Method | Signature | Description |
|---|---|---|
| ``create_run`` | ``(session_id, metadata=None) -> RuntimeStatus`` | Create a new run, update active-session mapping, return status. |
| ``append_event`` | ``(event: RuntimeEvent) -> RuntimeEvent`` | Append an event to the run's JSONL, update index. Raises ``ValueError`` for unknown runs. |
| ``read_events`` | ``(run_id, after_seq=None, limit=None) -> list[RuntimeEvent] | None`` | Read events, optionally cursor-based replay. Returns ``None`` for unknown runs. |
| ``get_status`` | ``(run_id) -> RuntimeStatus | None`` | Return current run status from index. Returns ``None`` for unknown runs. |
| ``get_active_run_for_session`` | ``(session_id) -> RuntimeStatus | None`` | Return the active (non-terminal) run for a session. Returns ``None`` if no active run. |
| ``mark_terminal`` | ``(run_id, status, result=None, error=None) -> RuntimeStatus | None`` | Mark a run as terminal, clear active-session mapping. Returns ``None`` for unknown runs. |

## Hermes Agent /v1/runs adapter (agent-runs)

When ``HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs`` is set, WebUI routes delegate
to the Hermes Agent /v1/runs HTTP runtime contract instead of local journals.

### Configuration

| Variable | Required | Description |
|---|---|---|
| ``HERMES_WEBUI_RUNTIME_ADAPTER`` | Yes | Set to ``agent-runs`` |
| ``HERMES_WEBUI_AGENT_RUNS_BASE_URL`` | Yes | Base URL of the Hermes Agent runtime API (e.g. ``http://127.0.0.1:8642``) |
| ``HERMES_WEBUI_AGENT_RUNS_API_KEY`` | No | Bearer token for agent runtime authentication |

### Agent API contract

| Adapter method | HTTP call |
|---|---|
| ``start_run`` | ``POST {base_url}/v1/runs`` |
| ``get_status`` | ``GET {base_url}/v1/runs/{run_id}`` |
| ``observe_events`` | ``GET {base_url}/v1/runs/{run_id}/events?after_seq=&limit=`` |
| ``cancel_run`` | ``POST {base_url}/v1/runs/{run_id}/stop`` |
| ``resolve_approval`` | ``POST {base_url}/v1/runs/{run_id}/approval`` |
| ``resolve_clarify`` | ``POST {base_url}/v1/runs/{run_id}/clarify`` |

### Error mapping

| Condition | Error code | Retryable |
|---|---|---|
| Connection refused / unreachable | ``agent_runtime_unreachable`` | Yes |
| Timeout | ``agent_runtime_timeout`` | Yes |
| HTTP 401 / 403 | ``agent_runtime_auth_error`` | No |
| Malformed JSON / contract mismatch | ``agent_runtime_bad_response`` | Yes |
| not_supported response | ``not_supported`` | No |

All error responses are redacted: no API keys, Authorization headers, tokens,
or stack traces are exposed.

### Implementation

- ``api/runtime_adapters/__init__.py`` — adapter factory, singleton, env-driven selection
- ``api/runtime_adapters/agent_runs.py`` — ``AgentRunsClient`` (HTTP transport) and ``AgentRunsAdapter`` (protocol translator)
- ``api/runtime_adapter.py`` — extended with ``agent-runs`` mode and helper functions
- ``api/runtime_routes.py`` — wired to delegate to agent-runs adapter when mode is active

### Deferred integration

- ``/api/chat/start`` is **not** routed through the agent-runs adapter in this
  phase. The legacy chat-start path remains unchanged.
- Live Hermes Agent HTTP smoke is deferred because the Agent Phase 4 route
  module is not yet mounted into a live server.
