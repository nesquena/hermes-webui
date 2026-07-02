# Agent Handoff — Hermes WebUI

> Phase 0 preflight completed 2026-07-02.
> Branch: `feat/runtime-adapter-hermex-contract`

## Phase 0 snapshot

| Field | Value |
|---|---|
| **Branch** | `feat/runtime-adapter-hermex-contract` |
| **Base** | `master` |
| **HEAD** | `d096b5f5d9b40789a64c1151b86350c39ce5581e` |
| **Dirty files** | none (clean working tree) |
| **Created** | 2026-07-02 |

## Test commands

```bash
# Full test suite (uses .venv, Python 3.11–3.13)
./scripts/test.sh

# Collect-only to count tests
./scripts/test.sh tests/ --collect-only -q

# Specific runtime-adapter tests
./scripts/test.sh tests/test_runtime_adapter_seam.py -v

# Turn-journal tests
./scripts/test.sh tests/test_turn_journal.py tests/test_turn_journal_lifecycle.py -v

# Stale-stream tests
./scripts/test.sh tests/test_stale_stream_pending_recovery.py -v

# JS runtime lint guard
npm run lint:runtime

# Python forward lint gate (diff only)
python3 scripts/ruff_lint.py --diff origin/master

# Browser smoke test
python tests/browser_smoke.py
```

## Relevant runtime files

### Core adapter seam
- `api/runtime_adapter.py` (431 lines) — `RuntimeAdapter` Protocol, data classes (`StartRunRequest`, `RunStartResult`, `RunEventStream`, `RunStatus`, `ControlResult`), `LegacyJournalRuntimeAdapter`, `RunnerRuntimeAdapter`, `build_runtime_adapter()`, feature-flag helpers
- `api/run_journal.py` (320 lines) — Slice 1 append-only run journal: `read_run_events()`, `find_run_summary()`, cursor replay

### Contract docs
- `docs/rfcs/hermes-run-adapter-contract.md` — RFC for migration slices (Slices 1–4 shipped), event/control contract, state inventory, acceptance test catalog
- `docs/CONTRACTS.md` — contracts index

### Tests
- `tests/test_runtime_adapter_seam.py` (815 lines) — adapter-seam tests

### Legacy execution path
- `server.py` — HTTP handler dispatch
- `api/routes.py` — all GET/POST route handlers
- `api/streaming.py` — SSE engine, `_run_agent_streaming()`, cancel support, compression

### Architecture docs
- `ARCHITECTURE.md` — current architecture reference
- `TESTING.md` — manual browser test plan, ~7,150 automated tests

## Phase 0 completion checklist

- [x] Inspect repo state (clean master, HEAD d096b5f)
- [x] Create branch `feat/runtime-adapter-hermex-contract`
- [x] Create `AGENT_HANDOFF.md`
- [x] Record branch, HEAD, dirty files, test commands, relevant runtime files
- [x] No feature code implemented
- [x] Only safe inspection commands run

## Phase 1: WebUI runtime contract — COMPLETE

| Field | Value |
|---|---|
| **Status** | Complete |
| **HEAD before commit** | `5f63b4d` |
| **Changed files** | `api/runtime_contract.py` (created), `tests/test_runtime_contract.py` (created), `docs/rfcs/runtime-api-contract.md` (created) |

### Verification

```bash
# Import check
python3 -c 'from api.runtime_contract import make_event, make_status, is_valid_event_type, is_valid_status; ...'  # passed

# Test suite
./scripts/test.sh tests/test_runtime_contract.py -v
# 16 passed in 2.20s — all green
```

### Deliverables

- `api/runtime_contract.py` — `RuntimeEvent`, `RuntimeStatus`, `make_event()`, `make_status()`, `is_valid_event_type()`, `is_valid_status()`, payload redaction
- `tests/test_runtime_contract.py` — 16 tests covering serialization, event_id stability, type/status validation, secret redaction, import isolation
- `docs/rfcs/runtime-api-contract.md` — event envelope, run status shape, reconnect behavior, control semantics, Hermex/mobile usage pattern, compatibility expectations

### Next task

**Phase 2: WebUI durable runtime journal** — COMPLETE (see below)

---

## Phase 2: WebUI durable runtime journal — COMPLETE

| Field | Value |
|---|---|
| **Status** | Complete |
| **HEAD before commit** | `6aa6a8c` |
| **Changed files** | `api/runtime_journal.py` (created), `tests/test_runtime_journal.py` (created), `docs/rfcs/runtime-api-contract.md` (updated) |

### Verification

```bash
# Focused test run
./scripts/test.sh tests/test_runtime_contract.py tests/test_runtime_journal.py -v
# 42 passed in 2.22s -- all green (16 contract + 26 journal)

# Import smoke check
python3 -c 'from api.runtime_journal import RuntimeJournal; print("OK")'
```

### Deliverables

- `api/runtime_journal.py` -- `RuntimeJournal` class with durable append-only run event storage using `RuntimeEvent`/`RuntimeStatus` from `api/runtime_contract.py`. Required public methods: `create_run()`, `append_event()`, `read_events()`, `get_status()`, `get_active_run_for_session()`, `mark_terminal()`. Storage at `STATE_DIR / "runs" /` with `run_<id>.jsonl` + `_index.json`. Atomic index writes, monotonic seq per run, secret redaction on disk, active session mapping with terminal cleanup.
- `tests/test_runtime_journal.py` -- 26 tests covering create, append, read (with after_seq and limit), get_status, terminal durability, active session mapping, secret redaction, unknown-run behavior, import isolation, event round-trip, and index survival across fresh object access.
- `docs/rfcs/runtime-api-contract.md` -- updated with journal storage layout and RuntimeJournal reference.

### Design decisions

- **Extension vs new module**: `api/run_journal.py` serves legacy SSE mirroring (`_run_journal` dir, per-session). The new `api/runtime_journal.py` serves the full durable journal contract (`runs` dir, flat layout, active-session index). Keeping them separate avoids coupling the legacy journal to the contract types.
- **Unknown run behavior**: `get_status()`, `read_events()`, `mark_terminal()`, and `get_active_run_for_session()` return `None` for unknown runs. `append_event()` raises `ValueError`. Documented in class docstring.
- **Redaction**: Events are stored via `RuntimeEvent.to_dict()` which calls `api/runtime_contract._redact_payload()`. Secrets are redacted at the serialization layer before hitting disk.

### Next task

**Phase 3: WebUI runtime routes + legacy-journal mirror** — COMPLETE (see below)

---

## Phase 3: WebUI runtime routes + legacy-journal mirror — COMPLETE

| Field | Value |
|---|---|
| **Status** | Complete |
| **HEAD before commit** | `c8f4e01` |
| **Changed files** | `api/runtime_routes.py` (created), `api/routes.py` (modified), `api/streaming.py` (modified), `api/runtime_journal.py` (modified), `tests/test_runtime_routes.py` (created), `tests/test_runtime_sse_reconnect.py` (created), `tests/test_runtime_legacy_journal_mirror.py` (created), `docs/rfcs/runtime-api-contract.md` (updated) |

### Verification

```bash
# Legacy-journal mode — all 78 pass
HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal ./scripts/test.sh \
  tests/test_runtime_contract.py \
  tests/test_runtime_journal.py \
  tests/test_runtime_routes.py \
  tests/test_runtime_sse_reconnect.py \
  tests/test_runtime_legacy_journal_mirror.py \
  -v

# Default/legacy-direct mode — all 31 pass
./scripts/test.sh \
  tests/test_runtime_routes.py \
  tests/test_runtime_legacy_journal_mirror.py \
  -v
```

### Deliverables

- `api/runtime_routes.py` — Route handlers for 7 new endpoints: capabilities, active-run, run status, run events (JSON + SSE), cancel, approval, clarify. Delegates to `api/runtime_journal.py` and `api/runtime_adapter.py`.
- `api/routes.py` — Added dispatching for GET and POST runtime routes using `parsed.path.startswith("/api/runs/")` and `parsed.path.endswith("/active-run")` patterns.
- `api/streaming.py` — Added `_mirror_to_runtime_journal()` helper with SSE-to-contract event mapping. Hooks into `_run_agent_streaming` to create journal run at start and mirror events via `put()`. Gated behind `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal`.
- `api/runtime_journal.py` — `create_run()` now accepts optional `run_id` parameter for explicit stream-based ID assignment.
- `tests/test_runtime_routes.py` — 23 tests covering capabilities, active-run, run status, events (JSON + SSE), cancel/approval/clarify not_supported, default mode compatibility, module imports.
- `tests/test_runtime_sse_reconnect.py` — 5 tests for SSE replay, after_seq resume, limit, terminal cleanup, event_id stability.
- `tests/test_runtime_legacy_journal_mirror.py` — 12 tests covering mirror activation gating, SSE-to-contract mapping (run.started, token.delta, tool progress/started, done, error), disk durability, default mode non-requirement.

### Event mapping (SSE → Contract)

| SSE Event | Contract Type |
|---|---|
| `token`, `interim_assistant` | `token.delta` |
| `reasoning` | `reasoning.delta` |
| `tool` (with `event_type=tool.started`) | `tool.started` |
| `tool` (without `event_type`) | `progress` |
| `tool_complete` | `tool.done` |
| `done` | `done` (terminal) |
| `apperror`, `error` | `error` (terminal) |
| `cancel` | `done` (terminal) |
| `approval` | `approval.requested` |
| `clarify` | `clarify.requested` |
| `metering` | `usage.updated` |
| others | `run.status` or `progress` |

### /api/chat/start compatibility

Not modified. Response shape unchanged. Default behavior remains backward-compatible. Journal mirroring activates only when `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal`.

### Next task

**Phase 4: Hermes Agent /v1/runs runtime API foundation** — COMPLETE (see ../hermes-agent at f7cc6c5)

---

## Phase 5: WebUI agent-runs adapter — COMPLETE

| Field | Value |
|---|---|
| **Status** | Complete |
| **HEAD before commit** | `c57a62e` |
| **Changed files** | `api/runtime_adapter.py` (modified), `api/runtime_adapters/__init__.py` (created), `api/runtime_adapters/agent_runs.py` (created), `api/runtime_routes.py` (modified), `tests/test_agent_runs_adapter.py` (created), `tests/test_runtime_adapter_selection.py` (created), `tests/test_agent_runs_error_mapping.py` (created), `docs/rfcs/runtime-api-contract.md` (updated) |

### Verification

```bash
# New adapter tests — 76 passed
./scripts/test.sh \
  tests/test_agent_runs_adapter.py \
  tests/test_runtime_adapter_selection.py \
  tests/test_agent_runs_error_mapping.py \
  -v

# Default compatibility test — 31 passed
./scripts/test.sh \
  tests/test_runtime_routes.py \
  tests/test_runtime_legacy_journal_mirror.py \
  -v

# Agent-runs env test — 88 passed, 8 expected failures
# (8 tests in test_runtime_routes.py rely on default env and correctly hit
# the agent-runs path when HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs overrides
# the parent env; those tests are designed for legacy-direct/journal mode)
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs \
HERMES_WEBUI_AGENT_RUNS_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_AGENT_RUNS_API_KEY=test-key \
./scripts/test.sh \
  tests/test_agent_runs_adapter.py \
  tests/test_runtime_adapter_selection.py \
  tests/test_agent_runs_error_mapping.py \
  tests/test_runtime_routes.py \
  -v
```

### Deliverables

- `api/runtime_adapter.py` — Added `agent-runs` to `_VALID_RUNTIME_ADAPTER_MODES`, added `runtime_adapter_agent_runs_enabled()` helper, extended `build_runtime_adapter()` with `agent_runs_adapter_factory` parameter.
- `api/runtime_adapters/__init__.py` — Adapter factory with singleton pattern. `get_runtime_adapter()` selects based on `HERMES_WEBUI_RUNTIME_ADAPTER`. Supports `legacy-direct` (returns None), `legacy-journal` (returns None), `agent-runs` (builds `AgentRunsAdapter`).
- `api/runtime_adapters/agent_runs.py` — `AgentRunsClient` (urllib-based HTTP transport for /v1/runs contract) and `AgentRunsAdapter` (translates between WebUI `RuntimeAdapter` protocol and Hermes Agent API). Structured error handling with `AgentRunsError` covering unreachable, timeout, auth_error, and bad_response conditions. All errors redact credentials.
- `api/runtime_routes.py` — Updated all route handlers to delegate to agent-runs adapter when `runtime_adapter_agent_runs_enabled()` is True. Capabilities reports `agent-runs` mode with approval/clarify support. Run status, events (JSON + SSE), cancel, approval, and clarify all route through the adapter. Legacy-direct and legacy-journal paths preserved.
- `tests/test_agent_runs_adapter.py` — 37 tests covering adapter start_run, get_status, observe_events, controls, error redaction, route integration (capabilities, status, events, cancel, approval, clarify), legacy compatibility, HTTP contract paths, and import isolation.
- `tests/test_runtime_adapter_selection.py` — 13 tests covering default env, explicit modes, unknown adapter error, base_url requirement, singleton, factory, and whitespace handling.
- `tests/test_agent_runs_error_mapping.py` — 26 tests covering error classes, urllib-to-AgentRunsError mapping (connection refused, timeout, 401, 403, 500, 404, OSError), event mapping, and client env construction.
- `docs/rfcs/runtime-api-contract.md` — Added Hermes Agent /v1/runs adapter section documenting configuration, API contract, error mapping, implementation locations, and deferred integration notes.

### Adapter modes supported

| Mode | Env value | Behavior |
|---|---|---|
| legacy-direct | (default or explicit) | Existing direct chat path, no journal |
| legacy-journal | `legacy-journal` | Phase 3 journal mirroring |
| agent-runs | `agent-runs` | Delegates to Hermes Agent /v1/runs HTTP API |

### /api/chat/start compatibility

Preserved. Not routed through agent-runs. Response shape unchanged. Default behavior remains backward-compatible.

### Live HTTP smoke status

Deferred. Hermes Agent Phase 4 route module is not yet mounted into a live server. The adapter is verified with mocked/fake HTTP tests.

### Next task

**Phase 6: Hermex/mobile API contract** — COMPLETE (see below)

---

## Phase 6: Hermex/mobile API contract — COMPLETE

| Field | Value |
|---|---|
| **Status** | Complete |
| **HEAD before commit** | `2db908b` |
| **Changed files** | `api/mobile_routes.py` (created), `api/runtime_journal.py` (modified), `api/routes.py` (modified), `tests/test_mobile_capabilities.py` (created), `tests/test_mobile_run_dashboard.py` (created), `tests/test_mobile_pending_actions.py` (created) |

### Mobile endpoints added

| Method | Path | Description |
|---|---|---|
| GET | `/api/mobile/capabilities` | Stable capability discovery |
| GET | `/api/mobile/runs` | Active run dashboard |
| GET | `/api/mobile/pending-actions` | Pending approvals/clarifications |
| POST | `/api/mobile/pending-actions/{action_id}/resolve` | Resolve approval or clarify |
| GET | `/api/mobile/reconnect/{session_id}` | Reconnect helper (optional) |

### Verification

```bash
# Focused mobile tests — 31 passed
./scripts/test.sh \
  tests/test_mobile_capabilities.py \
  tests/test_mobile_run_dashboard.py \
  tests/test_mobile_pending_actions.py \
  -v

# Full integration/regression — 143 passed
./scripts/test.sh \
  tests/test_runtime_routes.py \
  tests/test_runtime_sse_reconnect.py \
  tests/test_runtime_legacy_journal_mirror.py \
  tests/test_agent_runs_adapter.py \
  tests/test_runtime_adapter_selection.py \
  tests/test_agent_runs_error_mapping.py \
  tests/test_mobile_capabilities.py \
  tests/test_mobile_run_dashboard.py \
  tests/test_mobile_pending_actions.py \
  -v

# Agent-runs env regression — 43 passed, 8 expected failures
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs \
HERMES_WEBUI_AGENT_RUNS_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_AGENT_RUNS_API_KEY=test-key \
./scripts/test.sh \
  tests/test_mobile_capabilities.py \
  tests/test_mobile_run_dashboard.py \
  tests/test_mobile_pending_actions.py \
  tests/test_runtime_routes.py \
  -v

# Manual import smoke
python -c 'import api.mobile_routes; print("OK")'
```

### Deliverables

- `api/mobile_routes.py` — Route handlers for 5 mobile endpoints. Delegates to `RuntimeJournal` and runtime adapters for data. Returns stable JSON payloads with null for unavailable metadata fields, redacted secrets, and clean error responses.
- `api/runtime_journal.py` — Added `list_active_runs()` method to enumerate all active runs from the index for the run dashboard.
- `api/routes.py` — Registered mobile GET routes (capabilities, runs, pending-actions, reconnect) and POST route (pending-actions resolve) in the `handle_get`/`handle_post` dispatchers.
- `tests/test_mobile_capabilities.py` — 10 tests covering /api/mobile/capabilities across all adapter modes, feature keys, secret exclusion.
- `tests/test_mobile_run_dashboard.py` — 10 tests covering active run enumeration, required fields, terminal exclusion, pending action reporting, secret redaction, null fields for unavailable metadata.
- `tests/test_mobile_pending_actions.py` — 11 tests covering pending action listing, approval/clarify resolution in legacy/agent-runs modes, validation errors, not_supported propagation, secret exclusion.

### Hermex source note

Hermex source was unavailable during this phase. The implementation is the server-side contract according to the Phase 6 specification.

### /api/chat/start compatibility

Preserved. Not modified. Mobile routes are read-only observers of the runtime state and do not alter the chat path.

### Next task

**Phase 7: Deployment health diagnostics**

