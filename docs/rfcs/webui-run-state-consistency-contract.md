# WebUI Run State Consistency Contract

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-05-16
- **Tracking issue:** [#2361](https://github.com/nesquena/hermes-webui/issues/2361)
- **Related architecture:** [#1925](https://github.com/nesquena/hermes-webui/issues/1925), [`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md)

## Problem

A single WebUI agent turn is represented by several overlapping state layers:

- the visible transcript the user can read,
- the model context / `context_messages` the agent actually receives,
- `pending_user_message` and active stream metadata,
- live SSE events and in-memory stream state,
- durable run journal / replay state,
- automatic compression summaries and active-task handoff text,
- the browser's live timeline DOM/cache,
- sidebar ordering, unread state, and `updated_at` metadata.

Those layers are not independent. When they drift apart, the user sees failures
that look unrelated: a prompt is visible but missing from recovered model
context, a live run loses or reorders thinking/tool cards after switching
sessions, cleanup makes old sessions look newly active, replay duplicates content,
or automatic compression reference material appears inside the active turn.

This RFC defines a consistency contract for those layers. It complements the
larger run adapter direction in #1925 by documenting what must remain coherent
while WebUI still has multiple overlapping state stores.

## Goals

- Define the state layers involved in active and recovered WebUI turns.
- Make the source-of-truth expectations explicit for each layer.
- Give reviewers a checklist for streaming, replay, compression, recovery,
  model-context, and sidebar changes.
- Map recent real issues to reusable invariants so future fixes do not solve the
  same class of bug one symptom at a time.

## Non-goals

- Do not implement a runner process, sidecar, or new runtime boundary here.
- Do not replace #1925 or the run adapter contract.
- Do not rewrite the streaming protocol in this RFC.
- Do not reopen already-fixed narrow bugs.
- Do not make this a catch-all for unrelated UI polish.

## State Layers

| Layer | Purpose | Source-of-truth expectation | Must not do |
|---|---|---|---|
| Visible transcript | Shows what the user and assistant said | Session transcript plus live replay should produce one chronological user-visible story | Hide the user turn that started active work, or show internal recovery text as current user intent |
| Model context / `context_messages` | Supplies conversation state to the agent | Must include the current visible user turn unless deliberately excluded with a user-visible reason | Let the agent resume from context that contradicts what the user can see |
| Pending turn metadata | Bridges submitted-but-not-yet-finalized user input | Must identify the user turn and stream that own active work | Become a permanent duplicate transcript row after recovery |
| Live stream / SSE | Delivers active runtime events to the browser | Must remain an observation path, not the only durable truth for already-emitted events | Lose the visible scene on refresh, reconnect, or session switch |
| Run journal / replay | Rebuilds emitted runtime events after reconnect or restart | Must be cursor-safe and idempotent | Duplicate assistant text, thinking text, tool cards, or compression cards |
| Compression summary / handoff | Gives the agent recovery context after automatic compression | Must remain agent-facing recovery material unless explicitly rendered as history | Pollute the active turn or become implicit current user intent |
| Live UI scene/cache | Preserves expanded rows, in-progress cards, local scroll, and transient grouping | May optimize presentation but must be rebuildable or degradable from transcript/replay | Become the only place where chronological ordering exists |
| Sidebar/session metadata | Helps the user find active and recent sessions | Must reflect meaningful user or assistant activity | Treat background cleanup as a fresh user-facing update |

## Core Invariants

1. **Visible current turns enter model context.** If the user can see a current
   prompt and WebUI asks the model to continue that work, the prompt must be in
   the reconstructed model context unless WebUI shows an explicit reason it was
   excluded.
2. **Active turn UI keeps its owner.** The user turn that started active work
   must remain visible before assistant text, thinking cards, tool cards, or
   activity groups that belong to that work.
3. **Reattach preserves order or degrades clearly.** Refresh, reconnect, and
   session switch must preserve chronological live-scene order. If WebUI cannot
   restore the exact live scene, it should downgrade to an explicit structured
   replay state instead of silently reordering content.
4. **Maintenance is not activity.** Runtime maintenance such as stale-stream
   cleanup, orphan repair, or background compression must not refresh sidebar
   ordering, unread markers, or active-session affordances as if the user or
   assistant just acted.
5. **Replay is idempotent.** Replaying a run from a cursor must not duplicate
   transcript rows, thinking content, interim assistant text, tool cards, or
   compression cards.
6. **Compression is not current intent.** Automatic compression summaries and
   reference cards are recovery/handoff material. They must not be treated as a
   new user request, active-turn content, or the default visible explanation for
   the current answer.
7. **Observation has a degraded path.** Long-running or many-session observation
   should expose enough heartbeat/degraded status that the UI does not appear
   silent and ordinary APIs do not stall behind active streams.
8. **Every mutation names its layer.** A PR touching streaming, recovery,
   context reconstruction, compression, replay, or sidebar metadata should state
   which layer it changes and what regression proves the invariant still holds.

## Review Checklist

Use this checklist for PRs that touch run state, streaming, replay, compression,
context reconstruction, or session metadata:

- Which state layers does this PR read or write?
- Which layer is the source of truth after this change?
- Can the visible transcript and model context diverge? If yes, is that
  deliberate and user-visible?
- What happens after browser refresh, session switch, SSE reconnect, and WebUI
  restart?
- Does replay rebuild the same scene without duplicates?
- Can this change move a session in the sidebar without meaningful user or
  assistant activity?
- Can automatic compression or recovery text become visible active-turn content?
- What test or manual evidence proves the invariant?

## Existing Issue Map

| Example | State boundary exposed | Relevant invariant |
|---|---|---|
| [#2341](https://github.com/nesquena/hermes-webui/issues/2341) / [#2342](https://github.com/nesquena/hermes-webui/pull/2342) | Active reattach could show agent activity without the pending user turn that started it | 2 |
| [#2344](https://github.com/nesquena/hermes-webui/issues/2344) / [#2347](https://github.com/nesquena/hermes-webui/pull/2347) | Session switching could lose or reorder the live thinking/tool/interim timeline | 3, 5 |
| [#2345](https://github.com/nesquena/hermes-webui/issues/2345) / [#2349](https://github.com/nesquena/hermes-webui/pull/2349) | Stale stream cleanup could mutate `updated_at` and resurface old sessions | 4 |
| [#2346](https://github.com/nesquena/hermes-webui/issues/2346) / [#2348](https://github.com/nesquena/hermes-webui/pull/2348) | Thinking cards could repeat interim assistant progress text | 5 |
| [#2353](https://github.com/nesquena/hermes-webui/issues/2353) / [#2354](https://github.com/nesquena/hermes-webui/pull/2354) | Recovered pending user turns could be visible but missing from model context | 1 |
| [#2355](https://github.com/nesquena/hermes-webui/issues/2355) / [#2357](https://github.com/nesquena/hermes-webui/pull/2357) | Auto-compression rotation could leave reference-only cards in the active conversation tail | 3, 6 |
| [#2308](https://github.com/nesquena/hermes-webui/issues/2308) / [#2309](https://github.com/nesquena/hermes-webui/pull/2309) | Compressed sessions could resume stale agent tasks when the user starts an ordinary fresh chat | 6 |
| [#2283](https://github.com/nesquena/hermes-webui/pull/2283) | Run event journal replay provides the foundation for ordered recovery | 5 |

These references are evidence for the contract. This RFC does not make the
linked implementation PRs dependent on this document, and it does not close the
tracking issue by itself.

## Relationship To The Run Adapter RFC

The run adapter RFC defines the longer-term event/control boundary for WebUI and
Hermes runtime ownership. This RFC defines the consistency rules that the current
WebUI and any future adapter-backed implementation must preserve.

The two documents should be read together:

- The adapter contract answers: "Where should execution ownership live?"
- This consistency contract answers: "How do transcript, context, streams,
  replay, compression, and UI metadata stay coherent while execution is active
  or being recovered?"

## Rollout Plan

1. Land this RFC as a reviewable draft and refine it through PR discussion.
2. Link future streaming/recovery/compression/sidebar PRs back to the invariant
   they intentionally preserve or change.
3. Convert recurring checklist items into focused regression tests where
   practical.
4. If #1925 introduces a new adapter-backed runtime layer, update this RFC or
   replace it with the accepted implementation contract so these invariants do
   not live only in historical discussion.

