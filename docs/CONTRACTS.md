# Project Contracts

This document is a contributor-facing index for existing Hermes WebUI contracts,
RFCs, design constraints, and review expectations. It does not replace the
source documents and it does not mark proposals as implemented. Follow each
linked document's status and scope.

Use this file when starting a change so the relevant public contract is visible
before code is edited. This index focuses on documentation routing and
contributor guidance; it does not change runtime behavior or CI gates.

## Start here

- [`AGENTS.md`](../AGENTS.md): repository entry point for AI assistants,
  public-safety rules, and the short redline checklist.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md): contribution style, verification,
  PR description expectations, UI evidence, and project-specific constraints.
- [`README.md`](../README.md): product overview, quick start, architecture map,
  feature inventory, and docs index.
- [`CHANGELOG.md`](../CHANGELOG.md): release history maintained by the release
  workflow. Read it for context, but do not edit it in ordinary contributor PRs;
  put release-note-ready wording in the PR body instead.

## Runtime, durability, and state contracts

- [`docs/rfcs/webui-run-state-consistency-contract.md`](rfcs/webui-run-state-consistency-contract.md):
  proposed consistency rules for current WebUI streaming, recovery, replay,
  model-context reconstruction, compression, UI scene/cache, and sidebar metadata
  repairs. Start here for narrow fixes that keep the existing WebUI execution
  path.
- [`docs/rfcs/live-to-final-assistant-replies.md`](rfcs/live-to-final-assistant-replies.md):
  proposed product model for long-running assistant replies, live process text,
  tool activity, recovery, terminal outcomes, and final-answer boundaries. Start
  here for UI/UX changes to running-session assistant reply rendering.
- [`docs/architecture/stable-assistant-turn-anchor-phase0.md`](architecture/stable-assistant-turn-anchor-phase0.md):
  current Phase 0 inventory for the Stable Assistant Turn Anchors work under
  #3926. Use this before wiring anchor helpers into live SSE, replay,
  settlement, `INFLIGHT`, or `renderMessages()` paths.
- [`docs/rfcs/canonical-session-resolution.md`](rfcs/canonical-session-resolution.md):
  proposed contract for resolving URL routes, query parameters, localStorage,
  sidebar rows, and compression-lineage IDs to one canonical visible session
  target. Start here for session routing, boot restore, stale parent, or
  compression-tip selection changes.
- [`docs/rfcs/hermes-run-adapter-contract.md`](rfcs/hermes-run-adapter-contract.md):
  proposed event/control contract, runtime-state ownership matrix,
  acceptance-test catalog, and reversible migration gates for moving WebUI
  execution behind an adapter boundary. Use this for adapter-seam, control-plane,
  runner, sidecar, or execution-ownership work; do not treat it as authorization
  to implement those slices.
- [`docs/architecture/agent-api-contract.md`](architecture/agent-api-contract.md):
  current audit of WebUI dependencies on the hermes-agent source checkout and
  the replacement API/client surfaces needed before source mounts can be removed.
  Start here for issue #2491 and Docker/source-boundary migration slices.
- [`docs/rfcs/turn-journal.md`](rfcs/turn-journal.md): proposed crash-safe
  write-ahead journal for browser-originated chat turns.
- [`docs/rfcs/webui-pending-intent-controls.md`](rfcs/webui-pending-intent-controls.md):
  proposed control-surface companion to the long-running-session reply model for
  Queue, Steer, Stop-and-send, Interrupt, and leftover-steer inputs submitted
  while an agent run is active. Start here for busy-composer behavior, pending
  queued messages, interrupt replacement, steer visibility, or leftover-steer
  recovery changes.
- [`docs/rfcs/README.md`](rfcs/README.md): RFC conventions and current RFC index.

When a change touches streaming, recovery, replay, compression, context
reconstruction, cancellation, approval/clarify, session metadata, or run state,
read the relevant RFC before editing. In the PR description, name the state layer
or event/control surface affected and include a regression test or manual
verification for the relevant invariant.

Proposed RFCs are review guardrails, not implementation authorization. Do not
implement RFC fragments unless the task or tracking issue explicitly asks for
that slice.

## UI, UX, and theme contracts

- [`DESIGN.md`](../DESIGN.md): design tokens and the current calm-console
  direction: conversation first, quiet metadata, restrained accents, and
  progressive disclosure for debugging detail.
- [`docs/UIUX-GUIDE.md`](UIUX-GUIDE.md): contributor-facing synthesis of the
  repository's UI/UX principles, sourced from existing project docs and code
  comments.
- [`docs/ui-ux/index.html`](ui-ux/index.html): message-area inventory wired to
  the real app stylesheet.
- [`docs/ui-ux/two-stage-proposal.html`](ui-ux/two-stage-proposal.html):
  existing two-stage chat UX proposal for issue #536.
- [`THEMES.md`](../THEMES.md): theme and skin guidance; the core palette
  variable contract lives in `static/style.css`.

Current appearance has a theme axis (`light`, `dark`, `system`) and a separate
skin axis (`default`, `ares`, `mono`, `slate`, `poseidon`, `sisyphus`,
`charizard`, `sienna`, `catppuccin`, `nous`, `geist-contrast`) in
`static/boot.js` and `static/style.css`. Do not follow stale `data-theme`-only theme guidance unless
the current code and tests prove that model still applies.

For UI or UX work, include before/after evidence, verify relevant responsive
states, and prefer stable class/data hooks over one-off visual behavior.

## Choosing the relevant contract

Before editing, identify which contract family the task exercises. This is a
routing check, not a request to read every document in the repository. Read the
documents that match the touched subsystem.

Use this lightweight note in an issue comment, draft PR, task note, or AI-agent
handoff when it helps clarify scope:

```markdown
## Contract Routing

Task type:
Touched areas:
Relevant public docs:
- `AGENTS.md`
- `CONTRIBUTING.md`
- `docs/CONTRACTS.md`
- <subsystem-specific documents>
Scope boundaries:
Evidence needed before claiming done:
```

For small, obvious fixes, keep this short. The goal is to avoid routing mistakes,
not to create process overhead.

## Contract changes

Changing contract documents, RFC guidance, or contract tests changes review
expectations for future contributors. A PR that intentionally changes an
existing contract should include a `Contract Change` section in its PR body with:

- the previous contract,
- the new contract,
- the affected docs and tests,
- the compatibility or migration reason.

Contract tests and corresponding docs must move together. Tests that encode
product semantics must not silently redefine the contract by asserting the
opposite behavior without updating the public docs and naming the change in the
PR body.

The static tests for this guidance are advisory coverage. They pin contributor
wording so the rule stays visible. This advisory coverage is not an automated
policy gate; static coverage is not an automated policy gate and does not enforce
PR-body content on GitHub. A future release-time or CI check could
surface contract-affecting diffs whose PR body lacks `Contract Routing`, but this
document only defines the review expectation.

Release batches should list included contract-affecting PRs explicitly so
reviewers can distinguish ordinary green-CI fixes from changes that update the
project's product or runtime guardrails.

## PR preparation checklist

Before opening or updating a PR, verify `CONTRIBUTING.md` against the actual PR
body. This checklist applies even when code and tests are already done.

Required checks:

- The PR solves one logical problem.
- The PR body contains all required sections from `CONTRIBUTING.md`:
  `Thinking Path`, `What Changed`, `Why It Matters`, `Verification`,
  `Risks / Follow-ups`, and `Model Used`.
- `Model Used` discloses provider/model and notable agent/tool use, or says
  `None -- human-authored`.
- UI/UX changes include before/after evidence and responsive-state coverage.
- Runtime/streaming changes name the state layer or invariant being changed and
  list the regression or manual invariant check.
- Contract-affecting PRs include `Contract Routing`; intentional contract
  changes also include `Contract Change`.
- Onboarding/setup validation used isolated `HERMES_HOME` and
  `HERMES_WEBUI_STATE_DIR`, unless the human operator explicitly requested real
  state.
- Docs updates are included or explicitly not needed, and release-note-worthy
  changes are described in the PR body rather than by editing `CHANGELOG.md`.
- After the GitHub write, read the PR back and verify the headings rendered as
  intended.

Green CI plus a focused diff is not sufficient if the PR description or evidence
does not match the touched subsystem.

## Setup, onboarding, and operational references

- [`TESTING.md`](../TESTING.md): automated test command and manual browser test
  plan.
- [`ARCHITECTURE.md`](../ARCHITECTURE.md): API, module layout, and design
  constraints.
- [`docs/onboarding.md`](onboarding.md): first-run wizard and provider setup.
- [`docs/onboarding-agent-checklist.md`](onboarding-agent-checklist.md): safety
  rules for assistant-led install, reinstall, bootstrap, provider setup, local
  model setup, Docker onboarding, and WSL onboarding.
- [`docs/docker.md`](docker.md): Docker compose setup, common failures, and
  bind-mount migration.
- [`docs/troubleshooting.md`](troubleshooting.md): diagnostic flows for common
  failures.
- [`docs/EXTENSIONS.md`](EXTENSIONS.md): administrator-controlled WebUI
  extension injection.

## Quick redline checklist

Before opening a change for review, confirm:

- The change solves one logical problem; unrelated refactors are split out.
- `AGENTS.md`, this index, and any linked contract for the touched subsystem were
  read before editing.
- Behavior, setup, architecture, testing, or workflow changes update the relevant
  docs; release-note-ready changes include PR-body release-note wording while
  `CHANGELOG.md` is left to release commits.
- UI/UX changes include before/after evidence and cover relevant desktop,
  narrow, and mobile states.
- Runtime, streaming, recovery, replay, compression, or sidebar changes state
  which layer they mutate and include a regression for the invariant.
- New dependencies, build tools, frameworks, or long-lived processes are avoided
  unless the benefit and rollback story are explicit.
- Onboarding/setup validation uses isolated `HERMES_HOME` and
  `HERMES_WEBUI_STATE_DIR` unless the human operator explicitly asks to use real
  state.
- Secrets, private paths, local-only workflows, and personal notes stay out of
  tracked docs and examples.

## Future evolution

This index is not intended to make the first contract set final. Future PRs may
add, revise, split, or retire contracts when real issues, implementation changes,
RFC decisions, contributor feedback, or review experience show that guidance is
incomplete or stale.

Potential follow-up areas include session import/export, cron, extensions,
security boundaries, Docker/runtime isolation, and lightweight checks that keep
key contract links from drifting.
