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
  accepted product model for long-running assistant replies, live process text,
  tool activity, recovery, terminal outcomes, and final-answer boundaries. Start
  here for UI/UX changes to running-session assistant reply rendering.
- [`docs/rfcs/stable-assistant-turn-anchors.md`](rfcs/stable-assistant-turn-anchors.md):
  implemented presentation/reconciliation model that attaches live, settled,
  replayed, and recovered activity to one assistant-turn owner and projects one
  `activity_scene_v1` into Compact Worklog, Transparent Stream, or Final answer
  only. Remaining hardening stays tracked under #3400.
- [`docs/architecture/stable-assistant-turn-anchor-phase0.md`](architecture/stable-assistant-turn-anchor-phase0.md):
  cumulative implementation inventory for the Stable Assistant Turn Anchors
  work under #3926. Use it to distinguish shipped wiring from historical slice
  boundaries before changing live SSE, replay, settlement, `INFLIGHT`, or
  `renderMessages()` paths.
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
- [`docs/rfcs/session-sse-contract-v1.md`](rfcs/session-sse-contract-v1.md):
  proposed contract vocabulary, cursor/resume semantics, replay identity, snapshot
  fallback, event taxonomy, and implementation gates for the per-session SSE
  stream `GET /api/sessions/{session_id}/events` (#4812). Distinct from the
  existing global session-list stream `GET /api/sessions/events`. Start here for
  any work that touches per-session SSE, `Last-Event-ID` replay, or session
  lifecycle event delivery. The Phase 1 server contract is now shipped; the RFC
  remains the vocabulary reference while broader client and platform claims stay
  behind the recorded proof gates.

When a change touches streaming, recovery, replay, compression, context
reconstruction, cancellation, approval/clarify, session metadata, or run state,
read the relevant RFC before editing. In the PR description, name the state layer
or event/control surface affected and include a regression test or manual
verification for the relevant invariant.

Proposed RFCs are review guardrails, not implementation authorization. Do not
implement RFC fragments unless the task or tracking issue explicitly asks for
that slice.

## Agent-delegated text-to-speech contract

Hermes WebUI owns the authenticated browser transport and playback lifecycle,
but it does **not** own cloud/local provider implementations or credentials. For
`engine: agent`, each capability, provider-selection, or synthesis operation is
delegated to one short-lived `api.agent_tts_worker` child using the active
profile environment. The worker imports the installed Hermes Agent's current
TTS/configuration callables; no Agent daemon, API, service, or core change is
required.

The callable compatibility boundary is fail-closed:

- capability requires Agent config loading, TTS catalog visibility/active checks,
  requirement checks, and `text_to_speech_tool(text, output_path)`;
- provider writes additionally require `apply_provider_selection`, `save_config`,
  and config-path discovery;
- older/missing signatures return a finite sanitized unsupported response and do
  not alter the persisted WebUI engine;
- provider rows come from Agent metadata. Known provider IDs map through a fixed
  lowercase `tts_provider_*` locale allowlist; command/plugin rows use their safe
  Agent names without dynamically creating locale keys.

The active profile owns the canonical provider and provider credentials in its
Agent `config.yaml`/environment. The WebUI never accepts provider, model, URL,
credential, output-path, or environment overrides on synthesis requests. Child
environments remove every name declared by any profile `.env`, all `HERMES_WEBUI_*`
authentication/deployment values, and interpreter/dynamic-loader injection
controls before projecting only the selected profile runtime. Shared YAML writes use
the same sidecar lock across WebUI threads and Agent TTS children, serialize the
full read/modify/write transaction, preserve symlink bindings, atomically replace
the referent, fsync the file and referent directory, and invalidate both link and
target cache entries.

`POST /api/tts` accepts only `{"engine":"agent","text":"..."}`. Browser
speech is local and never reaches that endpoint. The reserved IDs `edge`,
`openai`, `elevenlabs`, and `server` return
`409 legacy_tts_migration_required`; former extension IDs are inert persisted
repair values. Only Browser and Hermes Agent are selectable. If a persisted
Agent/legacy choice is unavailable, effective Browser playback is session-only
and never silently overwrites the saved choice.

Ordinary speech-preference autosaves carry the last observed
`speech_settings_revision`. The server compares that revision while holding the
shared settings write lock; stale writes return `409 settings_conflict`. Browser
autosaves are serialized, ignore stale completions, and refetch authoritative
settings before rolling back a failed optimistic engine change.

Each operation has a 64 KiB request/status limit, a 10-second capability timeout,
a 60-second synthesis timeout, a 16 MiB audio limit, one concurrent operation per
owner, and two global workers by default. `HERMES_WEBUI_TTS_MAX_WORKERS` may set
1–8 global workers; `HERMES_WEBUI_TTS_REQUEST_MAX_CHARS` may set the transport
text ceiling (clamped to 256–10,000 and further bounded by the Agent provider).
Every exit kills and reaps the child process group, including descendants left by
an already-exited worker leader. Audio is descriptor-, path-, size-, MIME-,
and signature-validated before raw bytes are returned with `no-store` and
`nosniff`; request directories and browser object URLs are cleaned on every exit.

All TTS routes use normal WebUI auth and POST CSRF checks, including the shared
trusted-header first-request principal. When WebUI auth is disabled, the same
trusted-proxy-aware local-origin gate as the embedded terminal applies before any
worker is started; spoofed forwarding headers fail closed. Relative `api()` URLs preserve subpath
and reverse-proxy deployments. Docker must ship an installed Agent package that
is importable by `sys.executable -m api.agent_tts_worker`.

Safe troubleshooting (do not print `.env`, config contents, keys, or cookies):

```bash
hermes tools
python -c "from tools import tts_tool; print(tts_tool.check_tts_requirements())"
python -c "from hermes_cli import config; print(config.get_config_path())"
```

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
