# Preserve expanded history during bounded refresh

Classification: upstream-candidate
Base: fork combined release `02c390e6313d232539ac8e01c93bd11c8df7cb23`, including upstream `be5c07175049fc32e093231a8d7fc4b7127c0f0e`.
Maintenance owner: fork maintainers.
Upstream status: not-filed. Private details removed: yes.

Problem: a bounded same-session refresh can re-hide rows loaded while its request was pending.
Root cause: discarding a stale projection hint does not enlarge the already-issued bounded response.
Change: if the loaded window changed and the returned head lies after the current loaded head, fetch canonical full history once, recheck load ownership, then project using the current validated boundary. Do not merge stale client rows into canonical history.
Expected: current loaded history stays accessible; changed revisions/boundaries use canonical fallback. Normal refresh makes no extra request. A failed catch-up leaves the current messages intact and follows existing error handling.
Reproduction/verification: `tests/browser_bounded_refresh_race.py` covers bounded, fully loaded, repeated expansion during catch-up, session switch, load generation change, failed catch-up, changed revision and unchanged-window cases in Chromium/WebKit at desktop/mobile widths. Base reproduces 100 loaded rows becoming 50.
Compatibility: client message projection only; no endpoint or persisted-state change. One full catch-up request per affected invocation is deliberate correctness work in a race, not a network optimization. Concurrent refresh invocations can each request catch-up; this patch does not introduce a request-deduplication state machine. Uses existing fork loaded-window helpers and same-session reload hints; those are reconstruction prerequisites.
Rollback: revert this change/test to restore prior refresh behavior. Retire when upstream canonical refresh preserves concurrently expanded windows.
