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
