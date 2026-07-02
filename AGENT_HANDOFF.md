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

## Next recommended phase

**Phase 1: Hermex/mobile WebUI contract design**

Define the Hermex contract (event shapes, control surface, diagnostic endpoints) on paper first, without changing the hot path. Derive from the existing `docs/rfcs/hermes-run-adapter-contract.md` and `api/runtime_adapter.py` event families. The contract should cover:

1. Event envelope and cursor/reconnect semantics for mobile clients
2. Control surface: observe, status, cancel, approval, clarify, queue, goal
3. Diagnostic endpoints: `/api/hermex/health`, run status, session-to-run mapping
4. Feature flag: `HERMES_WEBUI_RUNTIME_ADAPTER` already has `legacy-direct` / `legacy-journal` / `runner-local` modes; Hermex should be gated behind its own selector or re-use `legacy-journal` + explicit allowlisting
5. Response-shape parity constraint: do not expand `/api/chat/start` public shape
