# Hermes Run Adapter Contract and Migration Gates

- **Status:** Proposed
- **Author:** @Michaelyklam
- **Updated by:** @franksong2702
- **Created:** 2026-05-11
- **Revised:** 2026-05-17
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
accepted before any implementation PR for this adapter direction changes the
streaming hot path, introduces a runner process, or moves a new approval /
clarify / queue / goal control path. Narrow current-path bug fixes that do not
introduce a new runtime boundary can still proceed under the WebUI run-state
consistency contract and the relevant issue scope.

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

## Current Gate State — 2026-05-19

Slice 1 is now past the first active validation gate:

- #2283 shipped the run-journal replay layer in v0.51.71.
- A 100-trial synthetic replay/restart validation pass against current
  `origin/master` passed on 2026-05-16. The matrix covered completed-run replay,
  interrupted stale-pending recovery, fresh-pending grace handling, StreamChannel
  reconnect ordering, duplicate-prevention merge behavior, many-session recovery,
  large-journal derivation, and stream-to-turn-id lifecycle linking.
- The focused regression set
  `tests/test_turn_journal.py tests/test_turn_journal_lifecycle.py tests/test_stale_stream_pending_recovery.py`
  also passed on the same worktree.
- #2393, shipped through v0.51.76, capped live chat token SSE transports to the
  selected conversation pane. Background sessions now rely on existing
  status/replay/reattach behavior instead of keeping one live `/api/chat/stream`
  EventSource per active session.

This evidence did not prove the future runner/sidecar path. It did unblock the
adapter-seam work:

- #2416 shipped the Slice 2 RuntimeAdapter seam contract.
- #2424 shipped the default-off `legacy-journal` RuntimeAdapter seam in
  v0.51.81.
- #2438 shipped the response-shape parity follow-up in v0.51.83, keeping the
  adapter flag from expanding `/api/chat/start`'s public JSON contract.
- #2469 shipped the Slice 3a cancel-control gate in v0.51.85.
- #2479 shipped the first Slice 3a implementation in v0.51.86, routing Stop
  Generation through `RuntimeAdapter.cancel_run(...)` only when
  `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal` is enabled.
- #2487 shipped the Slice 3b approval/clarify gate, and #2496 shipped approval /
  clarify response routing through the adapter seam in v0.51.89.
- #2509 shipped the Slice 3c queue/continue + goal gate in v0.51.90.
- #2544 shipped the first Slice 3c implementation in v0.51.91. The goal
  route now uses `RuntimeAdapter.update_goal(...)` only when
  `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal` is enabled, while preserving the
  legacy-direct response shape and leaving post-turn goal evaluation in the
  existing agent loop.
- #2560 shipped the queue-staging clarification in v0.51.92. The RFC now treats
  `queue_message(...)` as a staged protocol method only; `/queue` remains
  browser-side queue/drain behavior, and no server-side queue endpoint or queue
  scheduler should be added merely for adapter symmetry.
- #2575 shipped the Slice 4a runner/sidecar contract gate in v0.51.93.
- #2599 shipped the Slice 4b `RunnerRuntimeAdapter` facade in v0.51.94. The
  facade normalizes an injected runner client's start / observe / status /
  control responses without owning `AIAgent`, streams, cancellation flags,
  approval queues, clarify queues, goal state, or cached-agent tables.
- The next implementation gate is a feature-flagged runner backend plus
  restart/reattach harness. It must stay default-off, keep legacy fallback
  intact, pass explicit profile/workspace/model payloads instead of mutating
  WebUI process globals, and avoid recreating `STREAMS` / `CANCEL_FLAGS` /
  approval queues / clarify queues under new names.

The next gate is runner-backend plumbing, not queue implementation
by default. Queue / continue routing should only move before Slice 4 if a future
maintainer decision identifies an existing server-side legacy entry point and
pins its response shape, ordering, and idempotency contract. Otherwise, keeping
`queue_message(...)` staged is the honest boundary while execution ownership
moves out of the main WebUI request process.

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

Status as of 2026-05-17: shipped. PR #2416 defined the adapter-seam contract,
PR #2424 added the default-off `LegacyJournalRuntimeAdapter` seam, and PR #2438
kept `/api/chat/start` response shape identical between `legacy-direct` and the
flagged `legacy-journal` path. Slice 2 remains a reversible boundary change, not
a sidecar or execution-ownership move.

Scope:

- introduce the `RuntimeAdapter` interface only after Slice 1 proves replay,
- implement the first backend as a thin facade over the still-legacy path plus
  journal,
- keep the browser event contract stable,
- keep controls routed to existing code until a later control-specific slice.

Revert path: switch the feature flag back to direct legacy path.

#### Slice 2 interface contract

The Slice 2 seam should introduce a deliberately small `RuntimeAdapter` boundary
without changing the execution backend. The first implementation is a
`LegacyJournalRuntimeAdapter` that delegates to the existing WebUI-owned
streaming path and reads the Slice 1 journal for status/replay. That makes the
adapter a protocol translator over the current backend, not a new runtime owner.

Minimal interface shape:

```python
class RuntimeAdapter:
    def start_run(self, request: StartRunRequest) -> RunStartResult: ...
    def observe_run(self, run_id: str, *, cursor: str | None = None) -> RunEventStream: ...
    def get_run(self, run_id: str) -> RunStatus: ...
    def cancel_run(self, run_id: str) -> ControlResult: ...
    def respond_approval(self, run_id: str, approval_id: str, choice: str) -> ControlResult: ...
    def respond_clarify(self, run_id: str, clarify_id: str, response: str) -> ControlResult: ...
    def queue_message(self, run_id: str, message: str, *, mode: str = "queue") -> ControlResult: ...
    def update_goal(
        self,
        session_id: str,
        action: Literal["set", "pause", "resume", "clear", "status", "edit"],
        text: str | None = None,
    ) -> ControlResult: ...
```

`queue_message` is named for the legacy queued-message payload shape: it accepts
follow-up chat text rather than arbitrary runtime input. The method name does not
require a new HTTP route. Today `/queue` is primarily browser-side queue/drain
behavior; the adapter method enters the protocol so a later queue/continue slice
has a typed control surface, but route wiring remains deliberately staged until
the exact legacy entry point and ordering/idempotency contract are explicit.

For `update_goal`, the `action` argument is the bounded adapter capability label.
During the legacy-journal slice, the legacy goal parser still receives the full
`text` payload and remains authoritative for details such as the body of
`set <goal text>`. Future slices must not route goal semantics from `action`
alone; doing so would drop the goal body and change `/api/goal` behavior.

Required data classes / payload fields:

| Type | Required fields | Notes |
|---|---|---|
| `StartRunRequest` | `session_id`, `message`, `attachments`, `workspace`, `profile`, `provider`, `model`, `toolsets`, `source`, `metadata` | Mirrors current `/api/chat/start` inputs without introducing new behavior. |
| `RunStartResult` | `run_id`, `session_id`, `stream_id`, `status`, `started_at`, `cursor`, `active_controls` | `stream_id` may remain the legacy stream id during Slice 2. |
| `RunStatus` | `run_id`, `session_id`, `status`, `last_event_id`, `terminal_state`, `active_controls`, `pending_approval_id`, `pending_clarify_id` | Backed by live legacy state plus journal/session metadata. |
| `RunEventStream` | ordered events matching Artifact 1, resumable from cursor | Can be implemented by existing SSE + journal replay at first. |
| `ControlResult` | `accepted`, `status`, `event_id`, `safe_message`, optional internal `payload` | Controls may still call existing handlers in Slice 2. Public HTTP responses must not leak adapter-only fields unless a later RFC expands them. |

The interface is intentionally narrower than a runner. It does not own `AIAgent`,
tool execution, callback queues, cancellation flags, approval callbacks, or
clarify callbacks in Slice 2. Those remain in the legacy path until their
individual migration slices.

#### Slice 2 feature flag and revert contract

Slice 2 should be guarded by one WebUI-local setting/environment flag, for
example `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal` with default
`legacy-direct` until the seam is proven. The flag selects only the route/adapter
entry point:

```text
legacy-direct   -> current /api/chat/start and /api/chat/stream path
legacy-journal  -> RuntimeAdapter facade over the same legacy execution path + journal
```

Reverting must be operationally boring:

1. set the flag back to `legacy-direct`,
2. restart WebUI if needed,
3. existing session transcripts and journal files remain readable,
4. no migration or data deletion is required.

The PR that introduces the seam should include a source-level regression that
the default path remains `legacy-direct` and that the adapter flag is the only
way the new entry point is selected.

#### Slice 2 backend mapping

| Adapter method | Slice 2 backend | Explicit non-goal |
|---|---|---|
| `start_run` | call the existing chat-start preparation and legacy `_run_agent_streaming` path | do not move `AIAgent` construction or thread ownership |
| `observe_run` | combine existing live SSE fan-out with journal replay cursor semantics | do not build a second renderer or event protocol |
| `get_run` | derive status from session metadata, live stream presence, and journal terminal state | do not make `STREAMS` authoritative for durable run existence |
| `cancel_run` | delegate to existing cancel handler/control path | do not redesign cancellation semantics yet |
| `respond_approval` | delegate to existing approval response path | do not persist approval callbacks in the main server as a new adapter-owned queue |
| `respond_clarify` | delegate to existing clarify response path | do not persist clarify callbacks in the main server as a new adapter-owned queue |
| `queue_message` | delegate to existing queue/continue path when that slice is accepted | do not invent a parallel continuation buffer or run scheduler |
| `update_goal` | delegate to existing goal command/control path when that slice is accepted | do not move goal evaluation or continuation ownership into the adapter |

Any implementation that needs a new long-lived queue, agent cache, cancellation
registry, or callback registry inside the main WebUI process is out of scope for
Slice 2 and should become a spec amendment before code lands.

#### Slice 2 acceptance tests

Before the seam is enabled by default, tests should prove at least:

- the `RuntimeAdapter` interface exists and all methods are implemented by the
  `LegacyJournalRuntimeAdapter`;
- the default route remains the legacy direct path unless the adapter flag is
  explicitly enabled;
- `start_run` returns the same browser-facing `stream_id` / `session_id` shape as
  `/api/chat/start` for a synthetic request;
- `observe_run(..., cursor=...)` preserves the existing journal replay ordering
  and duplicate-prevention behavior;
- `get_run` distinguishes live stream, completed, failed, cancelled, and stale /
  interrupted states using current live state plus journal/session metadata;
- `cancel_run` delegates to the current cancellation path and still emits one
  terminal result;
- approval and clarify methods are present but documented as delegated legacy
  controls until their migration slices;
- disabling the flag returns to the old route path without changing session or
  journal data.

These tests are adapter-seam tests, not runner-survives-restart tests. The
execution-survives-WebUI-restart gate remains deferred to Slice 4.

### Slice 3: Control migration

Status as of 2026-05-18: Slice 3a cancel routing shipped in v0.51.86 via #2479,
Slice 3b approval/clarify routing shipped in v0.51.89 via #2496 / #2507, and
the Slice 3c queue/continue + goal gate shipped in v0.51.90 via #2509.
Cancel was the smallest control-plane migration because it already had one clear
browser affordance, one active-run target, and an existing legacy handler to
delegate to. Approval and clarify then proved the same protocol-translator shape
for user-mediated callback controls. Queue/continue and goal are the final
pre-runner control migration because they can change run lifecycle semantics
rather than just resolve an already-pending control.

Scope:

- move cancel first,
- then approval,
- then clarify,
- then queue/continue and goal controls,
- each control gets its own acceptance tests and rollback path.

Revert path: per-control feature flags or route-level fallback to legacy control
handlers.

#### Slice 3a: Cancel control gate

The first control migration should route Stop Generation through the
`RuntimeAdapter.cancel_run(...)` seam while preserving the current legacy cancel
semantics. It should not introduce a new cancellation registry, worker-owned
signal table, or sidecar boundary. During this slice, `cancel_run` is still a
protocol translator over the existing cancellation path.

Acceptance properties:

1. **Same visible result as legacy Stop Generation.** A cancelled turn still
   emits one terminal cancelled/interrupted state and preserves any already
   streamed partial assistant content according to the existing cancellation
   contract.
2. **Adapter flag is behavior-preserving.** With
   `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal`, Stop Generation uses
   `RuntimeAdapter.cancel_run(...)`; with the default `legacy-direct` path, the
   current route remains available as fallback.
3. **No new runtime-surrogate state.** The implementation must not add a second
   `CANCEL_FLAGS`-like map, cached `AIAgent` table, long-lived queue, or local
   callback registry inside the main WebUI process.
4. **Journal/status coherence.** After cancellation, replay and session reload
   classify the turn as cancelled/interrupted rather than stale/unknown, and the
   terminal state is visible through the same journal/session diagnostic surface
   used by Slice 1.
5. **Idempotent duplicate cancel.** Repeating cancel for the same run should be
   safe: one terminal result is recorded, later attempts return a bounded
   `ControlResult` such as `not-active` rather than creating extra terminal
   events or resurrecting stale stream state.

Suggested regression coverage:

- a route/source test proving the flagged cancel path calls the adapter seam and
  the default path remains the legacy route;
- an adapter unit test proving `cancel_run` delegates exactly once and returns a
  bounded `ControlResult` for unsupported/not-active runs;
- an existing cancellation preservation suite run (for example the partial-output
  and cancelled-turn status tests) under the adapter flag or an equivalent
  synthetic harness;
- a replay/session-load assertion that a cancelled run's terminal state remains
  classifiable from the journal/session surface after reload.

Non-goals for Slice 3a:

- no approval or clarify migration;
- no queue/continue or goal migration;
- no runner process, sidecar, or execution-survives-WebUI-restart claim;
- no public `/api/chat/start` response-shape expansion for adapter-only fields.

#### Slice 3b: Approval and clarify control gate

The next control migration should cover approval and clarify together as one
gate, but not necessarily one implementation commit. They are distinct browser
widgets, but architecturally they share the same high-risk shape: the agent loop
pauses on a live callback, the browser presents a user-mediated decision, and the
runtime must resume from a bounded response without orphaning callback state.

During Slice 3b, `RuntimeAdapter.respond_approval(...)` and
`RuntimeAdapter.respond_clarify(...)` remain protocol translators over the
existing legacy callback paths. They must not create a second approval queue,
clarify queue, callback registry, pending-prompt table, or runner-owned wait loop
inside the main WebUI process.

Acceptance properties:

1. **Same visible result as legacy approval / clarify.** Existing approval cards,
   clarify prompts, choices, denial paths, and resumed-agent behavior remain
   unchanged for users. The adapter flag changes only the route/control entry
   point.
2. **Stable response contracts.** Existing approval and clarify HTTP endpoints
   keep their current browser-facing response shapes. Adapter-only fields such as
   internal status strings, callback ids, or active-control metadata must not leak
   into public responses unless a later RFC explicitly expands the contract.
3. **Bounded missing-prompt behavior.** Responding to a non-existent, already
   resolved, stale, or expired approval/clarify id returns a bounded
   `ControlResult` such as `not-active` / `expired` / `unsupported`; it must not
   block the request, recreate a callback, or synthesize a success path.
4. **Replayable request and resolution events.** Approval/clarify request and
   resolution events remain journal-visible so reload/reconnect can show the last
   safe state. Slice 3b does not have to make pending approvals survive a WebUI
   process restart while execution is still in-process; that property belongs to
   the runner/sidecar gate.
5. **No new runtime-surrogate state.** The implementation must not add new
   process-local global maps, long-lived queues, or callback registries under
   adapter-specific names. If the existing legacy callback path needs more state
   to satisfy the route, stop and amend this RFC before landing code.
6. **Idempotent duplicate responses.** Repeating the same approve/deny/clarify
   response is safe: the runtime accepts at most one response for the pending
   request, records one resolution event, and later attempts return bounded
   not-active/expired status without resuming the run twice.

Suggested regression coverage:

- route/source tests proving flagged approval and clarify response paths call the
  adapter seam while the default path remains the existing legacy handler;
- adapter unit tests proving `respond_approval` and `respond_clarify` delegate
  exactly once, return accepted/not-active/unsupported `ControlResult` values, and
  never expose unsafe internal strings to the browser response;
- journal/session-load assertions that request and resolution events remain
  replayable and renderable after reconnect;
- duplicate-response tests for approval and clarify ids;
- existing approval/clarify UI/static tests under default legacy mode to prove no
  browser contract drift.

Non-goals for Slice 3b:

- no queue/continue or goal migration;
- no runner process, sidecar, or execution-survives-WebUI-restart claim;
- no persistence of pending approval/clarify callbacks outside the current legacy
  callback model;
- no change to approval risk classification, allowed choices, or clarify prompt
  UX;
- no public chat-start/status response-shape expansion for adapter-only fields.

#### Slice 3c: Queue/continue and goal control gate

The next control migration should specify queue/continue and goal before any code
routes those actions through `RuntimeAdapter`. They may ship as separate
implementation PRs, but they should share one gate because both affect what the
agent does after the current user turn instead of merely resolving a pending
prompt. Queue/continue controls append or schedule follow-up input against live
or resumable work; goal controls set, pause, resume, clear, or inspect a
standing cross-turn objective. Both can accidentally create a second continuation
model if WebUI buffers or evaluates them independently.

During Slice 3c, `RuntimeAdapter.queue_message(...)` and
`RuntimeAdapter.update_goal(...)` should remain protocol translators over the
existing legacy queue/goal paths. They must not create a WebUI-owned run queue,
goal evaluator, continuation scheduler, agent loop, or sidecar substitute inside
the main WebUI process.

`RuntimeAdapter.update_goal(...)` controls goal state mutations only. Post-turn
goal evaluation and the decision to continue remain in the existing agent
conversation loop until the later runner/sidecar slice moves execution
ownership; Slice 3c must not move that evaluator into WebUI or the adapter.

Acceptance properties:

1. **Same visible result as legacy queue/continue and goal.** Existing `/queue`
   and `/goal` semantics, browser status affordances, paused/resumed states, and
   post-turn continuation behavior remain unchanged for users. The adapter flag
   changes only the route/control entry point.
2. **Stable response contracts.** Existing queue/continue and goal HTTP or
   command responses keep their current browser-facing shapes. Adapter-only run
   metadata, internal status, or capability details must not leak into public
   responses unless a later RFC explicitly expands the contract.
3. **Bounded unavailable-control behavior.** Requests for a missing run,
   unsupported profile, inactive session, paused/cleared goal, or stale queued
   continuation return bounded `ControlResult` states such as `not-active`,
   `unsupported`, or `conflict`; they must not create a phantom run, resurrect a
   dead stream, or silently enqueue work against the wrong session.
4. **Replayable lifecycle/status evidence.** Queue/continue submission, goal
   status changes, and resulting post-turn continuation decisions remain visible
   through the journal/session diagnostic surface where the legacy path already
   emits equivalent state. Slice 3c does not have to make queued follow-ups or
   goals survive a WebUI process restart while execution is still in-process;
   that stronger property belongs to the runner/sidecar gate.
5. **No new runtime-surrogate state.** The implementation must not add a second
   process-local queue, goal table, scheduler, cached-agent registry, or
   continuation loop under adapter-specific names. If the existing legacy path
   cannot support the route without new ownership state, stop and amend this RFC
   before landing code.
6. **Ordering and idempotency are explicit.** Repeating the same queue/continue
   request should not duplicate follow-up work unless the legacy path already
   defines that behavior. Goal pause/resume/clear/status operations should be
   safe to repeat and should report one coherent state.

Suggested regression coverage:

- route/source tests proving flagged queue/continue and goal paths call the
  adapter seam while the default path remains the existing legacy handler;
- adapter unit tests proving `queue_message` and `update_goal` delegate exactly
  once, return accepted/not-active/unsupported/conflict `ControlResult` values,
  and do not expose unsafe internal strings to browser responses;
- ordering/idempotency tests for repeated queue/continue and repeated goal
  pause/resume/clear/status operations;
- journal/session-load assertions that queue/goal state remains diagnosable after
  reconnect where the legacy path currently emits state;
- existing queue/goal UI/static tests under default legacy mode to prove no
  browser contract drift.

Non-goals for Slice 3c:

- no runner process, sidecar, or execution-survives-WebUI-restart claim;
- no durable WebUI-owned queue or goal scheduler;
- no migration of `AIAgent` construction, post-turn goal evaluation, or the
  agent continuation loop out of the legacy path;
- no change to `/goal` command semantics, queue ordering semantics, or supported
  capability metadata;
- no public chat-start/status response-shape expansion for adapter-only fields.

### Slice 4: Runner process / sidecar boundary

Slice 4 is the first gate that may move active execution ownership out of the
main WebUI request process. It should start as a docs/test contract PR before any
runner code lands. Slice 1's journal/replay layer has shipped and passed active
validation, Slice 2's default-off adapter seam has shipped, and Slice 3's
cancel/approval/clarify/goal control routing has proven the protocol-translator
pattern. Queue remains staged unless maintainers explicitly ask for a separate
pre-runner queue route.

The Slice 4 implementation must not make the adapter a new runtime surrogate.
The runner boundary may own active execution, process supervision, run lifecycle,
and callback state, but those responsibilities must be centralized behind the
adapter/runner contract rather than recreated as scattered globals in the main
WebUI server.

Scope:

- move long-lived execution out of the main WebUI request process,
- runner owns active execution state,
- main WebUI server observes/replays through the adapter/journal,
- future Hermes CLI/Python/local API or `/v1/runs` backends can be evaluated
  behind the adapter.

Revert path: disable runner backend and fall back to journaled legacy backend.

#### Slice 4a: Runner contract gate

Before runner code lands, define a narrow contract that covers:

1. **Backend selection and rollback.** The existing `legacy-direct` and
   `legacy-journal` paths remain available. Any new runner backend is
   feature-flagged, default-off, and revertible by switching the adapter mode back
   to `legacy-journal` without deleting sessions or journal files.
2. **Process ownership.** The runner, not the main WebUI request process, owns
   `AIAgent` construction/reuse, active run execution, cancellation flags,
   approval/clarify callback wait state, and post-turn continuation evaluation
   for runs assigned to that backend.
3. **Durable observation.** The main WebUI server observes through
   `RuntimeAdapter.observe_run(...)`, `get_run(...)`, and the journal cursor. A
   WebUI restart must not be required for the runner to finish writing ordered
   events and terminal state.
4. **Restart/reattach success criterion.** Start a long-running run, restart only
   `hermes-webui.service`, reload the session, rediscover the active or terminal
   runner-owned run, replay/catch up from cursor without duplicate transcript /
   tool / reasoning state, and preserve cancel if the run is still active.
5. **Control parity.** Cancel, approval, clarify, goal status/control, and any
   accepted queue/continue behavior route through adapter methods with stable
   browser response shapes. Unsupported controls return bounded `ControlResult`
   states instead of silently falling back to stale in-process state.
6. **Profile/workspace isolation.** Runner startup receives explicit profile,
   workspace, attachments, model/provider, toolset, and source metadata rather
   than relying on process-global environment mutation in the WebUI server.

Suggested contract tests before implementation:

- source/RFC tests proving Slice 4 remains feature-flagged and default-off;
- a fake-runner adapter test that simulates WebUI restart by discarding server
  process-local state while preserving runner/journal state, then verifies
  `get_run` and replay recover the same terminal state;
- a control-parity fixture proving unsupported runner controls return bounded
  `ControlResult` values and do not fall back to legacy `STREAMS` /
  `CANCEL_FLAGS` state;
- a profile/workspace payload test proving runner requests carry explicit context
  fields without mutating global `os.environ` in the main WebUI process.

Non-goals for Slice 4a:

- no removal of the legacy in-process backend;
- no default-on runner mode;
- no public chat-start/status response-shape expansion;
- no new server-side queue endpoint or scheduler just for adapter symmetry;
- no dependency on Hermes Agent shipping `/v1/runs` before WebUI can validate the
  local runner boundary.

#### Slice 4b: Runner adapter client facade

Status as of 2026-05-20: shipped in v0.51.94 via #2599.

The first code slice after the Slice 4a contract should be a small
`RunnerRuntimeAdapter` facade that delegates to an injected runner client. This
is still not the runner process itself. Its job is to pin the adapter-facing
normalization rules before route wiring or process supervision lands:

- `start_run` forwards a `StartRunRequest` carrying explicit session, profile,
  workspace, attachments, model/provider, toolset, source, and metadata payloads;
- `observe_run` and `get_run` normalize runner responses into `RunEventStream`
  and `RunStatus` so a recreated WebUI server can observe the same runner-owned
  state without relying on process-local `STREAMS`;
- controls normalize accepted / not-active / unsupported outcomes into bounded
  `ControlResult` values;
- the facade itself owns no `AIAgent`, worker thread, cancellation registry,
  approval queue, clarify queue, goal scheduler, or server-side queue.

The implementation remains default-off until a later slice adds an actual runner
client/backend and explicit route selection.

#### Slice 4c: Feature-flagged runner backend and restart/reattach harness

Status as of PR #TBD: implementation proposed. The code adds a default-off
`runner-local` adapter selection point plus factory wiring for an injected runner
client, while keeping live browser chat routes on the legacy backend. The
restart/reattach harness remains synthetic/fake-runner based until a later slice
introduces a supervised runner process.

After the facade exists, the next narrow implementation slice should add a real
runner-client/backend selection point and a synthetic restart/reattach harness,
without routing normal browser chat to that backend yet.

Scope:

- add a concrete runner-client factory behind an explicit mode such as
  `HERMES_WEBUI_RUNTIME_ADAPTER=runner-local`, while preserving `legacy-direct`
  and `legacy-journal` as the default/revert paths;
- validate that `StartRunRequest` carries explicit session, profile, workspace,
  attachments, provider/model, toolset, source, and metadata fields into the
  runner boundary without relying on WebUI process-global environment mutation;
- prove a recreated WebUI adapter can rediscover runner-owned status and replay
  ordered events from the runner/journal surface after process-local state is
  discarded;
- keep controls bounded through `ControlResult` values, with unsupported controls
  returning `unsupported` / `not-active` rather than falling back to stale legacy
  `STREAMS` or callback queues;
- keep the live `/api/chat/start` path on the legacy backend until the runner
  backend has a passing restart/reattach harness and maintainer approval to wire
  route selection.

Acceptance tests for Slice 4c:

1. **Default-off selection.** `legacy-direct` remains the default; `runner-local`
   or any later runner mode is selected only by an explicit feature flag.
2. **No route-shape drift.** Adding the runner backend does not expand public
   `/api/chat/start`, cancel, approval, clarify, goal, or status response shapes
   while the route remains legacy-backed.
3. **Restart/reattach harness.** A fake or local runner fixture can start a run,
   discard the first WebUI adapter instance, recreate the adapter, and still
   observe ordered events plus terminal/live status from durable runner-owned
   state.
4. **Control bounds.** Cancel / approval / clarify / queue / goal controls route
   through the runner client only when the runner backend is selected, and
   unsupported controls return bounded `ControlResult` values without consulting
   legacy process-local state.
5. **No runtime-surrogate globals.** The main WebUI server must not gain new
   module-level maps for runner-owned streams, cancellation flags, pending
   approval/clarify callbacks, cached agents, or goal/queue schedulers.

Non-goals for Slice 4c:

- no default-on runner backend;
- no removal of the legacy in-process backend;
- no public response-shape expansion;
- no live chat route switch to the runner backend before the restart/reattach
  harness is reviewed;
- no server-side queue endpoint or queue scheduler just for adapter symmetry.

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
