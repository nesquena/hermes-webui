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

--- 

## Phase 7: Deployment health diagnostics — COMPLETE

| Field | Value |
|---|---|
| **Status** | Complete |
| **HEAD before commit** | `3e0a1fb` |
| **Changed files** | `api/deployment_health.py` (created), `api/routes.py` (modified), `api/mobile_routes.py` (modified), `tests/test_deployment_health.py` (created), `tests/test_deployment_health_security_warnings.py` (created) |

### Deployment endpoint added

- GET `/api/deployment/health` — Read-only health diagnostics for server safety, runtime readiness, auth exposure risk, workspace readiness, and runtime adapter status. No secrets exposed.

### Mobile capabilities update

- `features.deployment_health` changed from `false` to `true` in `/api/mobile/capabilities`.

### Verification

```bash
# Focused deployment health tests — 42 passed
./scripts/test.sh tests/test_deployment_health.py tests/test_deployment_health_security_warnings.py -v

# Full regression — 185 passed
./scripts/test.sh \
  tests/test_mobile_capabilities.py tests/test_mobile_run_dashboard.py \
  tests/test_mobile_pending_actions.py tests/test_runtime_routes.py \
  tests/test_runtime_sse_reconnect.py tests/test_runtime_legacy_journal_mirror.py \
  tests/test_agent_runs_adapter.py tests/test_runtime_adapter_selection.py \
  tests/test_agent_runs_error_mapping.py tests/test_deployment_health.py \
  tests/test_deployment_health_security_warnings.py -v

# Agent-runs env regression — 46 passed, 8 expected failures
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs \
HERMES_WEBUI_AGENT_RUNS_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_AGENT_RUNS_API_KEY=test-key \
./scripts/test.sh tests/test_deployment_health.py tests/test_mobile_capabilities.py \
  tests/test_runtime_routes.py -v

# Manual import smoke
python3 -c 'import api.deployment_health; print("OK")'
```

### Deliverables

- `api/deployment_health.py` — `handle_deployment_health()` route handler producing the `/api/deployment/health` response. Includes helpers for OS isolation detection, workspace checks, provider configuration probing, agent-runs reachability checks, Tailscale/Cloudflare Tunnel detection, and structured warnings list.
- `api/routes.py` — Registered `GET /api/deployment/health` in `handle_get()` dispatcher, immediately after the existing `/api/system/health` route.
- `api/mobile_routes.py` — Flipped `features.deployment_health` from `false` to `true` so Hermex can discover the endpoint.
- `tests/test_deployment_health.py` — 24 tests covering response shape, sections, schema stability, secret exclusion, runtime adapter reporting, workspace path/exists/writable, provider configured state, warning/ok status classification, and mobile capabilities integration.
- `tests/test_deployment_health_security_warnings.py` — 18 tests covering public bind without password, HTTP public access warnings, Tailscale/Cloudflare Tunnel suppression, legacy-direct/runtime adapter warnings, agent-runs reachable/unreachable, OS isolation reporting, and secret exclusion from warnings.

### /api/chat/start compatibility

Preserved. Not modified. The deployment health endpoint is an independent read-only route.

### Deferred

- No WebUI settings page was added for this endpoint (deployment health is consumed by Hermex and diagnostics tools).
- Server smoke test deferred (requires live server; test suite provides comprehensive coverage).

### Next task

**Phase 8: Safe workspace search** — COMPLETE (see below)

---

## Phase 8: Safe workspace search — COMPLETE

| Field | Value |
|---|---|
| **Status** | Complete |
| **HEAD before commit** | `02dbf5e` |
| **Changed files** | `api/workspace_search.py` (created), `api/routes.py` (modified), `api/mobile_routes.py` (modified), `tests/test_workspace_search.py` (created), `docs/rfcs/runtime-api-contract.md` (updated) |

### Workspace search endpoint added

- GET `/api/workspace/search` — safe recursive workspace search with name and/or content search

### Mobile capabilities update

- `features.workspace_search` changed from `false` to `true` in `/api/mobile/capabilities`.

### Verification

```bash
# Focused workspace search tests — 27 passed
./scripts/test.sh tests/test_workspace_search.py -v

# Full regression — 191 passed
HERMES_WEBUI_RUNTIME_ADAPTER=legacy-direct ./scripts/test.sh \
  tests/test_workspace_search.py \
  tests/test_mobile_capabilities.py \
  tests/test_deployment_health.py \
  tests/test_deployment_health_security_warnings.py \
  tests/test_runtime_routes.py \
  tests/test_runtime_sse_reconnect.py \
  tests/test_runtime_legacy_journal_mirror.py \
  tests/test_agent_runs_adapter.py \
  tests/test_runtime_adapter_selection.py \
  tests/test_agent_runs_error_mapping.py \
  -v

# Agent-runs env regression — 37 passed (mobile + workspace search)
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs \
HERMES_WEBUI_AGENT_RUNS_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_AGENT_RUNS_API_KEY=test-key \
./scripts/test.sh tests/test_workspace_search.py tests/test_mobile_capabilities.py -v

# Manual import smoke
python3 -c 'import api.workspace_search; print("OK")'
```

### Deliverables

- `api/workspace_search.py` — `handle_workspace_search()` route handler. Name search via `os.walk` with case-insensitive basename/path matching. Content search reads text files safely, returns first matching line with line number and trimmed preview. Safety: workspace root resolution via `api.config.DEFAULT_WORKSPACE`, symlink escape blocked, ignored directories excluded, binary files skipped, files >1MB skipped for content search, secret redaction on previews.
- `api/routes.py` — Registered `GET /api/workspace/search` in `handle_get()` dispatcher, before mobile routes section.
- `api/mobile_routes.py` — `features.workspace_search` flipped from `false` to `true`.
- `tests/test_workspace_search.py` — 27 tests covering basic endpoint, name search, content search, both mode, safety, and mobile integration.
- `docs/rfcs/runtime-api-contract.md` — Added workspace search endpoint section documenting path, query params, response shape, safety properties, error responses, and mobile integration.

### /api/chat/start compatibility

Preserved. Not modified. The workspace search endpoint is independent of the chat path.

### Deferred

- No WebUI frontend UI added (endpoint + tests + docs is sufficient for this phase).
- Server smoke test deferred (requires live server; test suite provides comprehensive coverage).

### Next task

**Phase 9: Full verification and final implementation report** — COMPLETE (see below)

---

## Phase 9: Full verification and final implementation report — COMPLETE

| Field | Value |
|---|---|
| **Status** | Complete |
| **HEAD before commit** | `368ca07` |
| **Changed files** | `AGENT_HANDOFF.md` (updated), `IMPLEMENTATION_REPORT.md` (created) |

### WebUI — Focused verification (14 test files)
```
./scripts/test.sh tests/test_runtime_contract.py tests/test_runtime_journal.py \
  tests/test_runtime_routes.py tests/test_runtime_sse_reconnect.py \
  tests/test_runtime_legacy_journal_mirror.py tests/test_agent_runs_adapter.py \
  tests/test_runtime_adapter_selection.py tests/test_agent_runs_error_mapping.py \
  tests/test_mobile_capabilities.py tests/test_mobile_run_dashboard.py \
  tests/test_mobile_pending_actions.py tests/test_deployment_health.py \
  tests/test_deployment_health_security_warnings.py tests/test_workspace_search.py -v
Result: 254 passed, 0 failed in 7.32s — PASS
```

### WebUI — Agent-runs env verification
```
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs \
HERMES_WEBUI_AGENT_RUNS_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_AGENT_RUNS_API_KEY=test-key \
./scripts/test.sh tests/test_agent_runs_adapter.py \
  tests/test_runtime_adapter_selection.py tests/test_agent_runs_error_mapping.py \
  tests/test_mobile_capabilities.py tests/test_mobile_run_dashboard.py \
  tests/test_mobile_pending_actions.py tests/test_deployment_health.py \
  tests/test_deployment_health_security_warnings.py tests/test_workspace_search.py \
  tests/test_runtime_routes.py -v
Result: 188 passed, 8 failed — 8 expected failures in test_runtime_routes.py
  (tests designed for legacy-direct/journal mode; documented in Phase 5)
```

### WebUI — Full test suite
```
./scripts/test.sh
Result: 11937 passed, 5 failed, 94 skipped — all 5 failures in pre-existing
  unrelated tests (scheduled_jobs, tls, sessiondb)
```

### WebUI — Import/config smoke
```
python3 - import api.runtime_contract, api.runtime_journal, api.runtime_routes,
  api.runtime_adapters.agent_runs, api.mobile_routes, api.deployment_health,
  api.workspace_search
Result: All 7 imports OK. AgentRunsAdapter config OK.
```

### Server smoke
Deferred — requires full live server config. Automated tests provide comprehensive coverage.

### Next task

**Ready for PR review and optional Hermes Agent server route mounting.**

---

## Phase 10B — WebUI live agent-runs smoke (completed)

### State Before Phase 10B
- **Commit:** `ade8fd1`
- **Message:** `Document WebUI runtime hardening verification`

### Live Server Startup

WebUI started with agent-runs adapter pointed at live Hermes Agent runtime API:

```bash
cd hermes-webui
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs \
HERMES_WEBUI_AGENT_RUNS_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_AGENT_RUNS_API_KEY=test-key \
HERMES_WEBUI_PORT=8789 \
HERMES_WEBUI_PASSWORD=test-password \
./ctl.sh start
# Bound: 127.0.0.1:8789
# Note: Port 8787 was occupied by unrelated process; port 8789 used for smoke
```

Agent server was a standalone Python server (full `hermes gateway run` not viable due to messaging adapter dependencies):

```bash
cd hermes-agent && uv run python /tmp/hermes-agent-standalone.py
# 127.0.0.1:8642, HERMES_USE_RUNTIME_RUNS=1
# register_runtime_routes(app) delegates to RunManager
```

### WebUI Live Smoke Results

All smoke tests ran against http://127.0.0.1:8789 with cookie-based auth (password: test-password).

| Test | Endpoint | Result |
|---|---|---|
| Runtime capabilities | GET /api/runtime/capabilities | runtime_adapter="agent-runs", resumable_events=true, last_event_id=true, cancel/approval/clarify supported |
| Mobile capabilities | GET /api/mobile/capabilities | deployment_health=true, workspace_search=true, resumable_runs=true |
| Deployment health | GET /api/deployment/health | runtime_adapter="agent-runs", agent_runtime_reachable=false (standalone server lacks /v1/health; adapter works correctly) |
| Run status proxy | GET /api/runs/{run_id} | Correctly proxies to live Agent via agent-runs adapter |
| Run events proxy | GET /api/runs/{run_id}/events | Returns events matching Agent RuntimeEvent contract |
| Cancel proxy | POST /api/runs/{run_id}/cancel | 200, status "cancelled", clean response, no traceback |
| Workspace search | GET /api/workspace/search | 200, no errors, no secret leakage |

### Agent-Runs Adapter Verification

The agent-runs adapter (`api/runtime_adapters/agent_runs.py`) successfully:
- Proxied run status from Agent /v1/runs/{run_id}
- Proxied run events from Agent /v1/runs/{run_id}/events
- Proxied cancel to Agent /v1/runs/{run_id}/stop
- Reported capabilities correctly (resumable_events, last_event_id, all controls)
- Preserved secret redaction across all responses

### Post-Smoke Test Results (WebUI, agent-runs env)

```
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs \
HERMES_WEBUI_AGENT_RUNS_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_AGENT_RUNS_API_KEY=test-key \
./scripts/test.sh tests/test_agent_runs_adapter.py \
  tests/test_runtime_adapter_selection.py \
  tests/test_agent_runs_error_mapping.py \
  tests/test_runtime_routes.py \
  tests/test_mobile_capabilities.py \
  tests/test_deployment_health.py \
  tests/test_workspace_search.py -v

Result: 149 passed, 8 failed in 5.98s
  8 failures in test_runtime_routes.py — expected (tests use journal mocks designed
  for legacy-direct/journal mode; documented in Phase 5 handoff)
```

### Issues Found

1. **`agent_runtime_reachable: false`** — Standalone server exposes `/health` but deployment health checks `/v1/health`. The full gateway API server provides `/v1/health`. No functional impact on the adapter.

2. **Port 8787 occupied** — Unrelated `command-center` process. Smoke used port 8789.

3. **WebUI requires auth** — All endpoints require cookie-based authentication. Smoke tests included login step.

### Next task

**PR review / optional Hermex iOS client validation**

---

## Phase 11A — PR Review, Security Audit, and Merge-Readiness Package (completed)

### State Before Phase 11A
- **Commit:** `76a86fe`
- **Message:** `Document live agent-runs smoke verification`

### Review Scope
Full branch diff (`feat/runtime-adapter-hermex-contract` vs `master`): 28 files, 8166 insertions, 3 deletions.

### Security Audit
- No API keys, tokens, passwords, or credentials in source files
- No hardcoded personal paths
- No accidental changes to `/api/chat/start` or `/api/chat/stream`
- Agent-runs mode is opt-in — defaults to `legacy-direct`
- Workspace search: symlink traversal blocked via `Path.resolve().relative_to()` containment check
- Workspace search: secret-like patterns in previews redacted via regex
- Deployment health: API key never present in response body
- Runtime journal: events redacted via `RuntimeEvent.to_dict()` before disk write
- SSE mirroring: gated behind `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal`

### Bugs Found and Fixed

| # | File | Issue | Severity | Fix |
|---|------|-------|----------|-----|
| 1 | `api/routes.py:13950-13960` | `body.setdefault("run_id", ...)` allowed body-supplied run_id to override URL path on cancel/approval/clarify POST routes (authorization bypass) | MEDIUM | Changed to `body["run_id"] = ...` — URL-derived run_id is now always authoritative |

### Test Results

**Focused tests (default mode, post-fix):**
```
./scripts/test.sh (14 test files) -v
Result: 254 passed, 0 failed in 7.65s — PASS
```

**Agent-runs env tests (post-fix):**
```
HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs ./scripts/test.sh (7 test files) -v
Result: 149 passed, 8 failed — 8 expected failures in test_runtime_routes.py
(tests designed for legacy-direct/journal mode; documented in Phase 5)
```

**Import smoke (post-fix):**
```
All 8 modules: api.runtime_contract, api.runtime_journal, api.runtime_routes,
  api.runtime_adapter, api.runtime_adapters.agent_runs, api.mobile_routes,
  api.deployment_health, api.workspace_search
Result: All imports OK — PASS
```

### Remaining Known Issues
1. RuntimeJournal cross-instance race — separate `threading.Lock` per instance; `os.replace()` is atomic so file corruption unlikely, but stale index reads possible (WARN)
2. Dead code: `_redact_header_value` in `agent_runs.py` — never called (LOW)
3. Dead code: `_RT_SKIP_EVENTS` in `streaming.py` — never used (LOW)
4. Deployment health agent-runs check is synchronous, blocks for up to 5s (LOW)
5. Mobile routes silently swallow adapter exceptions — safe degradation, may hide bugs (LOW)
6. Workspace path may expose system username in deployment health response (LOW)
7. 8 test_runtime_routes.py tests fail under agent-runs env — expected, documented (INFO)

### Files Modified in Phase 11A
- `api/routes.py` — body run_id precedence fix

### Next task
**Phase 11C — True live AIAgent interruption and continuation, or PR submission if continuation remains out of scope.**

---

## Phase 11B — Approval/Clarify Proxy Integration (completed)

### State Before Phase 11B
- **Commit:** `620d002`
- **Message:** `Phase 11A: PR review — fix body run_id precedence in control routes`

### What Was Done
Updated the agent-runs adapter and runtime routes to handle the new Agent approval/clarify response shapes:
- `AgentRunsAdapter.respond_approval` / `respond_clarify` now map not_found/conflict/not_supported/resolved
- `handle_run_approval` / `handle_run_clarify` use shared `_control_result_response` helper mapping status→HTTP codes
- New `TestApprovalClarifyErrorMapping` test class (9 tests) covering all error states
- Mobile pending actions already correctly proxy through the adapter

### Changed Files
- `api/runtime_adapters/agent_runs.py` — error mapping for not_found/conflict/not_supported
- `api/runtime_routes.py` — `_control_result_response` helper, updated HTTP status mapping
- `tests/test_agent_runs_error_mapping.py` — `TestApprovalClarifyErrorMapping` (9 tests)

### Exact Tests
```
./scripts/test.sh tests/test_agent_runs_adapter.py \
  tests/test_agent_runs_error_mapping.py \
  tests/test_runtime_routes.py \
  tests/test_mobile_pending_actions.py -v
Result: 104 passed, 0 failed (4 files)
```

Agent-runs env: 96 passed, 8 failed (8 expected — test_runtime_routes.py tests for legacy-direct/journal mode)

### Live Smoke Status
Not performed as a full HTTP integration (requires live Agent server with injected pending actions). Adapter error mapping fully verified via unit tests. RunManager-level smoke performed in hermes-agent repo.

### Remaining Risks
1. True full-chain live smoke requires test-only pending action injection endpoints (not added — production safety)
2. No secrets leaked in any error response path (verified)

### Next task
**Phase 11C — True live AIAgent interruption and continuation, or PR submission if continuation remains out of scope.**

---

## Phase 15 — Cross-repo Runtime Integration Verification (completed)

### State Before Phase 15
- **Commit:** `d29e380`
- **Message:** `Phase 11B: Harden approval and clarify proxy handling`

### What Was Verified

The WebUI agent-runs adapter was verified to correctly proxy all required Agent runtime endpoints:

| WebUI Endpoint | Agent Endpoint | Status |
|---|---|---|
| GET /api/runs/{run_id} | GET /v1/runs/{run_id} | Verified |
| GET /api/runs/{run_id}/events | GET /v1/runs/{run_id}/events | Verified |
| POST /api/runs/{run_id}/cancel | POST /v1/runs/{run_id}/stop | Verified |
| POST /api/runs/{run_id}/approval | POST /v1/runs/{run_id}/approval | Verified |
| POST /api/runs/{run_id}/clarify | POST /v1/runs/{run_id}/clarify | Verified |
| POST /api/mobile/pending-actions/{id}/resolve (approval) | POST /v1/runs/{run_id}/approval | Verified |
| POST /api/mobile/pending-actions/{id}/resolve (clarify) | POST /v1/runs/{run_id}/clarify | Verified |

Error mapping verified:
- Agent `not_found` -> WebUI 404 (`action_not_found`)
- Agent `conflict` -> WebUI 409
- Agent success -> WebUI 200

Secret redaction verified across all paths.

### No Code Changes Required

Existing tests already comprehensively cover the agent-runs adapter (6 test files, 138+ tests).

### Test Results

**Default mode: 138 passed, 0 failed**
**Agent-runs env: 130 passed, 8 expected failures**

### Phase 18 — WebUI agent-runs live smoke coverage (completed)

**New files:**
- `scripts/smoke_agent_runs_live.sh` — live HTTP smoke for WebUI agent-runs adapter
- `tests/test_agent_runs_live_http_smoke.py` — 8 pytest tests for smoke harness construction

**Cross-repo smoke verified (via hermes-agent/scripts/smoke_cross_repo.sh):**
1. WebUI runtime capabilities — GET /api/runtime/capabilities shows agent-runs mode
2. WebUI proxied run status — GET /api/runs/{run_id} returns terminal state
3. WebUI proxied run events — GET /api/runs/{run_id}/events contains done event
4. WebUI cancel/stop — POST /api/runs/{run_id}/cancel proxies correctly
5. WebUI deployment health — GET /api/deployment/health shows agent-runs adapter

**No runtime architecture changes made.** agent-runs adapter remains opt-in via HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs.

### Test Results (Phase 18)

**Default env: 146 passed, 0 failed** (7 test files)
**Agent-runs env: 138 passed, 8 expected failures** (test_runtime_routes.py specific)

### Files Updated

- `scripts/smoke_agent_runs_live.sh` — new
- `tests/test_agent_runs_live_http_smoke.py` — new
- `AGENT_HANDOFF.md` — Phase 18 section added
- `IMPLEMENTATION_REPORT.md` — Phase 18 section added
- `PR_DESCRIPTION.md` — Phase 18 changes added

---

## Phase 19 — Real-credential Smoke Readiness (verified)

**Phase 19 completed on the Agent side.** No WebUI code changes were required.

### Phase 19 Results

- Agent deterministic smoke (Agent-only): 7/7 PASSED
- Agent deterministic smoke (cross-repo): 11/11 PASSED
- Agent runtime tests: 409 passed, 0 failed (16 files)
- WebUI default env tests: 146 passed, 0 failed
- WebUI agent-runs env tests: 138 passed, 8 expected failures
- Real DeepSeek smoke: SKIPPED (DEEPSEEK_API_KEY not set in env)
- Agent approval/clarify deterministic trigger: wired (events verified)
- Messaging-adapter smoke plan: documented in `docs/messaging-adapter-live-smoke.md`

### Architecture Preserved

- WebUI agent-runs adapter unchanged
- Default adapter (legacy-direct) remains default
- Runtime routes, journal, mobile APIs, deployment health unchanged


---

## Phase 20 -- Agent-Runs Real Smoke Readiness

Date: 2026-07-02

### Verification summary

- Deterministic cross-repo Agent to WebUI smoke: PASSED, 11 passed, 0 failed.
- DEEPSEEK_API_KEY: not present in the active environment.
- Real DeepSeek cross-repo smoke: SKIPPED.
- WebUI proxied status/events: PASSED via deterministic cross-repo smoke.
- WebUI cancel/stop proxy: PASSED via deterministic cross-repo smoke.
- Runtime adapter default behavior: unchanged; agent-runs remains opt-in.
- No secrets were committed.

### Remaining deferred items

1. Re-run cross-repo real DeepSeek smoke when DEEPSEEK_API_KEY is available.
2. Verify WebUI proxied status/events against a real DeepSeek-backed Agent run.
3. Preserve deterministic cross-repo smoke as fallback coverage when credentials are unavailable.
