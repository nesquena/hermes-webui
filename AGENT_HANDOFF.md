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

**Phase 2: WebUI durable runtime journal**

Wire `api/runtime_contract.py` types into `api/run_journal.py` so journal entries use the canonical `RuntimeEvent` and `RuntimeStatus` shapes. Do not change live streaming behavior. Do not modify `api/streaming.py` except for import discovery.
