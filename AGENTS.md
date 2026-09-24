# Agent instructions for Hermes WebUI

This file is the shared entry point for AI assistants working in this
repository. Keep it project-specific and safe to publish. Do not put personal
machine setup, private network details, credentials, tokens, or local-only
workflow notes here.

## Documentation by task

Use the references that match the change; there is no blanket reading list for
unrelated work. Keep subsystem-specific safety and contract requirements below.

- `README.md` for product behavior, setup, usage, and the docs index
- `CONTRIBUTING.md` for contribution scope, PR format, and review requirements
- `docs/CONTRACTS.md` to find the contract/RFC for the subsystem being changed
- `docs/GUIDELINES.md` for bug-class coverage, state ownership, and evidence standards
- `CHANGELOG.md` for release-history questions; ordinary PRs do not edit it
- `ARCHITECTURE.md` for design constraints and current module layout
- `TESTING.md` for local verification commands and relevant manual checks
- `docs/onboarding.md` for first-run onboarding behavior
- `docs/troubleshooting.md` for diagnostic flows
- `docs/rfcs/README.md` for larger RFCs and state/durability contracts
- `docs/UIUX-GUIDE.md` and `DESIGN.md` for layout, interaction flow, themes,
  chat rendering, and composer chrome

## Onboarding and reinstall support

If the task involves install, reinstall, bootstrap, first-run onboarding,
provider setup, local model server setup, Docker onboarding, WSL onboarding, or
support for a failed first run, read `docs/onboarding-agent-checklist.md`
before running commands or inspecting logs.

Follow that checklist's safety rules:

- use isolated `HERMES_HOME` and `HERMES_WEBUI_STATE_DIR` for trials unless the
  human explicitly asks to use real state
- do not delete or overwrite a real `~/.hermes` directory without explicit
  approval
- do not print API keys, OAuth tokens, cookies, full `.env` files, full
  `auth.json` files, or password hashes
- collect non-secret status and log evidence before recommending a fix

## Contribution style

- Keep one logical change per PR; split unrelated refactors or cleanup.
- Read `docs/CONTRACTS.md` and the linked contract/RFC for the touched
  subsystem before editing.
- For local pytest runs, use `./scripts/test.sh` instead of bare `python3`,
  `python -m pytest`, or `pytest`. The script creates/uses the repo `.venv`,
  pins execution to Python 3.11-3.13, and installs missing dev test dependencies.
  `HERMES_WEBUI_TEST_PYTHON` selects the supported base interpreter used to
  create or rebuild `.venv`; it must not install test dependencies into a
  system/Homebrew interpreter directly.
  If a direct pytest invocation reports an unsupported interpreter, rerun through
  `./scripts/test.sh` before debugging product code.
- Prefer the existing Python + vanilla JavaScript structure. Do not add
  dependencies, build tools, frameworks, or long-lived processes without clear
  justification and a rollback story.
- Update docs when changing setup, onboarding, runtime behavior, architecture,
  testing guidance, or user-facing workflows.
- Do not edit `CHANGELOG.md` in ordinary contributor PRs. The release workflow
  owns changelog updates through release commits. If a change is release-note
  worthy, include concise release-note wording in the PR body instead.
- For UI or UX changes, include before/after evidence and test relevant
  desktop, narrow, and mobile states.
- For behavior changes, add or update automated tests where practical and list
  the manual verification performed.
- For runtime, streaming, recovery, replay, compression, or sidebar metadata
  changes, name the state layer being mutated and prove the relevant invariant.
- Active-run Steer resolves the stream-bound agent with explicit stream and
  worker ownership before consulting the reusable session cache. Compression
  may rotate the agent identity; steering must never evict or close an agent.
  Keep HTTP response writes outside runtime registry locks. A Gateway-owned
  active run must resolve to the Gateway outcome before any local cache fallback,
  even when no in-process worker is registered for that stream. Stop publishes
  cancellation and detaches stream/agent entries under the same stream lock
  (STREAMS_LOCK -> ACTIVE_RUNS_LOCK); interrupt and session persistence remain
  outside it. The retained cache-only path (no registered worker) revalidates
  stream membership, owner, and active-run session/backend/phase and enqueues
  agent.steer() under that same lock edge, so a Stop that claims cancellation
  never strands guidance an earlier Steer response reported as accepted. Test
  both Stop/Steer orderings — registered and cache-only — with deterministic
  barriers. Initial active-run publication and its cancel flag must share that
  lock edge with Stop; do not recreate the flag after journal setup. Worker
  registration must also check its retained cancel event and live stream
  membership: Stop can remove CANCEL_FLAGS during initialization.
  Do not re-register a cancelled worker; finalize outside the stream lock.
  Local Steer accepts only explicit starting/running phases. Close admission
  by publishing finalizing under STREAMS_LOCK before the last pending-steer
  drain; earlier accepted guidance is drained, later guidance is rejected with
  `not_running` while the owned stream is still live (not `stream_dead`). Use
  one idempotent terminal settlement before done/error/end and final cleanup,
  covering returned errors, exceptions and self-heal, not only the success path.
  Merge Agent-returned `pending_steer` with the registered worker's final slot
  drain and emit leftovers before terminal events, outside registry locks.
  Test both drain/Steer orderings, including compression-rotated identities.
- Inactive-session recovery is separate from live Steer. Resolve durable
  compression lineage in the session's profile database, read-only, even when
  the WebUI sidecar has no snapshot flag. Never reopen a sealed parent. Reject
  stale chat POSTs before workspace/model/pending-state mutation; the browser
  loads the continuation and preserves the draft without automatic replay.
  Explicit closures and unknown terminal reasons do not authorize a redirect.
- For Docker build changes in `docker_init.bash`, mirror directory exclusions
  in both the `rsync` and `cp -a` paths — `/opt/hermes` may contain subdirectories
  with restricted permissions (e.g. `.playwright/`).

## Completion and verification

A change is ready for review when:

- The requested scope is implemented, with related call sites and lifecycle
  paths covered or explicitly identified as out of scope; unrelated cleanup is absent.
- Behavior changes have observable regression evidence where practical, including
  proof that a bug-fix test fails before the fix and passes after it. Evidence covers
  affected and neighboring tests, not only new tests.
- State changes identify the authoritative value and its owner, use that value
  through decision, action, and persistence, and account for cleanup on every exit.
  Fallbacks and defaults extend the existing mechanism rather than duplicate it.
- UI changes include before/after evidence at desktop and narrow widths and
  cover relevant mobile interactions; controls fit their frequency of use.
- Relevant docs are current. The PR body follows `CONTRIBUTING.md`, includes
  `Contract Routing` (and `Contract Change` for intentional contract changes)
  where required by `docs/CONTRACTS.md`, and reports actual commands, outcomes,
  assumptions, and anything not verified. Mocks are not proof of external behavior.

Within the requested scope, you may run local tests with disposable fixtures,
fix failures caused by the change, and rerun affected tests without asking for
approval at each step. Use `./scripts/test.sh` for pytest as specified above.
This permission applies only with confirmed isolated state and no live
credentials or services; the runner manages Python dependencies but is not a
network sandbox. It does not authorize modifying real state, handling credentials,
restarting existing services, or exposing the app beyond localhost. Those actions
require explicit human approval and the onboarding safety rules still apply.
If verification is blocked, report the blocker rather than claim completion.

## Local state and secrets

Hermes WebUI can read and write real agent state, sessions, workspaces,
credentials, and cron data. Treat local validation as potentially destructive
unless you have confirmed the active state directories.

For authority, capability, identity, or containment checks, fail closed when
safety cannot be confirmed: unknown is not allowed. Validate adversarial inputs
at the point of use, account for check-then-use races, and scope caches by the
complete identity so profiles and sessions cannot leak into each other.

Prefer isolated trial state for experiments:

```bash
HERMES_HOME=/tmp/hermes-webui-agent-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-webui-agent-state \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py
```

Do not include private machine instructions in this tracked file. Use a
git-ignored local note for personal workflow details.
