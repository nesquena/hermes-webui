# WebUI Run State Consistency Contract

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-05-16
- **Updated:** 2026-08-22
- **Tracking issue:** [#2361](https://github.com/nesquena/hermes-webui/issues/2361)
- **Related architecture:** [#1925](https://github.com/nesquena/hermes-webui/issues/1925), [`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md), [`stable-assistant-turn-anchors.md`](stable-assistant-turn-anchors.md)

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

## Current implementation relationship

Stable Assistant Turn Anchors now implement the presentation/reconciliation
portion of this contract for one assistant turn. The run journal and settled
transcript provide durable observations; the Anchor registry and
`activity_scene_v1` reconcile those observations into Compact Worklog,
Transparent Stream, or Final answer only; `S.messages`, `INFLIGHT`, renderer
caches, and DOM remain projections or recovery caches rather than independent
semantic owners.

This RFC remains `Proposed` because its broader cross-layer contract also covers
model-context reconstruction, compression handoff, session metadata, and future
runtime-adapter migration. Shipped Anchor coverage strengthens invariants 2, 3,
and 5; it does not mark every run-state boundary implemented.

## State Layers

| Layer | Purpose | Source-of-truth expectation | Must not do |
|---|---|---|---|
| Visible transcript | Shows what the user and assistant said | Session transcript plus live replay should produce one chronological user-visible story | Hide the user turn that started active work, or show internal recovery text as current user intent |
| Model context / `context_messages` | Supplies conversation state to the agent | Must include the current visible user turn unless deliberately excluded with a user-visible reason | Let the agent resume from context that contradicts what the user can see |
| Pending turn metadata | Bridges submitted-but-not-yet-finalized user input | Must identify the user turn and stream that own active work | Become a permanent duplicate transcript row after recovery |
| Live stream / SSE | Delivers active runtime events to the browser | Must remain an observation path, not the only durable truth for already-emitted events | Lose the visible scene on refresh, reconnect, or session switch |
| Worker lifecycle registry (`ACTIVE_RUNS`) | Tracks whether a worker still occupies the session, so a successor turn cannot start on top of it | Broader than "attachable UI work": a cancelled worker stays registered while it unwinds | Be read directly as the set of runs a browser may attach to |
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
   compression cards. Replayed long-task events should enter the same
   browser-facing timeline renderer as live SSE events so recovery does not
   downgrade a structured Thinking / progress / tool / compression turn into a
   separate flattened presentation.
   When session loading combines a WebUI sidecar with Hermes Agent `state.db`, a
   native-image user turn may appear as both rich multipart content and scalar
   text that replaces each image part with `[screenshot]`. Reconciliation may
   treat those rows as one turn only when the multipart value contains text and
   recognized native-image parts, its exact scalar projection matches, role and
   tool shape match, timestamps match exactly, stable IDs and provider metadata
   do not conflict, and the pairing is unambiguous. Keep the rich sidecar row;
   if any requirement is missing or contradictory, preserve both rows rather
   than deduplicating. Literal scalar `[screenshot]` text alone is not identity
   evidence.
   Visible interim assistant progress must remain visible timeline content; a
   compact Activity disclosure may summarize adjacent tool/debug detail, but it
   must not be the only place where the user can see emitted progress text.
6. **Compression is not current intent.** Automatic compression summaries and
   reference cards are recovery/handoff material. They must not be treated as a
   new user request, active-turn content, or the default visible explanation for
   the current answer.
   Automatic compression may appear during a live turn only as a quiet,
   non-interactive context divider in the Worklog timeline, not as a clickable
   tool row. It should use action wording: `Compressing context` while active
   and `Context auto-compressed` when the agent has continued past the
   compression barrier or when a completion event arrives. The timer is
   diagnostic detail, not the source of truth for the divider's running state.
   Later tool, reasoning, or interim assistant events prove the compression
   barrier has passed even if no explicit completion event was delivered.
   Settled final history should omit live-only automatic-compression rows unless
   there is a user-visible recovery or error state to explain.
7. **Observation has a degraded path.** Long-running or many-session observation
   should expose enough heartbeat/degraded status that the UI does not appear
   silent and ordinary APIs do not stall behind active streams.
8. **Every mutation names its layer.** A PR touching streaming, recovery,
   context reconstruction, compression, replay, or sidebar metadata should state
   which layer it changes and what regression proves the invariant still holds.
9. **Lifecycle-busy is not client-attachable.** `ACTIVE_RUNS` answers "may a new
   turn start?", not "may a browser attach a renderer?". Cancellation splits the
   two: `cancel_stream()` keeps the row as `phase="cancelling"` so a successor
   cannot overlap the unwinding worker, but the client has already reached a
   terminal state for that stream because its run journal ends in a terminal
   event. Recovery paths that hand a stream id to a renderer — session SSE
   recovery and hidden-tab status polling — must therefore exclude cancelling
   rows, while busy/admission checks must keep counting them. Reading the
   registry with a single meaning resurrects a cancelled run on every fresh
   subscription: the client attaches, consumes the terminal event, tears the
   renderer down, resubscribes, and the loop repeats indefinitely.

   Because a cancelling row can otherwise persist forever, cancellation unwind is
   bounded: a cancelling row older than that window **and** owning no live
   `STREAMS` channel is reclaimed from `ACTIVE_RUNS` along with its stream-owner
   entry, so a wedged worker cannot suppress background wakeups permanently.
   Reclamation requires both conditions — age alone must not evict a row that
   still owns a live channel. Staleness is measured from the cancellation
   timestamp (falling back to run start), so a long-running turn cancelled
   moments ago is never mistaken for an orphan.
10. **Sidecar writes are generation-fenced.** A writer may replace a session JSON
   sidecar only while the exact durable revision it observed is still current.
   Text publication must use canonical LF bytes so the stored digest matches the
   file on native Windows. First creation must use an atomic create-only primitive:
   hard-link on POSIX, create-only rename on Windows, or fail closed when neither
   is available. SID rotation must start from an absent revision for the new ID.
   A raw metadata patch may advance only aliases that owned the exact pre-write
   revision; stale aliases are invalidated and reloaded instead, and callers must
   adopt the returned revision owner before model resolution or turn start.
   Recovery must recheck durable deletes under the SID authority, validate the
   embedded SID, and invalidate stale in-memory aliases. A shrinking write must
   not proceed unless its recoverable history is durable: row count or message
   coverage alone does not prove dominance. Dominance covers the complete
   canonical recovery snapshot, including ordered context/tool sequences and
   all unknown metadata; only derived generation/count and superseded activity
   time are excluded. The comparison accepts legacy snapshots that omit newer
   bookkeeping fields, has a fixed work budget, and treats any overflow or
   unverifiable field as incomparable. Malformed, non-object, foreign-SID, and
   incomparable primary backups are archived byte-for-byte and
   content-addressedly before the valid live snapshot is promoted. Archive
   publication streams into a complete
   temporary file, verifies the expected digest, and atomically renames it, so
   archive preservation does not depend on hard-link support; the stricter
   create-only primitive remains mandatory for live sidecars. Backup retirement
   requires matching receipts for both the backup and the committed live
   generation. Any deletion path must decide and act while holding the same SID
   authority, revalidate its payload and exact revision at the deletion point,
   publish a durable tombstone before unlinking, and remove the primary sidecar,
   backup, backup archives, and session-owned replay-v10 recovery artifacts
   before reporting success. Hidden ephemeral deletion follows this rule after
   both cancellation and normal completion, with the agent lock acquired before
   the SID authority. Its response-level cleanup may remain best-effort, but a
   protocol failure must be logged and must not fall back to a raw unlink.

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
- Does replay use the same timeline-rendering path as live SSE for thinking,
  interim assistant text, tool cards, compression cards, and terminal states?
- Can this change move a session in the sidebar without meaningful user or
  assistant activity?
- Does this change read `ACTIVE_RUNS` for admission ("may a turn start?") or for
  attachment ("may a browser render this?"), and does it use the matching
  predicate for that question?
- If it introduces or changes a reclamation window, what proves an in-flight
  cancellation is not evicted early, and that a wedged one is eventually freed?
- Can automatic compression or recovery text become visible active-turn content?
- Can a stale save, repair, recovery, or metadata patch replace a newer sidecar?
  Does recovery revalidate delete tombstones and state-db ownership while holding
  the same SID authority used for publication?
- If the write shrinks history, is backup publication fail-closed and monotone by
  complete canonical snapshot coverage rather than message rows or count? Does
  the bounded comparison include every durable top-level field, archive
  unverifiable/incomparable generations before promotion, and does cleanup prove
  both backup and live receipts still match before unlinking?
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
