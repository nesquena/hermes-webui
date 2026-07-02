# PR: WebUI Runtime Hardening — Hermex Contract, Agent-Runs Adapter, and Diagnostics

## Summary

Adds comprehensive WebUI-side runtime infrastructure: canonical event contract (`api/runtime_contract.py`), durable append-only runtime journal (`api/runtime_journal.py`), route handlers for runtime and mobile endpoints (`api/runtime_routes.py`, `api/mobile_routes.py`), an agent-runs HTTP adapter that delegates to the Hermes Agent `/v1/runs` API (`api/runtime_adapters/agent_runs.py`), deployment health diagnostics (`api/deployment_health.py`), and safe recursive workspace search (`api/workspace_search.py`). All changes are additive; default behavior is backward-compatible.

## Motivation

WebUI currently instantiates `AIAgent` directly for chat. This PR provides the WebUI-side infrastructure to:

1. Mirror SSE chat events to a durable runtime journal (legacy-journal mode)
2. Delegate run execution to the Hermes Agent `/v1/runs` HTTP API (agent-runs mode)
3. Serve Hermex-compatible mobile API endpoints for capabilities, run dashboard, and pending actions
4. Provide deployment health diagnostics for server safety, auth exposure, and runtime readiness
5. Provide safe recursive workspace search with traversal blocking and secret redaction

## Major Changes

### New modules
- `api/runtime_contract.py` — RuntimeEvent, RuntimeStatus dataclasses, validation, redaction
- `api/runtime_journal.py` — Durable JSONL journal with active-session index
- `api/runtime_routes.py` — 7 route handlers (capabilities, active-run, status, events JSON+SSE, cancel, approval, clarify)
- `api/runtime_adapter.py` — Adapter protocol, builder, feature-flag helpers (extended)
- `api/runtime_adapters/__init__.py` — Adapter factory with singleton
- `api/runtime_adapters/agent_runs.py` — HTTP client and adapter for Hermes Agent /v1/runs API
- `api/mobile_routes.py` — 5 Hermex-compatible mobile endpoints
- `api/deployment_health.py` — Deployment health diagnostics
- `api/workspace_search.py` — Safe recursive workspace search

### Modified modules
- `api/routes.py` — Route dispatch for runtime, mobile, deployment health, and workspace search paths
- `api/streaming.py` — SSE-to-contract event mirroring for legacy-journal mode

### New tests (14 files, 254 tests)
- `tests/test_runtime_contract.py` (16), `test_runtime_journal.py` (26), `test_runtime_routes.py` (23), `test_runtime_sse_reconnect.py` (5), `test_runtime_legacy_journal_mirror.py` (12), `test_agent_runs_adapter.py` (37), `test_runtime_adapter_selection.py` (13), `test_agent_runs_error_mapping.py` (26), `test_mobile_capabilities.py` (10), `test_mobile_run_dashboard.py` (10), `test_mobile_pending_actions.py` (12), `test_deployment_health.py` (24), `test_deployment_health_security_warnings.py` (18), `test_workspace_search.py` (27)

## API Changes

### New endpoints (14 total)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/runtime/capabilities` | Adapter mode, feature flags |
| GET | `/api/sessions/{sid}/active-run` | Active run for session |
| GET | `/api/runs/{run_id}` | Run status |
| GET | `/api/runs/{run_id}/events` | Event replay (JSON or SSE) |
| POST | `/api/runs/{run_id}/cancel` | Cancel run |
| POST | `/api/runs/{run_id}/approval` | Resolve approval |
| POST | `/api/runs/{run_id}/clarify` | Resolve clarification |
| GET | `/api/mobile/capabilities` | Hermex capability discovery |
| GET | `/api/mobile/runs` | Active run dashboard |
| GET | `/api/mobile/pending-actions` | Pending approvals/clarifications |
| POST | `/api/mobile/pending-actions/{id}/resolve` | Resolve action |
| GET | `/api/mobile/reconnect/{sid}` | Reconnect helper |
| GET | `/api/deployment/health` | Health diagnostics |
| GET | `/api/workspace/search` | Safe workspace search |

### Unchanged endpoints
- `/api/chat/start` — preserved, no behavior change
- `/api/chat/stream` — preserved, journal mirroring only active in legacy-journal mode

## Config Flags

| Env Var | Values | Default | Description |
|---------|--------|---------|-------------|
| `HERMES_WEBUI_RUNTIME_ADAPTER` | `legacy-direct`, `legacy-journal`, `agent-runs` | `legacy-direct` | Selects runtime adapter mode |
| `HERMES_WEBUI_AGENT_RUNS_BASE_URL` | HTTP URL | (required for agent-runs) | Hermes Agent API base URL |
| `HERMES_WEBUI_AGENT_RUNS_API_KEY` | string | (optional) | API key for agent-runs auth |

## Tests Run

```
254 focused tests (default mode): 254 passed, 0 failed
157 agent-runs env tests: 149 passed, 8 expected failures
11937 full test suite: 11937 passed, 5 pre-existing unrelated failures
Import smoke: 8 modules — all PASS
Live smoke (Phase 10B): PASS
```

## Compatibility Notes

- Default: `legacy-direct` — existing direct chat path, no change
- Agent-runs is opt-in — `HERMES_WEBUI_RUNTIME_ADAPTER=agent-runs` required
- `/api/chat/start` and `/api/chat/stream` preserved — zero behavior change
- Journal mirroring only activates when `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal`

## Known Limitations

- Approval/clarify resolution depends on Agent-side support (currently returns `not_supported`)
- RuntimeJournal cross-instance race possible — separate instances can't share locks, though `os.replace()` is atomic
- Dead code: `_redact_header_value` in agent_runs.py, `_RT_SKIP_EVENTS` in streaming.py
- Mobile routes silently swallow adapter exceptions
- Workspace search double-walks for `type=both` queries
- 8 test_runtime_routes.py tests fail under agent-runs env (expected — tests designed for legacy-direct/journal mode)

## Phase 15 Changes (this PR)

Phase 15 verifies the WebUI side of cross-repo runtime integration. No code changes were required — the existing agent-runs adapter and test suite already correctly implement the Agent runtime contract.

**Verification results:**
- Agent contract shapes (create, status, events, stop, approval, clarify) all correctly proxied
- Error mapping verified: not_found→404, conflict→409, success→200
- Secret redaction preserved across all response paths
- 138 tests passed (default mode), 130 passed/8 expected failures (agent-runs mode)
- 345 Agent runtime tests pass with the same contract

### Phase 18 — WebUI agent-runs live smoke coverage

**New files:**
- `scripts/smoke_agent_runs_live.sh` — live HTTP smoke script for WebUI agent-runs
- `tests/test_agent_runs_live_http_smoke.py` — 8 tests for smoke harness construction

**Live smoke verified (cross-repo):**
1. Runtime capabilities → agent-runs mode
2. Proxied run status → terminal state
3. Proxied run events → done event
4. Cancel/stop → proxies correctly
5. Deployment health → agent-runs adapter

**Tests:** 146 passed (default), 138 passed/8 expected failures (agent-runs env)

**No architecture changes.** agent-runs remains opt-in.

### Phase 19 — Real-credential Smoke Readiness

No WebUI code changes. Phase 19 validated:
- Deterministic cross-repo smoke (--fake): 11/11 PASSED
- Default tests: 146 passed, 0 failed
- Agent-runs env tests: 138 passed, 8 expected failures
- Real DeepSeek smoke: SKIPPED (no key)
- Agent-side approval/clarify deterministic trigger wired (events verified)

## Rollback Plan

```bash
unset HERMES_WEBUI_RUNTIME_ADAPTER
unset HERMES_WEBUI_AGENT_RUNS_BASE_URL
unset HERMES_WEBUI_AGENT_RUNS_API_KEY
# Default behavior (legacy-direct) restored.
# To fully revert: git revert <phase-commit-range>
```
