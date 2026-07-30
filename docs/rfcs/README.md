# RFCs

This directory holds design documents for hermes-webui features that are
worth thinking through in writing before (or alongside) implementation —
typically when the change touches durability, recovery, schema, or cross-
cutting infrastructure.

## Conventions

- One file per RFC. Filename is the topic (kebab-case), not a number.
- Top of every RFC carries a small header:

      - **Status:** Proposed | Accepted | Implemented | Withdrawn
      - **Author:** @github-handle
      - **Created:** YYYY-MM-DD

- Sections usually include: Problem, Goals, Non-goals, Proposal, Open
  questions, Rollout plan. Skip what doesn't apply.
- An RFC is a starting point for review. Comments and revisions land via PR
  edits, not separate discussion threads.
- An RFC documents a design direction. It is **not** an invitation to file
  implementation PRs against fragments of it. Before opening any PR that
  implements an accepted RFC, confirm with a maintainer in the tracking
  issue that the implementation slice is wanted and that no other
  contributor is already building it. Speculative implementations of RFC
  fragments without a confirmed integration site will be held.

## When to file an RFC

- The change is large enough that you want consensus before writing code.
- The change touches data-at-rest formats or recovery semantics.
- The change introduces a new architectural primitive (journal, queue,
  scheduler, cache layer) that other features will build on.
- A reviewer asks for one during code review.

When in doubt, just ship the code — small features don't need RFCs.
First-time contributor RFCs should be discussed in an issue before opening a PR.

## Current RFCs

- [`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md) — #1925
  event/control contract, runtime-state ownership matrix, acceptance catalog,
  and reversible migration gates for moving WebUI execution behind an explicit
  adapter boundary.
- [`webui-run-state-consistency-contract.md`](webui-run-state-consistency-contract.md)
  — #2361 consistency rules for keeping transcript, model context, live streams,
  replay, compression, and session metadata coherent during active and recovered
  WebUI runs.
- [`live-to-final-assistant-replies.md`](live-to-final-assistant-replies.md)
  — #3400 accepted product model for long-running assistant replies, live
  process prose, tool activity, recovery, terminal outcomes, display
  projections, and the final-answer boundary.
- [`transparent-stream-activity-mode.md`](transparent-stream-activity-mode.md)
  — #3820 implemented opt-in display mode for power users who need a
  transparent, chronological Thinking / progress / tool-call stream alongside
  the default Compact Worklog and opt-in Final answer only projections.
- [`stable-assistant-turn-anchors.md`](stable-assistant-turn-anchors.md) — #3926
  implemented frontend presentation/reconciliation model for anchoring live
  assistant activity, settled final answers, replay, and all activity display
  modes to one assistant turn; remaining hardening is tracked under #3400.
- [`canonical-session-resolution.md`](canonical-session-resolution.md) — #2361
  focused contract for resolving URL, query parameter, localStorage, sidebar,
  and compression-lineage session IDs to one canonical visible chat target.
- [`turn-journal.md`](turn-journal.md) — Crash-safe WebUI turn journal for
  recovering interrupted chat submissions.
- [`webui-pending-intent-controls.md`](webui-pending-intent-controls.md) —
  #3058 control-surface companion to #3400 for Queue, Steer, Stop-and-send,
  Interrupt, and leftover-steer inputs submitted while a long-running agent
  session is active.
- [`session-sse-contract-v1.md`](session-sse-contract-v1.md) — #4812 Proposed
  contract vocabulary, replay identity, event taxonomy, cursor/resume semantics,
  and implementation gates for `GET /api/sessions/{session_id}/events`.
