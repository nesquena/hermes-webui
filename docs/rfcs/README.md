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

## When to file an RFC

- The change is large enough that you want consensus before writing code.
- The change touches data-at-rest formats or recovery semantics.
- The change introduces a new architectural primitive (journal, queue,
  scheduler, cache layer) that other features will build on.
- A reviewer asks for one during code review.

When in doubt, just ship the code — small features don't need RFCs.
First-time contributor RFCs should be discussed in an issue before opening a PR.

## Current RFCs

- [`turn-journal.md`](turn-journal.md) — Crash-safe WebUI turn journal for
  recovering interrupted chat submissions.
