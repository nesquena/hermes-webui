# Hermes Run Adapter Contract and Migration Gates

- **Status:** Proposed
- **Author:** @Michaelyklam
- **Updated by:** @franksong2702
- **Created:** 2026-05-11
- **Revised:** 2026-05-14
- **Tracking issue:** [#1925](https://github.com/nesquena/hermes-webui/issues/1925)

## Credit and Scope

This RFC codifies the direction discussed in #1925. It does not introduce an
implementation. The central guardrail comes from Michael Lam's review framing:

> the adapter should be a protocol translator, not a runtime surrogate.

The product boundary from #1925 is:

> WebUI should be thin in execution ownership, not thin in product scope.

That means WebUI remains the full browser workbench for sessions, workspace
files, chat rendering, tool cards, approvals, status, diagnostics, and controls.
The change is that long-lived execution ownership should move behind an explicit
runtime boundary instead of remaining scattered through the main WebUI request
process.

This document is intentionally a reviewable spec and migration gate. It should be
accepted before any implementation PR changes the streaming hot path, introduces a
runner process, or moves cancellation / approval / clarify control flow.

## Problem

Browser-originated chat turns are still executed inside the WebUI server process.
The current path creates process-local stream state, starts background agent
threads, constructs or reuses `AIAgent`, and owns callback state for token, tool,
reasoning, approval, clarify, cancellation, and terminal events.

That shape works, but it makes the WebUI process the owner of active runtime
truth. Consequences include:

- restarting WebUI can orphan active work,
- reconnect depends on process-local state rather than a durable run/event view,
- cancellation and stale writeback bugs recur around ownership boundaries,
- approvals and clarify prompts are tied to live callbacks,
- future Hermes runtime APIs cannot be adopted cleanly because WebUI lacks a
  single adapter boundary.

The immediate goal is not to build a sidecar. The immediate goal is to define the
browser contract, classify current runtime state, and gate the first reversible
journal slice.

## Goals

- Preserve the current rich WebUI workbench experience.
- Make the browser-facing event/control contract explicit.
- Classify every current runtime-owned state primitive as `runner process`,
  `journal`, `adapter API surface`, or `WebUI presentation cache`.
- Identify future backend mapping: existing Hermes runtime API, missing Hermes
  API, or temporary WebUI compatibility shim.
- Define acceptance tests that must survive any migration.
- Define reversible implementation slices, starting with an append-only
  in-process event journal / replay layer.

## Non-goals

- Do not implement the adapter in this RFC.
- Do not introduce a runner process or sidecar in the first implementation slice.
- Do not change `_run_agent_streaming` control flow in the first journal slice.
- Do not recreate `STREAMS`, cached `AIAgent` objects, callback queues, or
  cancellation flags under new names.
- Do not reduce WebUI product scope or move normal workbench UX out of WebUI.
- Do not depend on Hermes Agent shipping a WebUI-specific runtime connector before
  WebUI can improve its own boundary.

## Artifact 1: Browser Event and Control Contract

This is the compatibility contract the browser depends on, regardless of whether
the backend is today's in-process streaming path, an in-process journaled path, a
future WebUI-managed runner, or a future Hermes `/v1/runs` backend.

The current inventory should be derived from `static/messages.js` consumers and
SSE/event production in `api/streaming.py`. Future edits to those files should
update this RFC or the implementation contract that replaces it.

### Event Envelope

Every replayable runtime event should be representable with:

```json
{
  "event_id": "run_123:42",
  "seq": 42,
  "run_id": "run_123",
  "session_id": "20260514_...",
  "type": "tool.updated",
  "created_at": 1778750000.0,
  "terminal": false,
  "payload": {}
}
```

Required semantics:

- `seq` is monotonic per run.
- `event_id` is stable enough to use as an SSE `id:` value or equivalent cursor.
- Reconnect supports `Last-Event-ID` or `after_seq`.
- Replay is at-least-once; WebUI deduplicates by `run_id` + `seq` or `event_id`.
- Terminal runs can replay their final `done`, `cancelled`, or `error` state.

### Event Families

| Event family | Required payload | Browser responsibility | Runtime source of truth |
|---|---|---|---|
| `run.started` / `status` | lifecycle state, controls available, session id, workspace/profile/model/toolset summary | render active state and controls | runtime run state |
| `token.delta` | assistant message id or segment id, delta text, content type | append visible assistant text | runtime model output stream |
| `reasoning.delta` / `reasoning.done` | reasoning block id, delta/final text, visibility metadata | render thinking/progress UI | runtime reasoning events |
| `progress` | concise phase/status text, optional tool context | render activity/progress text | runtime progress callbacks |
| `tool.started` | tool call id, name, sanitized arguments, start time | open/update tool card | runtime tool lifecycle |
| `tool.updated` | stdout/stderr/structured partial data, progress metadata | update tool card | runtime tool lifecycle |
| `tool.done` | result, status/exit code, duration, error flag | finalize tool card | runtime tool lifecycle |
| `approval.requested` | approval id, action summary, risk metadata, available choices | show approval widget | runtime approval state |
| `approval.resolved` | approval id, choice, resulting status | close/update approval widget | runtime approval state |
| `clarify.requested` | clarify id, question, choices/input mode | show clarify widget | runtime clarify state |
| `clarify.resolved` | clarify id, answer metadata/status | close/update clarify widget | runtime clarify state |
| `title.updated` | title text, source/confidence | update title surfaces | session/title subsystem |
| `usage.updated` / `usage.final` | tokens, cost, model/provider, duration where available | update usage surfaces | runtime usage accounting |
| `error` | stable error code, safe message, redacted diagnostics, terminal flag | render error and final state | runtime terminal/error state |
| `done` | final lifecycle state, usage, terminal result/error summary, last seq | finalize run UI | runtime terminal state |

### Reconnect Metadata

Every active or terminal run must expose:

- `run_id`
- `session_id`
- `status`: `queued`, `running`, `awaiting_approval`, `awaiting_clarify`,
  `paused`, `cancelling`, `cancelled`, `failed`, `completed`, or `expired`
- last committed event cursor / `last_event_id`
- terminal state and final result/error when finished
- currently available controls
- pending approval/clarify ids, if any
- session-to-active-run mapping for the current WebUI session

### Controls

| Control | Required semantics | Target owner |
|---|---|---|
| observe | attach to live events and replay from cursor | adapter API surface backed by runtime/journal |
| status | poll lifecycle state when SSE/WebSocket is unavailable | adapter API surface backed by runtime/journal |
| cancel | request graceful cancellation; terminal event follows | runner/runtime control plane |
| queue / continue | append follow-up work according to Hermes semantics | runner/runtime control plane |
| approval | resolve pending approval by id with supported choices | runner/runtime control plane |
| clarify | answer pending clarify request by id | runner/runtime control plane |
| goal | set/status/pause/resume/clear goal where capability exists | runtime command/capability plane |

WebUI may keep presentation state such as expanded rows, selected tabs, and local
scroll position. WebUI must not privately mutate runtime truth for these controls.

## Artifact 2: Runtime State Inventory and Classifier

Classifications:

- `runner process`: should be owned by the eventual execution runner / runtime
  backend, not the main WebUI request process.
- `journal`: should be captured in append-only durable events for replay and
  diagnostics.
- `adapter API surface`: should be exposed through a WebUI-owned boundary that
  can later switch backend implementations.
- `WebUI presentation cache`: may remain local because it is not execution truth.

| Current primitive | Current legacy source of truth | Target classification | Future backend mapping | Slice 1 handling | Notes / gap |
|---|---|---|---|---|---|
| `STREAMS` / `STREAMS_LOCK` | `api.state_sync` process memory | adapter API surface + presentation fan-out | WebUI runner or future Hermes run observation API | keep live path; mirror events into journal | Must stop being authoritative for active run existence. |
| `CANCEL_FLAGS` | `api.state_sync` process memory | runner process | cancel/interrupt endpoint or runner control | no control-flow change | Final cancel state must return as a replayable event. |
| cached `AIAgent` objects / `AGENT_INSTANCES` | `api/config.py` process memory | runner process | runner-owned Hermes integration | unchanged | Moving this is deferred until after journal proof. |
| background thread lifecycle | `_run_agent_streaming` in `api/streaming.py` | runner process | runner-owned execution lifecycle | unchanged | Slice 1 must not rewrite thread/control flow. |
| token / partial text buffers | streaming callbacks and browser SSE state | journal + presentation cache | replayable runtime events | append emitted events | Browser can cache rendered state, but replay must rebuild it. |
| reasoning buffers | streaming callbacks and UI rendering state | journal + presentation cache | replayable reasoning events | append emitted events | Thinking cards must survive reconnect. |
| tool buffers / live tool calls | WebUI streaming callbacks | journal + presentation cache | replayable tool lifecycle events | append emitted events | WebUI owns rendering, not tool execution state. |
| approval callbacks / queues | live Python callbacks | runner process + adapter API surface + journal | approval state/control endpoint | journal request/resolution events only | Pending approval must eventually survive WebUI restart. |
| clarify callbacks / queues | live Python callbacks | runner process + adapter API surface + journal | clarify state/control endpoint | journal request/resolution events only | Pending clarify must eventually survive WebUI restart. |
| per-request `HERMES_HOME` env mutation lock | `api/streaming.py` / config helpers | runner process | runner/profile execution context | unchanged | Long-term runner must isolate profile env without process-global mutation. |
| session-to-active-run mapping | session JSON + active stream ids + memory | journal + adapter API surface | runtime run registry/session mapping | journal run metadata | Reopen session must discover active/completed run. |
| title generation state | WebUI callbacks/session saves | journal + presentation cache | runtime/session title event | append title events | WebUI may display title updates after event receipt. |
| usage accounting state | WebUI callbacks/session saves | journal + presentation cache | runtime usage event/source of truth | append usage events | Avoid divergent WebUI-only accounting. |
| command capability metadata | WebUI command registry + Hermes command assumptions | adapter API surface | runtime command/capability metadata | unchanged | Unknown command support should not be guessed by WebUI. |
| voice mode state | browser/UI + streaming path | presentation cache + adapter API surface | runtime input/control capability | unchanged | Acceptance tests must pin voice behavior before migration. |
| project/workspace context | WebUI session/workspace state + env mutation | adapter API surface + runner process | runtime run context | unchanged | Must preserve workspace-aware chat and project context. |

Unclassified state is a design blocker. If an implementation slice discovers a
runtime primitive that does not fit this table, update the RFC before landing code.

## Artifact 3: Acceptance Test Catalog

These are the user-observable behaviors that must survive the migration. The
catalog should become automated tests where practical. Where full automation is
not feasible in the first slice, the PR must include the strongest practical
diagnostic or manual validation plan.

| Behavior | Acceptance criterion | Why it matters | First slice that must prove it |
|---|---|---|---|
| Journal replay after refresh/reconnect | reconnect or restart after events have been journaled can replay from cursor without duplicate transcript/tool/reasoning state | proves the browser contract is replayable and duplicate-safe | journal/replay slice |
| Terminal replay | completed/failed/cancelled runs replay terminal state and do not duplicate transcript content | prevents stale spinner and duplicate-message regressions | journal/replay slice |
| Interrupted/stale run diagnostics | if WebUI restarts while execution is still owned by the WebUI process, replay shows the last journaled state and a clear interrupted/stale diagnostic instead of pretending the run kept executing | keeps slice 1 honest before a runner exists | journal/replay slice |
| Execution survives WebUI restart | active execution outlives the main WebUI process, reconnect discovers the active run, ordered replay catches up, and controls such as cancel still work | proves execution ownership actually moved out of the request process | runner/sidecar or external-runtime slice |
| Cancel during tool call | cancel emits one terminal cancelled state and no stale writeback | catches historical stream ownership races | control migration slice |
| Cancel during reasoning | partial/reasoning content is preserved cleanly and final state is not provider-error | catches cancellation classification regressions | control migration slice |
| Approval request/response | approval survives observation, browser response reaches runtime, result is replayable | approval callbacks are cross-cutting and easy to orphan | approval migration slice |
| Clarify request/response | clarify survives observation, browser response reaches runtime, result is replayable | same risk as approval, different UI/control path | clarify migration slice |
| Slash commands | `/compress`, `/branch`, `/retry`, and other supported commands keep current semantics | command behavior should not be reimplemented ad hoc | command capability slice |
| Model switch mid-session | provider/model changes route through the correct runtime context | prevents provider/source-of-truth drift | adapter control slice |
| Workspace context | run receives the session workspace and attachments context | preserves workbench value | adapter control slice |
| Multi-profile isolation | profile-specific runs write/read the correct Hermes home and memory | protects #2134-family isolation concerns | runner/profile slice |
| Queue/continue | follow-up input during live/resumable work obeys Hermes semantics | prevents parallel continuation model | control migration slice |
| Goal continuation | goal status/control survives the adapter boundary | goal logic is lifecycle-sensitive | goal capability slice |
| Voice mode | voice-originated input uses the same run/event/control contract | prevents alternate input path drift | adapter parity slice |
| Projects context | project metadata remains visible and correct across run replay | preserves session/workbench organization | adapter parity slice |

## Artifact 4: Slicing Plan and Reversibility

### Slice 0: Spec PR

Scope:

- this RFC update,
- no runtime behavior change,
- no streaming hot-path code change.

Revert path: revert the docs PR.

### Slice 1: Append-only journal/replay beside the legacy path

Pre-authorized only after this spec is reviewed and accepted in #1925.

Scope:

- add an append-only event journal alongside existing callback paths,
- capture the event families in Artifact 1,
- persist run metadata, cursor, terminal state, and safe diagnostic fields,
- allow reconnect to replay from a cursor and then continue live observation,
- keep `_run_agent_streaming` control flow unchanged,
- keep cancellation, approval, clarify, queue, and goal behavior unchanged.

Non-goals:

- no runner process,
- no sidecar,
- no adapter interface that changes control flow,
- no replacement of `STREAMS` as the live delivery path,
- no speculative rewrite of agent construction/caching.

Revert path:

- disable journal writes/replay behind one small integration seam,
- retain legacy WebUI streaming path unchanged.

Success criterion:

1. Start a non-trivial WebUI run.
2. Refresh/reconnect the browser, or restart WebUI after events have already been
   journaled.
3. Rediscover the run from journal metadata.
4. Replay from cursor without duplicate visible transcript content.
5. Render the same already-journaled token/reasoning/tool/status/terminal state
   the workbench would have rendered without the reconnect.
6. If WebUI restarted while execution was still owned by the WebUI process, show
   an explicit interrupted/stale diagnostic rather than claiming the active run
   kept executing.

### Slice 2: Adapter interface over the journaled legacy path

Scope:

- introduce the `RuntimeAdapter` interface only after Slice 1 proves replay,
- implement the first backend as a thin facade over the still-legacy path plus
  journal,
- keep the browser event contract stable,
- keep controls routed to existing code until a later control-specific slice.

Revert path: switch the feature flag back to direct legacy path.

### Slice 3: Control migration

Scope:

- move cancel first,
- then approval,
- then clarify,
- then queue/continue and goal controls,
- each control gets its own acceptance tests and rollback path.

Revert path: per-control feature flags or route-level fallback to legacy control
handlers.

### Slice 4: Runner process / sidecar boundary

Explicitly deferred until Slice 1 has worked in production for at least one
release cycle and the adapter surface has review approval.

Scope:

- move long-lived execution out of the main WebUI request process,
- runner owns active execution state,
- main WebUI server observes/replays through the adapter/journal,
- future Hermes CLI/Python/local API or `/v1/runs` backends can be evaluated
  behind the adapter.

Revert path: disable runner backend and fall back to journaled legacy backend.

## First Meaningful Success Criteria

The first meaningful milestones are deliberately split.

### Journal / Replay Gate

This gate belongs to Slice 1. It does not prove active execution survives a WebUI
process restart, because execution is still owned by the WebUI process in this
slice.

It proves:

1. A WebUI run emits append-only journal events with stable cursors.
2. Browser refresh/reconnect can replay already-journaled events from cursor.
3. Terminal `done`, `error`, or `cancelled` state replays without duplicate
   transcript content.
4. Tool/reasoning/status state can be reconstructed from replayed journal events.
5. If WebUI restarts before execution ownership has moved out of process, the UI
   can show a clear interrupted/stale diagnostic for the last journaled run state.

### Execution-Survives-WebUI-Restart Gate

This stronger gate belongs to the runner/sidecar or external-runtime slice, not
Slice 1. It proves execution ownership has actually moved out of the main WebUI
request process:

1. Start a long-running run from WebUI.
2. Restart only `hermes-webui`.
3. Keep the active run executing outside the restarted WebUI process.
4. Reload the browser/session.
5. Rediscover the active run and replay/catch up from cursor.
6. Preserve the rendered workbench state without duplicate transcript content.
7. If the run is still active, cancellation still works.

If this works without moving runtime ownership into a new pile of process-local
globals, the architecture is moving in the right direction.

## Open Questions

- What exact storage format should Slice 1 use: SQLite run/event tables, JSONL,
  or a hybrid with transcript-derived checkpoints?
- How long should event replay be retained after terminal state?
- Which event fields must be redacted before journal persistence?
- Should the journal live under the WebUI state dir, the session dir, or a
  future runtime-specific subdirectory?
- What is the minimum set of synthetic event fixtures needed to compare legacy
  rendering with replay rendering?
- Which controls need route-level feature flags before migration?
- If Hermes Agent later ships a durable `/v1/runs` API, which adapter fields map
  directly and which remain WebUI presentation concerns?
