# RFC: WebUI Turn Journal for Crash-Safe Chat Submissions

- **Status:** Proposed
- **Author:** @ai-ag2026
- **Created:** 2026-05-11

## Problem

A WebUI chat turn crosses several durability boundaries:

1. browser submits a user message,
2. WebUI creates or updates session runtime metadata,
3. the agent worker starts streaming,
4. assistant output is appended,
5. the JSON sidecar and derived index are saved.

If the server crashes between submission and the final sidecar save, recovery has to infer what happened from `pending_user_message`, `active_stream_id`, `.json.bak`, `_index.json`, and `state.db`. Those safeguards are useful, but they are still reconstructing intent after the fact.

The missing primitive is a small write-ahead journal for turns: record the submitted user turn durably before the worker starts, then advance the journal as the turn progresses.

## Goals

- Preserve the exact user-submitted turn, including attachments metadata, before any provider or worker work starts.
- Make crash recovery deterministic: a submitted-but-unfinished turn can be reported or reconstructed without guessing.
- Keep the journal append/update format simple enough for startup recovery, CLI audit, and future API repair endpoints.
- Avoid turning recovery into a background daemon. This is storage hygiene, not a long-running service.

## Non-goals

- Replacing `state.db.sessions` or WebUI JSON sidecars.
- Journaling every token or every SSE event.
- Replaying tool calls or provider streams.
- Automatically inventing assistant messages after ambiguous crashes.

## Proposed storage

Use one JSONL file per session under the existing WebUI state area:

```text
<SESSION_DIR>/_turn_journal/<session_id>.jsonl
```

Each line is an immutable event. Recovery can scan by `turn_id` and choose the latest status.

### Event shape

```json
{
  "version": 1,
  "event": "submitted",
  "turn_id": "20260511T001122Z-abcdef",
  "session_id": "abc123",
  "stream_id": "stream-xyz",
  "created_at": 1778458282.123,
  "role": "user",
  "content": "...",
  "attachments": [],
  "workspace": "/workspace",
  "model": "openai/gpt-5",
  "model_provider": "openai"
}
```

Later events for the same `turn_id`:

```json
{"version":1,"event":"worker_started","turn_id":"...","created_at":1778458283.0}
{"version":1,"event":"assistant_started","turn_id":"...","created_at":1778458284.0}
{"version":1,"event":"completed","turn_id":"...","created_at":1778458299.0,"assistant_message_index":12}
{"version":1,"event":"interrupted","turn_id":"...","created_at":1778458301.0,"reason":"server_startup_recovery"}
```

## Turn state machine

```text
submitted -> worker_started -> assistant_started -> completed
submitted -> interrupted
worker_started -> interrupted
assistant_started -> interrupted
```

`completed` is terminal. `interrupted` is terminal unless a later explicit repair creates a new turn. Recovery should not silently resume a provider call.

## Write rules

1. On `/api/chat/start` or equivalent turn-submission path:
   - generate `turn_id`,
   - append `submitted`,
   - fsync the journal file,
   - only then start the worker.
2. When worker thread enters `_run_agent_streaming`, append `worker_started`.
3. When assistant output is first persisted or clearly begins, append `assistant_started`.
4. After the sidecar save that includes the assistant answer succeeds, append `completed`.
5. On cancellation or known worker exception, append `interrupted` with a reason.

## Startup recovery semantics

On startup, for each journal file:

- Latest event is `completed`: no action.
- Latest event is `submitted` or `worker_started` and no matching user message exists in sidecar:
  - append/recover the user message into the session sidecar with a recovery marker.
- Latest event is `submitted`, `worker_started`, or `assistant_started` and no completed assistant turn exists:
  - add a visible interruption marker, not a fake assistant answer.
- Existing `.json.bak` and `state.db` recovery still run first so the sidecar is as complete as possible before journal reconciliation.

## Audit additions

`audit_session_recovery()` can report:

- `turn_journal_pending_turn` — repairable if the user message is absent from sidecar.
- `turn_journal_interrupted_turn` — ok/warn depending on whether a visible marker exists.
- `turn_journal_malformed_event` — manual review.

Safe repair should only materialize submitted user messages and interruption markers when the journal event content is valid JSON and the target message is absent.

## API surface

Initial read-only endpoint can be folded into the existing recovery audit:

```text
GET /api/session/recovery/audit
```

Later, if needed:

```text
GET /api/session/turn-journal?session_id=<id>
```

The latter should be diagnostic-only and redact or omit large attachment payloads.

## Rollout plan

1. Land backup/sidecar recovery and audit primitives.
2. Add this journal writer in the turn-submission path behind no config flag; it is local-only and append-only.
3. Add read-only audit reporting for pending journal turns.
4. Add safe repair for missing user messages and interruption markers.
5. Once stable, consider pruning completed journal entries older than a retention window, but only after sidecar/index recovery has no findings.

## Open questions

- Exact place to define `turn_id` so browser retry and server retry do not duplicate the same user message.
- Whether attachment files need their own durable manifest entry or whether metadata-only is enough for v1.
- How much of the assistant partial output, if any, should be recoverable after `assistant_started` but before `completed`.
- Whether completed journal entries should be compacted into a per-session checkpoint file.

## Minimal implementation slice

The first implementation PR should be deliberately small:

- helper: `append_turn_journal_event(session_id, event)`
- helper: `read_turn_journal(session_id)`
- unit tests for atomic append, malformed-line tolerance, and state derivation
- one call site: append `submitted` before worker start
- audit-only report of pending journal turns

Do **not** combine the first implementation with replay/repair. Replay is where most of the bugs in WAL systems live; ship the writer and audit first, prove the format, then add repair.
