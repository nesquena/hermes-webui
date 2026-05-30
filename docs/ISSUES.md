# Upstream Issues — Root Cause Analysis

## #1256: Browser tools fail with "Playwright not installed"

### Root Cause
The check lives in **hermes-agent** (upstream), not hermes-webui:

```
hermes-agent/tools/browser_tool.py → check_browser_requirements()
```

`check_browser_requirements()` does not recognize CDP (Chrome DevTools Protocol) mode — it only looks for a local Playwright/Puppeteer install. When the agent runs in CDP mode (connecting to an existing browser), the check still fails.

### WebUI side
The WebUI already passes `CLI_TOOLSETS` correctly per-request. The `enabled_toolsets` field in the cron/chat config is dynamic and works as intended.

### Fix required
The fix must happen in `hermes-agent/tools/browser_tool.py`:
- `check_browser_requirements()` should skip the Playwright check when CDP mode is configured
- Or add a `BROWSER_MODE=cdp` env var that bypasses the local browser requirement

### Workaround
Use `CLOUD_BROWSER=true` or configure `browser.base_url` to point to a remote CDP endpoint. This bypasses the local Playwright requirement.

---

## WebUI queue persistence / refresh churn bundle

### Upstream tracking
- PR: https://github.com/nesquena/hermes-webui/pull/3109
- Branch: `fix/webui-refresh-scroll-and-persistence`

### Scope
This bundle covers the regressions that made the chat feel unstable during normal use:
- queue disappearing after refresh or tab detour
- draft-only empty sessions getting wiped on restore
- active session refresh / scroll churn making long chats hard to read
- intermittent `Jump to question` rendering gaps

### Local proof / files
- `static/ui.js`
- `static/sessions.js`
- `static/boot.js`
- `api/routes.py` (SSE header adjustment in PR #3109)
- tests:
  - `tests/test_issue660.py`
  - `tests/test_queue_combine_persistence.py`
  - `tests/test_issue1360_streaming_scroll_hardening.py`
  - `tests/test_issue_active_session_refresh_regressions.py`
  - `tests/test_issue2246_question_jump.py`

### Recovery note after upstream update
If a future upstream update drops these behaviors locally, first check whether PR #3109 merged cleanly. If not, restore from branch `fix/webui-refresh-scroll-and-persistence` or re-derive from the files above.

---

## #3181: Hidden pre-compression snapshots can consume the visible pin quota

### Upstream tracking
- Issue: https://github.com/nesquena/hermes-webui/issues/3181

### Symptom
Pinning fails with:

```
Up to 3 sessions can be pinned. Unpin one before pinning another.
```

...even though the sidebar shows zero or fewer than three pinned sessions.

### Root Cause
This is a two-part bug:

1. `api/streaming.py::_preserve_pre_compression_snapshot(...)` can leave an archived parent snapshot logically pinned unless the snapshot save path explicitly clears `pinned`.
2. `POST /api/session/pin` counts pinned sessions from the persisted index / in-memory session map without excluding sidebar-hidden rows such as `pre_compression_snapshot` sessions.

Result: hidden lineage snapshots silently consume the visible pin quota.

### Local fix bundle
- `api/streaming.py`
  - save pre-compression snapshots with `pinned = False`
- `api/routes.py`
  - exclude `_hide_from_default_sidebar(...)` sessions from pin-limit counting
- regression tests:
  - `tests/test_compression_snapshot_runtime_clear.py`
  - `tests/test_issue_hidden_snapshot_pin_limit.py`

### Local cleanup already needed once
A bad local state can leave many hidden pinned snapshots in `~/.hermes/webui/sessions/_index.json`, so fixing code alone may not immediately clear the user-visible error. Existing bad rows may need one-time unpin cleanup.

### Recovery note after upstream update
If #3181 is still open after an update, re-check these files first:
- `api/streaming.py`
- `api/routes.py`
- `tests/test_compression_snapshot_runtime_clear.py`
- `tests/test_issue_hidden_snapshot_pin_limit.py`
