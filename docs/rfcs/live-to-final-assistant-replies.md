# Live-to-Final Assistant Replies for Long-Running Agent Sessions

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-06-03
- **Tracking issue:** [#3400](https://github.com/nesquena/hermes-webui/issues/3400)

## Background

This RFC is anchored on long-running agent sessions.

Short conversations are useful sanity checks, but they do not exercise the
hardest browser-agent states. A long-running session can spend minutes waiting,
make many tool calls, produce a long final answer, cross context-pressure
boundaries, hit tool-call or retry limits, lose network continuity, and still
need to recover into a readable final transcript.

The product model should therefore be defined against the long-running case. A
short conversation should be the same lifecycle with fewer events, not a
separate UI model.

## Problem

Hermes WebUI currently uses one chat surface to represent several different
things:

- the assistant's live process text while work is still running,
- tool activity and lifecycle status that support that work,
- recovery or replay state after refresh, reconnect, or session switching,
- the final answer after the turn settles.

Those meanings have repeatedly competed for the same visual space. The result is
that some long-running sessions feel noisy, some look silent while the agent is
working, some recover into a different shape after reconnect, and some terminal
edge cases can appear completed even when no final answer was produced.

This RFC defines the product semantics for that lifecycle.

## Public Issue Signals

The public issue history already shows the same problem recurring from several
directions. The table below uses representative examples; the broader inventory
lives in [#3400](https://github.com/nesquena/hermes-webui/issues/3400).

| Signal | Examples | Product implication |
| --- | --- | --- |
| Working output and final answer can blur together | [#536](https://github.com/nesquena/hermes-webui/issues/536) | Running process and final answer need separate semantics. |
| Compression state is hard to represent cleanly | [#469](https://github.com/nesquena/hermes-webui/issues/469), [#2973](https://github.com/nesquena/hermes-webui/issues/2973), [#3079](https://github.com/nesquena/hermes-webui/issues/3079) | Context compression should be visible while useful, but not become final transcript content. |
| Replay, reconnect, and session switching can lose active context | [#2283](https://github.com/nesquena/hermes-webui/issues/2283), [#2924](https://github.com/nesquena/hermes-webui/issues/2924), [#3391](https://github.com/nesquena/hermes-webui/issues/3391) | A recovered session should rebuild the same reply lifecycle as the live render. |
| Tool, activity, thinking, and progress rendering can become noisy or silent | [#1298](https://github.com/nesquena/hermes-webui/issues/1298), [#3014](https://github.com/nesquena/hermes-webui/issues/3014), [#3015](https://github.com/nesquena/hermes-webui/issues/3015) | Process text should stay primary; tool activity should remain supporting detail. |
| Terminal turns can end without a real final answer | [#3315](https://github.com/nesquena/hermes-webui/issues/3315), [#3316](https://github.com/nesquena/hermes-webui/issues/3316) | No-final, compression-exhausted, and tool-limit outcomes need explicit terminal states. |
| Stream ownership and cancellation affect what the user sees | [#3344](https://github.com/nesquena/hermes-webui/issues/3344), [#3345](https://github.com/nesquena/hermes-webui/issues/3345) | One visible turn must own its live, terminal, and final events. |
| Session awareness affects live work visibility | [#856](https://github.com/nesquena/hermes-webui/issues/856), [#1370](https://github.com/nesquena/hermes-webui/issues/1370), [#1436](https://github.com/nesquena/hermes-webui/issues/1436) | Sidebar/session state must not contradict the visible active session. |
| Busy input changes live-session control | [#720](https://github.com/nesquena/hermes-webui/issues/720), [#965](https://github.com/nesquena/hermes-webui/pull/965), [#1062](https://github.com/nesquena/hermes-webui/pull/1062) | Queue, steer, and interrupt are adjacent controls for long-running sessions, but command-level behavior belongs to a separate control-surface contract. |

## Goals

- Define the product model for assistant replies in long-running sessions.
- Make live process text, tool activity, lifecycle status, and the final answer
  share one coherent turn lifecycle.
- Preserve the same lifecycle through replay, reconnect, refresh, and session
  switching.
- Name terminal outcomes honestly when the run does not produce a normal final
  answer.
- Define which long-running edge cases belong to the first slice and which
  should be handled by later slices.

## Non-goals

This RFC does not define:

- pixel-level styling,
- provider/model selection,
- command-level queue/steer/interrupt behavior,
- a new runtime adapter, storage format, or SSE protocol,
- a backend tool-event schema change such as a shared display-title field.

## Product Model

### Live phase

While a turn is running, the assistant reply should read as a live process
narrative.

Requirements:

- Process text is the primary timeline.
- Tool activity is visible but visually quieter than process text.
- Tool rows and tool groups are collapsed by default.
- Full commands, arguments, raw output, and large payloads stay behind deeper
  disclosure.
- The run timer/status belongs with the active live turn, not as a top
  transcript artifact.
- Running-only lifecycle markers are transient.
- Internal recovery/control messages do not become visible chat content.

### Settled phase

When the turn settles, implementation detail should collapse without swallowing
the final answer.

Requirements:

- A compact activity summary appears above the final answer.
- The activity summary is collapsed by default.
- Expanding it reveals readable process history and tool history.
- Raw command/output detail remains behind a deeper disclosure.
- The final answer remains ordinary assistant prose below the summary.
- Running-only markers disappear from the settled transcript unless they explain
  a visible error or recovery outcome.
- Very long final answers remain complete and readable. They should not be
  hidden inside the activity summary or replaced by a progress/status artifact.

### Recovery and replay

Refresh, reconnect, session switching, and replay should preserve the same reply
model.

Requirements:

- Recovered sessions rebuild the same live/final structure used during live
  rendering.
- A reattached session must not silently switch to a different visual model.
- If the exact live scene cannot be reconstructed immediately, the UI should
  show an explicit restoring or degraded state instead of an empty running
  shell.
- Old in-progress browser state must not override durable session truth.
- Recovery/control events stay internal unless they describe a user-visible
  terminal outcome.

### Terminal outcomes

Every turn needs a terminal outcome. A turn without a final answer must not look
like a normal completed answer.

Required product states:

- **completed**: the assistant produced a final answer and the turn settled
  normally.
- **cancelled**: the user stopped the turn.
- **interrupted**: browser, stream, worker, or network continuity was lost
  before a final answer was produced.
- **compression exhausted**: context compression could not create enough room to
  continue safely.
- **tool limit reached**: the run hit a tool-call, retry, or iteration ceiling
  before a final answer was produced.
- **no response**: the provider or runtime returned no usable assistant content.
- **error**: fallback for failures that do not fit the above states.

Copy can evolve, but these semantic distinctions should stay stable in live
rendering, settled rendering, and replay.

When more than one terminal condition applies, the more specific condition
should win over the generic fallback. For example, cancelled, compression
exhausted, tool limit reached, and no response should not be flattened into a
plain error only because the turn also failed to produce a final answer.

## Long-Running Edge Cases

### Auto Compression

Auto Compression is a context lifecycle transition, not a tool call and not final
answer content.

Expected behavior:

- During live work, show compression as quiet transient status.
- When the run continues after compression, converge to a completed compression
  status such as `Context auto-compressed`.
- If one turn crosses the compression barrier more than once, each pass should
  remain understandable without turning compression into the main transcript.
- Do not keep compression status text in the settled transcript unless it
  explains an error or recovery state.
- Compression success in the UI does not by itself prove model-facing context
  was pruned; that remains a separate runtime/context invariant.

### Very long final answers

Long-running sessions can end with a final answer that is itself lengthy.

Expected behavior:

- The final answer remains the primary settled assistant content.
- Supporting activity stays above it and collapsed by default.
- Streaming and settle transitions should not jump the user away from the final
  answer or make the answer look like tool output.
- Any additional collapse, preview, or navigation affordance for very long final
  answers should preserve the full answer as ordinary assistant prose.

### Tool-call and retry ceilings

Long-running sessions can exhaust tool-call limits, retry budgets, or iteration
ceilings before a final answer is available.

Expected behavior:

- Treat these as explicit terminal outcomes, not as normal completion.
- Preserve the readable work history that led to the limit.
- Keep the final area honest: show that the run stopped because a limit was
  reached rather than inventing a final answer.
- Do not persist internal control prompts as ordinary user-visible transcript
  content.

### No-final and provider failure

Tool-heavy turns can end with tool output, provider failure, or no usable final
assistant message.

Expected behavior:

- Detect the absence of a final assistant answer at settle time.
- Surface a terminal state such as no response, interrupted, compression
  exhausted, tool limit reached, or error.
- Do not mark the turn completed only because some assistant/tool activity
  occurred earlier.

### Reconnect and session switch

Long-running work often outlives one browser attachment.

Expected behavior:

- Switching away and back should replay already-streamed process/tool history.
- Refresh and reconnect should preserve the active turn's identity.
- Slow rebuild should be visibly restoring or degraded, not blank.
- Sidebar/session metadata should not point the user at a stale or wrong active
  session.

### User intervention

During long-running work, the user may need to queue follow-up input, steer the
current direction, or interrupt the run.

Expected behavior:

- These controls should not corrupt the live-to-final reply lifecycle.
- Queue/steer/interrupt command semantics should be defined in a separate
  control-surface contract.
- This RFC only requires that live-session controls preserve clear ownership,
  terminal outcomes, and replayable state.

## Delivery Plan

### Slice 1: live-to-final reply lifecycle

The first implementation slice is represented by #3401. It should demonstrate
the core reply model:

- live process text is primary,
- tool activity is quiet and progressively disclosed,
- running-only compression status is transient,
- the settled activity summary appears above the final answer,
- settle-time rendering does not falsely present a no-final turn as completed,
- replay and reattach rebuild the same visible structure,
- stream ownership fixes are limited to preserving the visible turn's ownership,
  terminal events, and replay.

This slice should use `Refs #3400`; it should not close the umbrella issue.

### Slice 2: terminal and recovery stabilization

The next slice should close the edge cases that make long-running sessions look
misleading after they stop or recover:

- cancelled and interrupted final rendering,
- compression-exhausted terminal rendering,
- tool-limit / max-retry terminal rendering,
- no-final-answer provider failure classification,
- explicit restoring/degraded state during slow reattach,
- empty process placeholders that make tool-only runs look broken,
- live-vs-settled label clarity for tool activity.

### Slice 3: live-session control surface

The next adjacent product area is user intervention during live work:

- queue follow-up input while a turn is running,
- steer a live turn without losing ownership of the current reply,
- interrupt a live turn and preserve the user's corrective intent,
- define busy-input defaults and prompt visibility,
- ensure these controls replay and settle into the same terminal model.

This slice should reference the existing busy-input / CLI-parity history, but it
should be designed as a control-surface contract rather than as a reply-content
change.

### Slice 4: session and protocol integration

Broader integration work should stay separate from the reply-content model:

- native `Last-Event-ID` or equivalent reconnect cursor support,
- sidebar/session awareness for active long-running work,
- session-list disappearance or stale-session repair,
- shared tool display-title normalization across legacy live stream, persisted
  tool calls, replay, gateway paths, and future adapter/runner paths.

These are important follow-ups, but they should not be mixed into the first
reply-lifecycle implementation slice.

## Review Checklist

Use this checklist when reviewing PRs against this RFC:

- Does the change preserve long-running session readability?
- Does live process text stay primary over tool metadata?
- Are tool details available without becoming the main transcript?
- Does the final answer remain separate from supporting activity?
- Are compression, no-final, tool-limit, cancel, and interrupt outcomes
  classified honestly?
- Does reconnect/session switch rebuild the same reply lifecycle?
- Do internal recovery or control messages stay out of ordinary chat content?
- Is the PR's slice clear: lifecycle, terminal/recovery, live controls, or
  session/protocol integration?

## Relationship To Existing Contracts

This RFC sits above the current run-state and adapter contracts:

- [`webui-run-state-consistency-contract.md`](webui-run-state-consistency-contract.md)
  defines how transcript, context, stream, replay, compression, and session
  metadata stay coherent.
- [`canonical-session-resolution.md`](canonical-session-resolution.md) defines
  how URL, local browser state, sidebar rows, and compression lineage resolve to
  one visible session target.
- [`turn-journal.md`](turn-journal.md) defines crash-safe submitted-turn and
  interrupted-turn recovery semantics.
- [`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md) defines
  longer-term event/control ownership and migration gates.

This RFC defines the product meaning those lower-level contracts need to
preserve for long-running assistant replies.

## Open Questions

- Should very long final answers need additional navigation or preview
  affordances beyond the standard chat transcript behavior?
- Should repeated compression passes in one turn be shown as separate transient
  statuses or summarized into one compression lifecycle marker?
- Should queue, steer, and interrupt receive a dedicated public control-surface
  RFC, or should that contract live inside the existing adapter/control RFC?
