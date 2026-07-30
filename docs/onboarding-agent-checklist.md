# Agent-assisted onboarding checklist

This checklist is for an AI assistant helping a human install, reinstall, or
debug Hermes WebUI onboarding. It does not replace the human first-run wizard.
Use it before running bootstrap commands, inspecting logs, or recommending a
cleanup path.

If you are an AI assistant, read this file before assisting with onboarding,
bootstrap, provider setup, reinstall, or first-run support.

## Role split

The human operator owns:

- choosing the install path
- choosing the provider and model
- entering API keys, OAuth codes, and passwords
- approving any cleanup of a real Hermes home
- approving any external exposure outside localhost

The assistant owns:

- using isolated trial directories unless the human explicitly says otherwise
- checking non-secret status endpoints and logs
- explaining which step passed or failed
- collecting redacted evidence for Discord or GitHub support
- stopping before destructive cleanup, credential handling, or public exposure

## Hard safety rules

- Do not delete, move, or overwrite the real `~/.hermes` directory unless the
  human explicitly asks for that exact action.
- Do not print API keys, OAuth tokens, cookies, full `.env` files, full
  `auth.json` files, or password hashes.
- Do not modify real cron jobs, real sessions, real profiles, or real memory
  files during an onboarding trial.
- Do not expose WebUI on a public interface without password protection and
  explicit human approval.
- Do not proxy or tunnel local service checks such as `localhost`,
  `127.0.0.1`, private LAN addresses, or Docker container loopback paths.

## Pre-flight

Confirm the basic context:

```bash
pwd
git branch --show-current
git rev-parse --short HEAD
python3 --version
```

Check whether repo-local environment overrides will affect bootstrap:

```bash
test -f .env && grep -n 'HERMES_HOME\|HERMES_WEBUI_STATE_DIR\|HERMES_WEBUI_PORT\|HERMES_WEBUI_HOST' .env
```

If `.env` exists, do not print the full file. Inspect only the specific
non-secret keys needed to understand the active Hermes home, WebUI state
directory, port, or host.

## Isolated local trial

Use an isolated Hermes home and WebUI state directory for a reinstall or support
trial. This keeps the test away from the operator's real memory, sessions,
profiles, credentials, and cron state.

```bash
mkdir -p ~/hermes-onboarding-test
HERMES_HOME=~/hermes-onboarding-test/.hermes \
HERMES_WEBUI_STATE_DIR=~/hermes-onboarding-test/webui \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py
```

Open:

```text
http://127.0.0.1:8789
```

The bootstrap writes a port-specific log under the selected WebUI state
directory:

```text
~/hermes-onboarding-test/webui/bootstrap-8789.log
```

For daemon-style installs, `ctl.sh` writes the daemon log to the active
`HERMES_HOME` by default:

```text
~/.hermes/webui.log
```

When using the isolated trial environment, prefer the bootstrap command above
unless the human specifically wants to validate `ctl.sh`.

## Non-secret evidence commands

After the server starts, collect status without secrets:

```bash
curl -sS http://127.0.0.1:8789/health
curl -sS http://127.0.0.1:8789/api/onboarding/status
find ~/hermes-onboarding-test -maxdepth 3 -type f | sort
tail -n 120 ~/hermes-onboarding-test/webui/bootstrap-8789.log
```

When summarizing `/api/onboarding/status`, focus on:

- `completed`
- `system.hermes_found`
- `system.imports_ok`
- `system.config_path`
- `system.config_exists`
- `system.setup_state`
- `system.provider_configured`
- `system.provider_ready`
- `system.chat_ready`
- `system.current_provider`
- `system.current_model`
- `system.current_base_url`
- `system.env_path`

Do not paste the full payload if it contains unexpected sensitive local paths
or values. Redact paths and provider details when the human asks for a public
GitHub or Discord support report.

## Pass criteria

A local onboarding trial passes when:

- `/health` returns successfully.
- `/api/onboarding/status` returns JSON.
- The wizard appears when `completed` is false.
- The wizard stays out of the way when `completed` is true or
  `HERMES_WEBUI_SKIP_ONBOARDING=1` is intentionally set.
- `system.hermes_found` and `system.imports_ok` match the expected bootstrap
  state.
- `system.provider_ready` and `system.chat_ready` become true after the human
  completes a provider path that should support chat.
- `system.config_path` and `system.env_path` point inside the intended isolated
  `HERMES_HOME` during a trial.
- WebUI files are written under the intended `HERMES_WEBUI_STATE_DIR`.

If the human chooses a provider that must be completed in the CLI, passing can
mean the wizard correctly points them to `hermes model` or `hermes auth` rather
than trying to collect unsupported credentials in the browser.

## Failure triage

If the server does not start:

- check the bootstrap log
- check for a port conflict on `8789`
- confirm Python can run `bootstrap.py`
- confirm `.env` is not overriding the isolated directories or port

If onboarding reports `agent_unavailable`:

- confirm the bootstrap found or installed Hermes Agent
- check whether the running Python can import `run_agent.AIAgent`
- use `docs/troubleshooting.md`, especially the `AIAgent not available` flow

If onboarding reports `provider_incomplete`:

- confirm whether the provider is API-key based, OAuth based, or local
- let the human enter credentials or run the CLI auth flow
- do not ask the human to paste secrets into chat

If a local model server does not probe successfully:

- from native macOS/Linux, use `http://127.0.0.1:<port>/v1` when the server is
  on the same host
- from Docker Desktop, use `http://host.docker.internal:<port>/v1`
- from another LAN machine, use the server's LAN IP and `/v1`
- remember that `localhost` inside a container is the container itself

If password or reverse-proxy behavior is confusing:

- keep the first pass on `127.0.0.1`
- require password protection before exposing WebUI beyond localhost
- include the reverse proxy shape in the support report without pasting tokens
  or cookies

## Final support report

Use this shape when reporting results to the human, Discord, or GitHub:

```text
Install path:
OS / Python:
Repo commit:
Command used:
WebUI URL:
State isolation:
Health result:
Onboarding status summary:
Files created or changed:
Log excerpt:
Pass/fail:
Next recommended action:
```

Redact secrets and private paths before posting publicly.
