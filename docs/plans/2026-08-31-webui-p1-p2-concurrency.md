# Hermes WebUI P1/P2 Concurrency and UX Implementation Plan

> **For Hermes:** Execute with Codex workers in isolated worktrees, then run spec and quality review before merge.

**Goal:** Improve Hermes WebUI responsiveness, replay correctness, durability, and user visibility when multiple long-running conversations are active concurrently.

**Architecture:** Preserve the current Python stdlib + vanilla-JS deployment model and the existing in-process Hermes `AIAgent` execution path. Prefer bounded, keyed, incremental state over broad rebuilds; preserve the existing rAF/incremental streaming renderer. Keep the WebUI launchd service and live user state untouched during development and verification.

**Base:** `origin/master` (`e168b67e4278df618d1cab61fdb3a8dc55b29a81`), repository default branch is `master`.

## Workstreams

### A. Run-journal replay integrity

Files: `api/run_journal.py`, `tests/test_run_journal_seq_cache.py`.

- Make sequence reservation and append atomic under one per-run/path critical section.
- Preserve compatibility with direct `append_run_event()` callers.
- Add a deterministic concurrent `RunJournalWriter.append_sse_event()` regression proving on-disk order is gapless and `read_session_run_events()` does not return `replay_noncontiguous`.

### B. Session/state file safety and graceful shutdown

Files: `api/models.py`, `api/workspace.py`, `server.py`, `ctl.sh` only if necessary, focused tests.

- Enforce explicit private permissions for session JSON, backups, indexes, workspace metadata, and settings on create/replacement/recovery paths.
- Preserve atomicity and existing compatibility; do not modify production state.
- Add graceful drain semantics for routine supervisor shutdown: stop admission, expose/record draining state, wait for active workers within a bounded deadline, then exit; preserve explicit force behavior.
- Add tests for permissive umask, replacement, and active-worker shutdown behavior.

### C. Session/profile metadata latency

Files: `api/route_session_list_cache.py`, `api/routes.py`, `api/profiles.py`, `api/models.py`, focused tests.

- Make `/api/sessions` serve a last-known snapshot immediately while stale rebuild runs single-flight in the background.
- Keep active-run overlay fresh without re-running full CLI/Codex/lineage projection.
- Separate cheap profile metadata from expensive skill counts/gateway probes; use bounded TTL + background refresh.
- Keep profile/source scoping and invalidation semantics correct.
- Add tests for stale-while-revalidate, concurrent callers, cache invalidation, and three-large-session behavior.

### D. In-flight persistence and multi-conversation UX

Files: `static/messages.js`, `static/ui.js`, `static/sessions.js`, tests.

- Replace repeated whole-transcript localStorage serialization with compact recovery metadata/delta or bounded tail while preserving reconnect recovery guarantees.
- Keep selected conversation fully live; add compact background activity/status projections for other active sessions without rendering all token streams.
- Preserve current rAF/incremental markdown path and session-switch ownership guards.
- Add browser/static tests for three sessions, switching, reconnect, cancellation, and bounded persistence bytes.

### E. Client bootstrap/cache and lifecycle efficiency

Files: `static/index.html`, `static/sw.js`, relevant JS/CSS/tests.

- Keep auth/navigation network-first but make version-pinned shell assets cache-first or stale-while-revalidate.
- Lazy-load non-chat surfaces where compatible with the no-build architecture; split locale loading only if tests prove safe.
- Remove mobile zoom prohibition while preserving iOS input behavior and touch targets.
- Reduce duplicate per-tab global refresh work where possible, with explicit lifecycle disposal.
- Add browser-level/static tests for cache versioning, offline shell integrity, responsive behavior, and timer/EventSource lifecycle.

## Global acceptance gates

- No credentials, production state, or live WebUI process touched.
- All changes are based on this branch, not the dirty operator checkout.
- Each workstream adds a focused failing test before implementation and passes its focused suite after implementation.
- Preserve existing API compatibility and current user-facing behavior outside the targeted fixes.
- Run focused performance/concurrency/replay/security/browser tests, syntax/lint checks, and the broad suite in an isolated process after integration.
- Verify with a synthetic three-session stress harness: no replay noncontiguous result, bounded persistence, no cross-session state contamination, bounded sidebar latency, and correct completion visibility.
- Review the final diff for scope, secrets, generated artifacts, and docs accuracy before opening the PR.
