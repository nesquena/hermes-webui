# RFC: WebUI Pending Intent Controls

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-05-28
- **Tracking issue:** [#3058](https://github.com/nesquena/hermes-webui/issues/3058)
- **Parent RFC:** [`live-to-final-assistant-replies.md`](live-to-final-assistant-replies.md) / [#3400](https://github.com/nesquena/hermes-webui/issues/3400)
- **Related issues/PRs:** [#2555](https://github.com/nesquena/hermes-webui/issues/2555), [#3108](https://github.com/nesquena/hermes-webui/issues/3108), [#3808](https://github.com/nesquena/hermes-webui/issues/3808), [#3822](https://github.com/nesquena/hermes-webui/pull/3822), [NousResearch/hermes-agent#28172](https://github.com/NousResearch/hermes-agent/issues/28172)
- **Related docs:** [`webui-run-state-consistency-contract.md`](webui-run-state-consistency-contract.md), [`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md), [`turn-journal.md`](turn-journal.md)

## RFC Positioning

This RFC is a child RFC under the Hermes WebUI Live-to-Final product line.

The Live-to-Final parent RFC defines how a running agent turn moves from live
process, tool activity, recovery state, and replay into a settled final answer
or terminal outcome.

This RFC covers one specific part of that model: what user input means when the
agent is already busy. In that state, a new user input must be interpreted
clearly as Queue, Steer, or Stop-and-send.

This RFC does not redefine the whole Live-to-Final model. It defines the product
semantics for user intervention during an active run.

## Problem

Hermes WebUI currently treats user input during a running agent turn as one of
three busy modes: Queue, Interrupt, or Steer. These look like equivalent send
modes, but they are not the same kind of action.

Queue means: wait, then send this as the next normal user turn.

Steer means: guide the currently running turn without starting a new turn.

Interrupt is currently a compound behavior: queue the message, cancel the
current run, then drain the queued message as the next turn. It is not a simple
send mode.

This mixed model has caused real failures:

- Queue can be lost across refresh, session switch, or restore paths.
- Steer can be transient and hard to trace.
- Interrupt-and-send can race with cancellation, so WebUI shows a user message
  that never reaches the Agent durable conversation chain.
- The composer primary button is ambiguous while busy.

This RFC defines a clearer pending-intent model for user input while the agent
is busy.

## Terms

### Queue

Queue means the user submitted a message while the agent is running, but the
message does not affect the current run. It waits until the current run ends,
then sends as the next normal user turn.

Queue belongs to the current session.

### Steer

Steer means the user submitted mid-run guidance. It is delivered to the current
active run so the run can use it in later processing.

In the UI, Steer should render like a normal user message. In runtime semantics,
it is not the next normal user turn. It is mid-run input for the active run.

Steer belongs to the current active run.

### Stop

Stop means the user stops the current active run. Stop by itself does not send a
new message.

If the current session has a waiting Queue message, Stop may be upgraded into
Stop-and-send.

If the current run has already received a Steer, Stop only stops that run. The
delivered Steer does not automatically become Queue and is not sent as the next
turn.

### Stop-and-send

Stop-and-send means there is already a waiting queued message, and the user
chooses to stop the current run and send that queued message as the next normal
user turn.

Stop-and-send is an upgrade action for Queue.

### Interrupt

Interrupt is a legacy term from the current implementation and older UI copy. It
was previously treated as a default busy send mode, but its behavior is closer
to Queue + Cancel + Send.

This RFC no longer defines Interrupt as a user-facing default busy mode. If the
term remains, it should be legacy/internal wording or an implementation detail
of Stop-and-send.

## Settings Model

Current Settings expose `Busy input mode` with three choices:

- Queue follow-up
- Interrupt current turn
- Steer mid-turn correction

This puts Queue, Interrupt, and Steer at the same level, as if they were
equivalent default send modes.

This RFC proposes replacing that with:

- Queue by default
- Steer by default

The setting should only decide what happens when the agent is running and the
user directly sends new input: either queue the input, or deliver it as Steer.

`Interrupt current turn` should no longer appear as a default busy mode. Its
capability is represented by Stop-and-send when a waiting Queue message exists.

Legacy migration:

- current `queue` -> `Queue by default`
- current `steer` -> `Steer by default`
- current `interrupt` -> `Queue by default`

The reason is that old `interrupt` combined two behaviors: it preserved the new
input as the next user turn and cancelled the active run. Migrating those users
to Steer would preserve urgency, but it would drop the cancel-and-send part of
their old intent. Queue by default is the safer migration because it never
injects a message into the active run without an explicit Steer action.

If the user explicitly wants to stop the current run and send a waiting queued
message, they can use Stop-and-send. Stop-and-send is the explicit replacement
for the old cancel-and-send behavior that `interrupt` tried to provide.

## Composer Interaction Model

When the agent is idle, Composer keeps the existing behavior:

- input has content: primary button is Send
- input is empty: primary button is disabled

When the agent is busy:

- input has content and default is Queue: primary button queues the message
- input has content and default is Steer: primary button sends Steer
- input is empty: primary button is Stop

Queued messages should appear as a compact strip/card attached above Composer,
reusing the existing Terminal collapsed-card pattern.

The queued strip should:

- sit directly above Composer
- show truncated message text with `...` when needed
- stay compact for mobile
- avoid large text buttons

The right side should expose icon controls:

- Delete: trash icon
- Edit: pencil icon
- Steer: dedicated steer icon

Edit moves the queued message back into Composer for editing.

Steer upgrades the queued message into Steer. After success, it disappears from
the queued strip and appears in the transcript/live area as a user Steer
message.

Open questions:

- When a waiting Queue exists, should the existing Stop button remain plain
  Stop, or should tooltip/state explain Stop-and-send behavior?
- Should Stop-and-send remain tied to the primary Stop affordance, or does it
  need a separate scoped affordance later?
- The current preference is not to change the Stop icon in the first version.

## Steer Live-to-Final Rendering

Steer is not only a temporary live UI state. It must preserve meaning in both
live and settled views.

### Live phase

Steer renders like a normal user message.

It visually inserts into the middle of the current assistant live run. It splits
the visible Assistant Worklog, but does not stop the active run. Assistant
Worklog continues after the Steer.

The Worklog should show explicit feedback, such as "guidance received," or
equivalent natural assistant process text.

A Steer message is user-visible because it is user-authored input, not an
internal recovery or runtime-control prompt. It renders like a user message, but
it remains metadata-marked as Steer so replay and settled Worklog do not treat
it as a normal next-turn user message.

### Settled phase

Steer must not disappear after Final Answer appears.

The collapsed Activity / Worklog must retain Steer as a timeline boundary.
Expanding Worklog should show what happened before Steer, what the user sent,
what happened after Steer, and how the final answer followed.

A delivered Steer is not a running-only marker. It is part of the causal
timeline of the active run, so it must remain visible or inspectable after
settle.

Steer must not exist only as a toast or transient DOM state. It must survive
refresh, session switch, replay, and settled render.

### Relationship to system control events

Steer is similar to system-delivered control events such as tool-iteration-limit
notices. The difference is source:

- Steer is user-delivered.
- Tool iteration limit is system-delivered.

Both are causal timeline events. Neither is the final answer. This RFC only
defines the user-delivered Steer side; tool-limit terminal semantics remain
owned by the Live-to-Final / terminal-state track.

## State Ownership

Queue belongs to the session.

A queued message must stay bound to the session where it was created. It must
not drain into another session after session switch, refresh, or background
completion.

Queue is waiting intent until drained. It should not render as an already-sent
normal user message before dispatch.

Steer belongs to the active run.

Once delivered, Steer is no longer Queue. It cannot be edited, deleted as
pending Queue, or used for Stop-and-send.

Steer renders like a user message, but metadata must preserve that it is Steer,
not a next-turn user message.

Stop belongs to the active run.

Stop only stops the active run unless the interaction clearly becomes
Stop-and-send.

Stop-and-send involves two objects:

- the waiting Queue message, owned by the session
- the active run cancellation, owned by the active run

It must preserve order:

1. mark the queued message as the next user turn
2. request cancellation of the active run
3. wait until the session is safe
4. send the queued message
5. never show a user message in WebUI that the Agent durable conversation did
   not receive

## Durability And Recovery

Pending intent must not be only current-page UI state.

Queue requirements:

- survives refresh
- survives session switch
- restores above Composer when returning to the session
- drains only into its original session
- respects Edit/Delete after recovery
- protects zero-message sessions that still contain draft or queued intent

Steer requirements:

- survives session switch
- survives refresh/replay
- remains inspectable after settled render
- does not duplicate as two user messages
- does not remain editable as Queue after delivery
- if delivery fails, preserves user input through Queue fallback or visible
  failure state

Stop-and-send requirements:

- queued message must not be lost during cancel
- old run must reach a clear terminal state
- exactly one successor run owns the queued message
- queued message must exist in WebUI transcript and Agent durable conversation
- if the session is not safe yet, the message must remain pending instead of
  forcing a successor run

Replay should rebuild the same causal timeline:

1. run start
2. Worklog
3. user intervention
4. runtime acknowledgement or handling
5. terminal state
6. final answer or error state

Recovery after replay does not need to reproduce every live animation, but it
must preserve event order and meaning.

## Leftover Steer

Leftover Steer is a Steer that could not be consumed by the active run before
that run ended.

The RFC distinguishes three cases:

- Not delivered: preserve the text through Queue fallback or visible failure.
- Delivered but returned by runtime as leftover: convert it into Queue with
  source metadata.
- Delivered but not proven applied: show only delivered, not applied.

WebUI must not silently discard leftover Steer. It also must not claim the
runtime applied the Steer without Agent/TUI Gateway evidence.

## Runtime Boundary

This RFC defines product semantics, not a permanent WebUI-private runtime
protocol.

This boundary is necessary because Hermes WebUI also has the #1925
RuntimeAdapter direction. The RuntimeAdapter contract moves WebUI toward clearer
runtime interfaces instead of permanently owning all Agent runtime behavior
directly. Hermes Desktop / TUI Gateway already exposes related surfaces such as
`session.steer`, `session.interrupt`, active session status, and event streams.

WebUI may own:

- pending intent presentation
- session-scoped Queue state
- queued message edit/delete/upgrade controls
- Steer rendering as user message
- WebUI-confirmed delivered state
- Stop / Stop-and-send presentation
- fallback, recovery, replay, and settled Worklog rendering

WebUI should not invent:

- a long-lived server-side queue scheduler
- runtime-level applied detection
- proof that Agent consumed a Steer
- a private interrupt model independent of Hermes Agent / TUI Gateway
- a control protocol that cannot map to RuntimeAdapter later

Delivered vs Applied:

- `delivered`: WebUI has sent Steer to the active run/runtime endpoint
- `applied`: Agent/runtime proves the model consumed or applied the Steer

WebUI may claim delivered. WebUI must not claim applied without Agent/TUI
Gateway evidence.

## Implementation Slices

Each slice must satisfy this RFC and the Live-to-Final parent requirements
around live rendering, settled Worklog, replay/recovery, and final answer
boundary.

### Slice 1: RFC and routing update

- update #3061
- define Queue / Steer / Stop / Stop-and-send / legacy Interrupt
- update docs/contracts routing
- clarify #3822 is a legacy interrupt safety fix, not endorsement of Interrupt
  as default busy mode

### Slice 2: Queue durability

- address #3108
- preserve queue and draft across refresh, session switch, tab restore, and
  zero-message restore

### Slice 3: Steer delivered visibility and replay

- render Steer as a user message
- keep active run alive
- preserve Steer in replay and settled Worklog
- claim delivered only, not applied

### Slice 4: Stop-and-send UI

- replace legacy Interrupt behavior with explicit Stop-and-send
- define Stop behavior when a waiting Queue exists
- ensure delivered Steer is not resent by Stop-and-send
- protect against cancel-race split-brain

### Slice 5: Agent-side Steer applied / tool boundary

- address #2555
- define when remaining tool calls stop or continue after Steer
- preserve Steer traceability
- only show applied after Agent/TUI Gateway emits proof

## Testing Expectations

Implementation PRs should include focused tests for:

- queued message stays session-scoped across switches
- queue survives refresh, tab restore, and zero-message draft restore
- queue drains only into its original session
- Queue can upgrade to Steer while still waiting, and the upgrade is
  irreversible
- accepted Steer renders as a user message in the live transcript
- accepted Steer remains visible after session switch and replay
- accepted Steer is not duplicated as a normal next-turn user message
- accepted Steer is not still editable as Queue
- delivered Steer remains inspectable in settled Activity / Worklog
- leftover Steer converts to Queue with source metadata
- Stop on a waiting Queue message cancels the current run and sends the queued
  message
- delivered Steer is not resent by Stop-and-send
- stop-and-send replacement survives cancel/reconnect timing
- stale or unavailable Steer fallback preserves user text and explains the
  fallback
- settled render and live SSE replay produce one coherent timeline

Manual verification should cover desktop, narrow, and mobile composer states
when the visible queue/steer/stop surfaces change.

## Issue / PR Routing

- [#3400](https://github.com/nesquena/hermes-webui/issues/3400): Live-to-Final
  umbrella. Tracks live-to-final invariants affected by pending intent, but does
  not own Queue/Steer/Stop-and-send command semantics.
- [#3058](https://github.com/nesquena/hermes-webui/issues/3058): Pending intent
  controls umbrella.
- [#3061](https://github.com/nesquena/hermes-webui/pull/3061): RFC PR for this
  contract.
- [#3108](https://github.com/nesquena/hermes-webui/issues/3108): Queue/draft
  durability.
- [#2555](https://github.com/nesquena/hermes-webui/issues/2555): Steer runtime
  behavior, tool-call boundary, traceability, applied signal.
- [#3808](https://github.com/nesquena/hermes-webui/issues/3808): legacy
  interrupt-and-send ownership bug.
- [#3822](https://github.com/nesquena/hermes-webui/pull/3822): focused safety
  fix for #3808. It does not preserve Interrupt as default busy mode and does
  not complete pending intent controls.
- [#1925](https://github.com/nesquena/hermes-webui/issues/1925):
  RuntimeAdapter / runtime ownership boundary.
- Hermes Agent / TUI Gateway follow-up: required for reliable applied/consumed
  Steer evidence.

## Open Questions

- When a waiting Queue exists, should the existing Stop button remain plain
  Stop, or should tooltip/state explain Stop-and-send behavior?
- Should Stop-and-send remain tied to the primary Stop affordance, or does it
  need a separate scoped affordance later?
- What steer icon best communicates "guide current run" without looking like
  Send or fast-forward?
- Should mobile hide Edit behind a menu, keeping only Delete and Steer visible?
  Or should all three icons stay visible in the attached queued strip?
- What exact Hermes Agent event shape, ordering, and metadata should prove
  `applied` after WebUI has already recorded `delivered`?
