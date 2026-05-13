# Capy WebUI Runbook

This runbook preserves the operational context for the Capy WebUI on Brendan's Mac Studio so future sessions do not rely on chat memory alone.

## Architecture

- Repo: `/Users/bschmidy10/hermes-webui`
- Process manager: user launchd LaunchAgent
- LaunchAgent: `/Users/bschmidy10/Library/LaunchAgents/com.capy.webui.plist`
- Service label: `com.capy.webui`
- Entrypoint: `/Users/bschmidy10/hermes-webui/server.py`
- Python: `/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python`
- Local URL: `http://127.0.0.1:8787/`
- Local health: `http://127.0.0.1:8787/health`
- Tailnet health: `https://capy.tail9c6e3.ts.net/health`
- State dir: `/Users/bschmidy10/.hermes/webui-mvp`
- Default workspace: `/Users/bschmidy10/workspace`
- Bot name: `Capy`
- Logs:
  - `/Users/bschmidy10/.hermes/webui-mvp/launchd.out.log`
  - `/Users/bschmidy10/.hermes/webui-mvp/launchd.err.log`
- Log rotation:
  - LaunchAgent: `/Users/bschmidy10/Library/LaunchAgents/com.capy.logrotate.plist`
  - Script: `/Users/bschmidy10/.hermes/scripts/rotate-capy-logs.py`
  - Label: `com.capy.logrotate`

The WebUI stays bound to localhost. Tailscale Serve exposes it privately to the tailnet by proxying `https://capy.tail9c6e3.ts.net/` to `http://127.0.0.1:8787/`. On this Mac, the Tailscale CLI lives at `/Applications/Tailscale.app/Contents/MacOS/tailscale`.

## Decisions

### Use user LaunchAgent, not root LaunchDaemon

Reason: the WebUI only needs to survive Ghostty/Terminal closure and restart after crashes while Brendan's GUI session is loaded. A root LaunchDaemon would add sudo/admin complexity and different GUI/TCC semantics.

Tradeoff: this is not a pre-login boot service. If pre-login availability becomes required, design a separate root LaunchDaemon with explicit admin approval.

### Supervise `server.py` directly

Reason: `start.sh` delegates through bootstrap code and can background the real server. launchd should supervise the long-lived Python process directly.

### Keep localhost binding

Reason: `127.0.0.1` plus Tailscale Serve limits exposure. Do not bind to `0.0.0.0` unless authentication and network exposure are reviewed.

## Capy Spaces product architecture

Capy Spaces is evolving toward a generic safe creator loop rather than only a catalog of demos:

1. Prompt or tool request produces a bounded, metadata-only Space/widget spec.
2. The spec is previewed in a sandbox and visually QA'd before any durable write.
3. Patch/repair flows produce metadata-only receipts and events.
4. Approved changes are committed through the revision system so rollback/time-travel remains available.

Generated/imported widget bodies, raw HTML, scripts, renderer/source/data payloads, prompt echoes, and credential-looking values stay disabled or quarantined until explicit sandbox tests cover richer execution. Visible demo/smoke surfaces should prove capabilities with bounded checklists and receipts, not by rendering generated widget bodies.

## Health checks

```bash
curl -fsS http://127.0.0.1:8787/health
curl -fsS https://capy.tail9c6e3.ts.net/health
launchctl print gui/$(id -u)/com.capy.webui
```

Expected: health JSON status is `ok`; launchd state is `running`.

## Restart

```bash
uid=$(id -u)
launchctl kickstart -k gui/$uid/com.capy.webui
curl -fsS http://127.0.0.1:8787/health
```

## Log rotation

Capy uses a user-level, no-sudo log rotation LaunchAgent to keep launchd logs bounded while preserving the same log file inodes that launchd writes to.

```bash
plutil -lint /Users/bschmidy10/Library/LaunchAgents/com.capy.logrotate.plist
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile /Users/bschmidy10/.hermes/scripts/rotate-capy-logs.py
launchctl print gui/$(id -u)/com.capy.logrotate
```

Expected: plist lint is OK, py_compile is quiet, and launchd has the `com.capy.logrotate` job loaded.

## Update notes

### 2026-05-12 20:20 CDT — Hermes Agent + WebUI upstream sync

- Hermes Agent repo: `/Users/bschmidy10/.hermes/hermes-agent`
  - Branch: `main`
  - Updated by rebasing local Capy commits onto `origin/main`.
  - Post-update head: `0258b0467`.
  - Rollback checkpoint: `backup/capy-before-update-20260512-195910`.
  - Verification: targeted gateway/runtime tests passed with `-o addopts=`, `hermes doctor` passed with only expected missing optional API-key warnings, gateway restarted via user LaunchAgent.
- WebUI repo: `/Users/bschmidy10/hermes-webui`
  - Branch: `feat/capy-spaces-foundation`
  - Merged `origin/master`; merge commit `4959d8e`.
  - Rollback checkpoint: `backup/capy-before-update-20260512-195910`.
  - Manual conflict resolutions kept Capy Spaces nav/shell plus upstream dashboard/logs/sidebar changes, kept Bash 3.2-compatible `ctl.sh`, preserved route alias safety checks, and made health checks use live stream registry aliases.
  - Test adjustments isolated session-recovery global state and updated the Capy Spaces static-shell assertion for rail-click options.
  - Verification: full WebUI test suite passed: `5760 passed, 2 skipped, 3 xpassed, 8 subtests passed`; `py_compile`/`bash -n` passed; WebUI restarted via `com.capy.webui`; local `/health` returned `ok`; browser visual smoke loaded `Capy` and the Capy Spaces panel without JS errors.
- Rollback commands if needed:

```bash
# WebUI rollback to the pre-merge checkpoint
cd /Users/bschmidy10/hermes-webui
git switch feat/capy-spaces-foundation
git reset --hard backup/capy-before-update-20260512-195910
launchctl kickstart -k gui/$(id -u)/com.capy.webui
curl -fsS http://127.0.0.1:8787/health

# Hermes Agent rollback to the pre-rebase checkpoint
cd /Users/bschmidy10/.hermes/hermes-agent
git switch main
git reset --hard backup/capy-before-update-20260512-195910
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway
/Users/bschmidy10/.local/bin/hermes doctor
```

### 2026-05-13 13:29 CDT — Post-update smoke, Spaces enablement, and fork push

- Visible-browser chat smoke passed:
  - Created a fresh WebUI conversation.
  - Sent `Reply with exactly WEBUI_SMOKE_OK.`
  - Received `WEBUI_SMOKE_OK` in about 4 seconds.
  - Refreshed the page and confirmed the conversation recovered without `Session not found`.
- Capy Spaces was enabled locally by adding `HERMES_WEBUI_SPACES_ENABLED=1` to the user LaunchAgent environment in `/Users/bschmidy10/Library/LaunchAgents/com.capy.webui.plist`.
  - LaunchAgent backup: `/Users/bschmidy10/Library/LaunchAgents/com.capy.webui.plist.bak-20260513-130413`.
  - After changing the plist, `launchctl kickstart` alone was not enough to pick up the new environment. The working sequence was `launchctl enable`, `launchctl bootstrap gui/$(id -u) ...`, then `launchctl kickstart -k`.
- Capy Spaces browser QA passed enough for continued development:
  - `/api/spaces` is no longer disabled in the browser session.
  - Recovery/control-plane metadata renders.
  - Product-home canvas renders with starfield shell, welcome card, resource links, demo shortcuts, creator dock, and panel buttons.
  - No browser console JS errors were reported.
  - Visual QA screenshot: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_5735efca2bc5491fae9a6b89b1de2dcc.png`.
  - Follow-up polish findings: lower canvas is sparse, `open_in_new` material icon labels render as literal text, welcome-card close button is cramped, and recovery-hard-gate messaging may confuse users while no Spaces exist.
- Cleaned the Hermes Agent repo by moving an unrelated untracked screenshot out of the repo:
  - From: `/Users/bschmidy10/.hermes/hermes-agent/comfyui-capy-workflows-visible.png`
  - To: `/Users/bschmidy10/.hermes/artifacts/comfyui/comfyui-capy-workflows-visible-20260513-131150.png`
- Pushed WebUI branch to Brendan's fork:
  - Remote: `capy-fork`
  - Branch: `feat/capy-spaces-foundation`
  - Head: `4b5a722`
  - GitHub suggested PR URL: `https://github.com/bschmidy10/hermes-webui/pull/new/feat/capy-spaces-foundation`
  - No GitHub Actions workflow runs were listed for this branch in either the fork or upstream at check time.
- Ten-minute post-update monitor passed:
  - Log: `/tmp/capy-post-update-monitor-20260513-131923.log`
  - 20/20 health checks returned `status=ok`.
  - `sessions=1`, `active_streams=0`, and `active_runs=0` throughout.
  - No `traceback`, `error`, `exception`, or `fatal` markers appeared in the recent launchd stdout/stderr scan.
- Final LaunchAgent state: `com.capy.webui` running, local health `ok`.

### 2026-05-13 16:24 CDT — Capy Spaces product-home polish

- Addressed the 13:29 polish findings on branch `feat/capy-spaces-foundation`:
  - Resource links no longer render literal `open_in_new` text; they use a compact `↗` affordance with accessible link labels.
  - Demo and panel buttons no longer expose literal Material icon names such as `newspaper`, `currency_bitcoin`, `smart_display`, or `arrow_forward`.
  - Empty Spaces state now uses a denser two-column product grid with explicit `Create first Space`, `Run research walkthrough`, and `Run kanban walkthrough` actions.
  - Welcome-card close button now has a rounded 36px hit area and more visual breathing room.
  - Recovery wording now says `Safe recovery controls` and `Generated widget execution: disabled`, while retaining metadata-only quarantine behavior.
- Verification:
  - Focused UI/product-home/recovery tests: `4 passed`.
  - Spaces UI behavior + foundation suites: `428 passed`.
  - Full WebUI test suite: `5779 passed, 2 skipped, 3 xpassed, 8 subtests passed`.
  - `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.
  - Local health returned `ok` with `active_streams=0` and `active_runs=0` after WebUI LaunchAgent restart.
  - Browser console check had no warnings/errors after switching to Capy Spaces.
  - Visual QA screenshot: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_9646e732b6d042dea2c55df19c73cc1d.png`.

## Debug checklist

1. Check local health before restarting.
2. Check whether port 8787 has a listener.
3. Inspect launchd state.
4. Inspect WebUI stdout/stderr logs.
5. If local health works but tailnet URL fails, inspect Tailscale Serve.
6. If `/health` works but `/` redirects to `/login`, debug auth/session state rather than service uptime.

Commands:

```bash
curl -fsS http://127.0.0.1:8787/health 2>&1 || true
lsof -nP -iTCP:8787 -sTCP:LISTEN 2>&1 || true
launchctl print gui/$(id -u)/com.capy.webui 2>&1 || true
tail -n 80 /Users/bschmidy10/.hermes/webui-mvp/launchd.err.log 2>/dev/null || true
/Applications/Tailscale.app/Contents/MacOS/tailscale serve status 2>&1 || true
```

## Config/profile pitfalls

The LaunchAgent may set `HERMES_CONFIG_PATH` for the default profile. Named profiles must still resolve their config from `~/.hermes/profiles/<name>/config.yaml`.

Regression tests for this behavior live in:

- `tests/test_capy_config_profile_reasoning.py`
- `tests/test_profile_path_security.py`
- `tests/test_reasoning_show_hide.py`

Run targeted verification with:

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_capy_config_profile_reasoning.py tests/test_profile_path_security.py tests/test_reasoning_show_hide.py -q -o 'addopts='
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile api/config.py api/streaming.py
```

## Recall guidance for future agents

- Load the `capy-mac-studio-operations` skill for WebUI/gateway/memory work.
- Use `session_search` when Brendan says "last time", "remember", "continue", or similar.
- Do not read or print `.env` secret values. If config shape must be inspected, redact values.
