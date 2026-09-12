# Bound missed-completion recovery to loaded history

Classification: upstream-candidate
Base: fork combined release `02c390e6313d232539ac8e01c93bd11c8df7cb23`, including upstream `be5c07175049fc32e093231a8d7fc4b7127c0f0e`.
Maintenance owner: fork maintainers.
Upstream status: not-filed. Private details removed: yes.

Problem: missed-completion recovery mounts full history and retains stale pagination offsets; a delayed recovery can also overwrite a successor turn started while the GET is pending.
Root cause: `_restoreSettledSession` bypassed loaded-window projection and only checked stream ownership before awaiting. A stale recovery status could fall through to destructive fallback cleanup.
Change: reuse the existing loaded-window projection and synchronize pagination metadata at recovery adoption; recheck existing stream ownership after await; stop both inline and deferred fallback cleanup for stale recovery. Boolean recovery callers also recheck before their continuation, and reconnect status probes recheck before wiring another source. The last-ditch recovery watchdog checks ownership before offline/hidden-tab deferral can mutate successor state.
Expected: recovered answer and canonical metadata remain visible without implicitly loading older history or overwriting a successor. Explicitly loaded full history and changed revisions/boundaries still use canonical adoption.
Reproduction/verification: `tests/browser_recovery_loaded_window.py`, Chromium/WebKit, both activity modes and desktop/mobile widths. Normal, fully loaded, changed revision/boundary, older-during-GET, switched session and replacement-stream cases, including scene-empty stream_end and reconnect-error continuation/composer ownership, plus the real watchdog callback fired deterministically during its pending GET after the tab becomes hidden. Base mounts 3302 instead of 52; pre-guard candidate overwrites replacement stream. Existing recovery/artifact/scroll focused tests pass; artifact extraction test loads the real ownership helpers rather than stubbing their result.
Compatibility: transport and durable session are unchanged; model context/regeneration policy is untouched. Full JSON payloads remain. Uses the fork loaded-window helpers; upstream reconstruction must supply the equivalent helper first.
Rollback: revert this logical change/tests. Retire once upstream covers both loaded-boundary and post-await ownership semantics.
