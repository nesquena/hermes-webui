# Project OS phase-1 topology and review-routing contract

This document is the repo-side contract for the current live multi-agent shape used by Project OS control-plane docs, task assignment notes, and review routing.

It exists to keep `Recover current repo`, `Refresh docs`, and related continuity updates from drifting back to older topology assumptions.

## Live phase-1 topology

The live phase-1 topology is limited to:

- `default`
- `ops`
- `builder`

Do not expand repo-side phase-1 docs, lane descriptions, or assignment rules beyond those three live profiles unless the task explicitly changes the topology.

## Builder lane contract

`builder` is now an active manual implementation lane.

That means:

- treat `builder` as a real implementation assignee when repo-side docs describe the current live lanes
- use `builder` for explicit manual implementation work when that lane is the active human-steered execution surface
- do not describe `builder` as reserve-only in refreshed PROJECT/PLAN/STATUS-style continuity output

At the same time, keep `builder` out of recurring cron/background ownership.

That means:

- recurring unattended cron ownership should stay on the existing live automation surfaces, not `builder`
- background watchdog, scheduler, and similar always-on ownership should not be reassigned to `builder` by default
- phase-1 docs should distinguish manual implementation ownership from recurring automation ownership

## Reviewer-profile boundary

Do not add a live reviewer profile to the phase-1 topology.

If the work needs review, route it through existing lanes and repo-side guidance instead of minting a fourth live profile.

## Review routing rules

### Code review

Route code review through `default` or `builder`, using the repo's normal review contracts and skills.

Minimum repo-side references:

- `CONTRIBUTING.md`
- `docs/CONTRACTS.md`
- repo review/testing guidance relevant to the touched subsystem

### UX/UI/design review

Route UX/UI/design review through Claude Design first.

If the design review produces implementation-facing findings, hand them back through the repo's UX/UI guidance before changing code:

- `docs/UIUX-GUIDE.md`
- `DESIGN.md`

This keeps design critique and repo implementation rules separate: Claude Design leads the review pass, while repo-side UX/UI guidance governs any follow-on implementation change.

## Assignment-rule implications for control-plane docs

When repo-side continuity or control-plane docs are refreshed:

- treat `default`, `ops`, and `builder` as the only live phase-1 profile topology
- allow `builder` to appear as the active manual implementation lane
- keep recurring/background ownership off `builder` unless a future contract explicitly changes that rule
- avoid introducing a live reviewer-profile lane in board/task/docs narratives
- distinguish code review routing from UX/UI/design review routing instead of collapsing them into one generic reviewer lane

## Non-goals

This contract update does not:

- add a live reviewer profile
- move recurring cron/background ownership onto `builder`
- broaden the live phase-1 topology beyond `default/ops/builder`
- change the thin-control-layer rule for Project OS itself

## When to read this

Read this document before editing repo-side Project OS continuity, assignment rules, or phase-boundary docs, especially when a task touches:

- control-plane prompt wording
- board/task assignee conventions
- live lane descriptions
- review-routing language
- phase-1 boundary notes
