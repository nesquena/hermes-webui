# Local Modifications

This file tracks changes applied to the local Hermes WebUI installation
that are not (yet) merged upstream. Each entry records what was changed,
why, when, and how to revert or upstream it.

Format: one entry per modification, newest first.

---

## 2026-06-07 — Purge __pycache__ before self-update restart

**Status:** PR open (https://github.com/nesquena/hermes-webui/pull/3774)

**Branch:** `fix/stale-pycache-after-self-update` (forked from `master` at v0.51.310)

**What:** After a WebUI self-update pulls new Hermes Agent code and restarts
via `os.execv()`, Python's `__pycache__/` bytecode cache can survive and
serve stale class definitions to the new process. This causes
`AttributeError` when new methods (e.g. `_apply_user_default_headers`)
don't exist on the cached `AIAgent` class.

**Changes:**
- `api/updates.py`: Added `_purge_agent_pycache()` — deletes all
  `__pycache__/` dirs in agent and WebUI repos inside `_schedule_restart()`
  right before `os.execv()`.
- `api/config.py`: Extended `verify_hermes_imports()` to check that
  `AIAgent._apply_user_default_headers` exists after import.
- `tests/test_pycache_purge.py`: 3 unit tests.

**How to revert:** `git checkout master && git branch -D fix/stale-pycache-after-self-update`

**How to re-apply if upstream changes:** Cherry-pick `12e72fa1` onto the
new base, or rebase the branch:
```bash
git fetch upstream
git checkout fix/stale-pycache-after-self-update
git rebase upstream/master
```

**Trigger:** Hermes Agent update (19:57 CST 2026-06-07) added
`_apply_user_default_headers` method to `AIAgent`. WebUI self-update pulled
both repos but the new server process got a stale `AIAgent` class from
cached bytecode, causing `AttributeError` on first chat request.

---
