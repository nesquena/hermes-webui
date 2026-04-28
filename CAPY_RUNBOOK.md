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
