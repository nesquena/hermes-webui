# Coding standards

Implementation rules for this codebase: how the code is *written* and verified,
not how the product *behaves*.

- Product semantics, state ownership, and durability contracts live in
  `docs/rfcs/` — routed from `docs/CONTRACTS.md`.
- Review shape, PR format, and evidence standards live in `CONTRIBUTING.md`
  and `docs/GUIDELINES.md`.
- `AGENTS.md` holds the task router and universal safety.

Each section below names the trigger that makes it apply. If you are not editing
the paths it names, skim past it.

## Where each kind of rule lives

| You are | The rule for it is in |
|---|---|
| writing runtime, streaming, recovery, or cancellation code | this file |
| deciding whether a subsystem change is finished | `docs/GUIDELINES.md` rules 1-10 |
| shaping a PR, or claiming verification | `CONTRIBUTING.md`, `docs/GUIDELINES.md` |
| changing what the user sees a run do | the RFC for that subsystem |
| adding a dependency, build tool, or new parallel abstraction | `docs/GUIDELINES.md` rule 8, `CONTRIBUTING.md` "Preserve the Design Constraints" |

## Runtime registry locks

Read before editing `api/streaming.py`, `api/session_ops.py`, `api/routes.py`,
`api/models.py`, `api/updates.py`, `api/background_process.py`, or
`api/config.py`.

`STREAMS_LOCK` guards the browser/SSE observation path; `ACTIVE_RUNS_LOCK` guards
worker liveness. They are independent registries.

- When both must be read atomically, nest `STREAMS_LOCK` outside
  `ACTIVE_RUNS_LOCK` (`api/streaming.py:14312`). Several call sites take them
  sequentially instead, because a consistent snapshot is enough
  (`api/models.py:906`, `api/routes.py:3178`, `api/updates.py:140`).
- Sequential is not the weaker order. When you only need a consistent read, take
  them one at a time, in either order, rather than nesting
  (`api/session_ops.py:539`, `api/background_process.py:664`).
- Never perform HTTP response writes or cache/database teardown under a registry
  lock (`api/streaming.py:14308`).

## Active-run Steer and Stop

Read before editing steer, stop, interrupt, or cancellation paths.

Active-run Steer resolves the stream-bound agent with explicit stream and worker
ownership before consulting the reusable session cache. Compression may rotate the
agent identity; steering must never evict or close an agent.

A Gateway-owned active run must resolve to the Gateway outcome before any local
cache fallback, even when no in-process worker is registered for that stream.

Stop publishes cancellation and detaches stream/agent entries under the same
stream lock edge; interrupt and session persistence remain outside it.

Initial active-run publication and its cancel flag must share that lock edge with
Stop; do not recreate the flag after journal setup. Worker registration must also
check its retained cancel event and live stream membership: Stop can remove
`CANCEL_FLAGS` during initialization. Do not re-register a cancelled worker;
finalize outside the stream lock.

The retained cache-only path (no registered worker) revalidates stream membership,
owner, and active-run session/backend/phase, and enqueues `agent.steer()` under
that same lock edge — so a Stop that claims cancellation never strands guidance
an earlier Steer response reported as accepted.

Local Steer accepts only explicit starting/running phases. Close admission by
publishing `finalizing` under `STREAMS_LOCK` before the last pending-steer drain:
earlier accepted guidance is drained, later guidance is rejected with
`not_running` while the owned stream is still live (not `stream_dead`).

Use one idempotent terminal settlement before done/error/end and final cleanup,
covering returned errors, exceptions, and self-heal — not only the success path.
Merge Agent-returned `pending_steer` with the registered worker's final slot
drain, and emit leftovers before terminal events, outside registry locks.

Tests for this area: cover both Stop/Steer orderings — registered and cache-only —
and both drain/Steer orderings, including compression-rotated identities, with
deterministic barriers rather than sleeps.

## Inactive-session recovery

Read before editing recovery, continuation, or compression-lineage paths.

Separate from live Steer. Resolve durable compression lineage in the session's
profile database, read-only, even when the WebUI sidecar has no snapshot flag.
Never reopen a sealed parent. Reject stale chat POSTs before workspace/model/
pending-state mutation; the browser loads the continuation and preserves the draft
without automatic replay. Explicit closures and unknown terminal reasons do not
authorize a redirect.

## Docker build parity — read before editing `docker_init.bash`

Mirror directory exclusions across *both* the `rsync` path and the `cp -a` fallback
path. `/opt/hermes` may contain subdirectories with restricted permissions (for
example `.playwright/`). The two branches are independent code, and they drift
silently — a new `--exclude` added to one is not added to the other unless someone
checks.

## Tests and verification — read before running tests or claiming a change is done

- Run pytest through `./scripts/test.sh`. It creates or reuses the repo `.venv`
  and installs missing dev test dependencies; `HERMES_WEBUI_TEST_PYTHON` selects
  the supported base interpreter (3.11-3.13) used to build it. Never install test
  dependencies into a system or Homebrew interpreter.
- If a direct `pytest` invocation reports an unsupported interpreter, that is a
  runner problem, not a product bug: rerun through `./scripts/test.sh` before
  debugging product code.
- For a bug fix, run the new test against the unmodified code first and confirm it
  fails for the right reason (`docs/GUIDELINES.md` rule 6).
- For runtime, streaming, recovery, replay, compression, or sidebar changes: name
  the state layer you mutate and prove its invariant (`docs/GUIDELINES.md` rules 2
  and 7).
- Use disposable fixtures and confirmed isolated state. See the authorization
  limits in `AGENTS.md` and `docs/onboarding-agent-checklist.md` before touching
  real state.