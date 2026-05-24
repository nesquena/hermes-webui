# Profile Gateway Tile Runtime Contract

- **Status:** Proposed
- **Author:** Hermes Agent
- **Created:** 2026-05-16

## Problem

The Profile Ops Console has an "Agent Gateway" tile, but its current behavior is not a reliable representation of the real Hermes Gateway runtime. The user-visible failures are:

1. A gateway can already be running when the WebUI opens, but the tile can still show stopped or fail to converge.
2. The tile is profile-local in the UI, but some status paths still behave like local-process checks rather than a profile runtime contract.
3. Docker and multi-container layouts break PID-based status checks because WebUI and the gateway may not share a PID namespace.
4. Failed starts surface only as a transient toast/title tooltip; users need an obvious info affordance, hover detail, and a click-to-open copyable error window.

This change needs a small runtime contract, not another one-off tile patch. The docs establish the important boundary that Hermes Gateway is a background process for a specific `HERMES_HOME`, and current Hermes Agent code shows the runtime files the WebUI must respect: `{HERMES_HOME}/gateway.pid` for PID tracking and `{HERMES_HOME}/gateway_state.json` for runtime health. Separate profile homes naturally have separate gateway process state. The WebUI should model that directly.

### 2026-05-16 stuck-stopping incident

After an initial implementation pass, manual testing in a shadow WebUI exposed a more specific failure mode: the Default profile Gateway tile could become permanently stuck in `stopping` after rapid on/off toggles and an attempted start on the `coder` profile.

Evidence gathered without mutating Gateway processes:

- The preserved Default profile WebUI state file contained:

  ```json
  {"phase":"stopping","phase_started_at":"2026-05-16T13:53:32.219899Z","last_error":null,"last_run_at":"2026-05-16T13:53:20.609165Z","desired_enabled":false}
  ```

- The actual Default Gateway runtime was alive under the profile `HERMES_HOME`, with `gateway.pid` and `gateway_state.json` pointing to the same process.
- The `coder` Gateway was not running; its runtime status recorded a failed startup because the Telegram bot token was already locked by the Default Gateway (`telegram-bot-token_lock`).
- The shadow WebUI process had been started before later backend-file edits, so its loaded Python code could lag behind the checkout on disk.
- The shadow log showed repeated `GET /api/profile/gateway/status?name=default` calls after the final stop request, but the user still saw a disabled `stopping` toggle. That means status polling existed, but the runtime contract did not reliably reconcile a stale stop intent back to an actionable state in the code that was actually serving the preview.

Root-cause conclusion:

- `stopping` was treated as a blocking UI phase rather than a bounded control attempt.
- The backend wrote `phase = stopping` before invoking stop, but an alive Gateway signal could remain true because the Gateway was service-managed, auto-restarted, or never actually stopped.
- The frontend disabled the toggle for `stopping`; without a backend transition timeout/reconciliation that the running server actually served, the UI had no recovery path.
- `coder` start failure was a separate profile-scoped failure caused by a shared external credential lock, not evidence that the Default Gateway had stopped.

This incident tightens the contract below: transient phases are **fresh intents only**, never indefinite locks. A stale `stopping` state must become either `stopped` if no alive signal remains, or an actionable `running`/attention state if the Gateway is still alive after the stop grace window.

## Goals

- Allow each profile/agent to start and stop its own gateway from the profile detail section.
- Treat selected profile, active chat profile, and gateway profile as separate concepts.
- Detect the current gateway state on initial render, including when the gateway was started before the WebUI process.
- Use efficient periodic health checks while the relevant profile UI is visible.
- Work in same-process, WSL, single-container, and split-container/Docker-isolated deployments.
- Present failed/unavailable states with an info icon that supports:
  - hover/focus summary,
  - click/keyboard open of a copyable detail dialog,
  - server-side sanitized error content.
- Keep the tile honest: controls must call real backend lifecycle operations or clearly report why control is unavailable.

## Non-goals

- Do not redesign the broader Profile Ops Console layout.
- Do not add a separate provider/API-key/credentials panel to the Gateway tile.
- Do not make the selected profile implicitly active for chat.
- Do not add fake success states or optimistic "running" labels that are not backed by a status signal.
- Do not require browser-side calls directly to the gateway API server; CORS, auth, and mixed deployment topology should remain backend concerns.
- Do not implement Hermes Agent upstream lifecycle endpoints as part of this WebUI change. The WebUI should be ready to use them later if they appear.

## Existing behavior and likely root causes

### Frontend

`static/panels.js` already has the right broad shape:

- `_gatewayStateByProfile` caches state per profile.
- `_profileGatewayTile(p)` renders the tile and seeds state from `p.gateway_running`.
- `_refreshGatewayStatus(profileName)` calls `/api/profile/gateway/status?name=...`.
- `_onGatewayToggle(profileName)` posts `{ name, action: "start" | "stop" }` to `/api/profile/gateway`.
- `_startGatewayPoller(profileName)` polls every 1.5s only while state is `starting` or `stopping`.
- Failure state exists, but the error affordance is mostly a title/CSS tooltip and not a copyable dialog.

Gaps:

- Stable states are not periodically refreshed, so post-start crashes, external starts/stops, and already-running gateways may not be corrected consistently after the first render path.
- The failed-state info icon is not a first-class interactive control.
- The UI only understands `stopped`, `starting`, `running`, `stopping`, and `failed`; Docker-isolated status needs an explicit `unknown` or `unavailable` state rather than pretending stopped.

### Backend

`api/profiles.py` exposes:

- `profile_gateway_status_api(name)`
- `profile_gateway_control_api(name, action)`
- local start via detached `hermes gateway run --replace`
- local stop via `hermes_cli.gateway.stop_profile_gateway()`
- `.gateway-state.json` for WebUI transition state and last-run metadata

Gaps:

- `_read_gateway_pid(profile_home)` expects `gateway.pid` to be a plain integer. Current Hermes Agent `gateway.status.write_pid_file()` writes JSON metadata such as `{ "pid": ..., "kind": "hermes-gateway", "argv": ..., "start_time": ... }`, with legacy integer support only in `gateway.status._read_pid_record()`. This mismatch can make the Profile Gateway status API miss a live gateway.
- `profile_gateway_status_api()` is based on WebUI-local PID visibility. In split-container Docker, PID checks fail even when the gateway is alive.
- The root/global `/api/gateway/status` already uses `api.agent_health.build_agent_health_payload()`, which has a freshness fallback for `gateway_state.json`, but profile-specific status does not yet use that same cross-container model.
- Control currently always uses a local subprocess/stop helper. In a two-container topology, that can start a gateway in the WebUI container rather than the intended Hermes Agent container.

### Docker / isolated topologies

Current compose files document:

- single-container: WebUI and agent run together, local process control is valid.
- two-container: `hermes-agent` runs `gateway run`; `hermes-webui` shares `hermes-home` but does not share PID namespace.
- three-container: agent, dashboard, and WebUI are split services.

In split-container layouts, shared files are reliable for status if the gateway writes fresh runtime state, but local process control is not necessarily reliable. The WebUI must pick a controller intentionally instead of assuming `subprocess.Popen([hermes, gateway, run])` targets the right runtime.

## Recommended approach

Introduce a profile-scoped Gateway Runtime Contract in the WebUI backend and have the tile consume only that contract.

### Alternatives considered

| Approach | Good for | Trade-offs | Recommendation |
|---|---|---|---|
| Keep current local PID/status logic and patch the UI | Smallest diff | Still wrong for JSON PID files, Docker PID namespaces, and external starts/stops | Reject |
| Browser probes `http://localhost:8642/health` directly | Simple for local API-server users | Breaks CORS, auth, remote hosts, containers, HTTPS/mixed content, and per-profile targeting | Reject |
| Backend profile runtime contract with pluggable status/control adapters | Accurate, testable, profile-scoped, works across local and Docker layouts | Slightly more backend structure | Recommended |

## Proposal

### 1. Profile Gateway status contract

`GET /api/profile/gateway/status?name=<profile>` should return a single canonical payload consumed by the tile:

```json
{
  "ok": true,
  "profile": "default",
  "phase": "running",
  "desired_enabled": true,
  "control_available": true,
  "status_source": "pid|runtime_file|remote_health|state_file|none",
  "health": {
    "alive": true,
    "state": "alive|down|unknown",
    "reason": "pid_alive|cross_container_freshness|gateway_not_running|gateway_stale_running_state|not_configured"
  },
  "pid": 12345,
  "updated_at": "2026-05-16T...Z",
  "last_error": null,
  "detail": null
}
```

Allowed `phase` values:

| Phase | Meaning | Toggle behavior |
|---|---|---|
| `running` | Gateway is observed alive for this profile | On; click requests stop |
| `starting` | User requested start and the gateway has not yet proven alive | Busy/disabled |
| `stopping` | User requested stop and the gateway has not yet proven stopped | Busy/disabled |
| `stopped` | Gateway is observed not running or has no status and no desired run | Off; click requests start |
| `failed` | Last lifecycle action failed | Off/error; click retries start; info available |
| `unknown` | There is evidence of a gateway but WebUI cannot verify liveness | Indeterminate; control depends on adapter; info available |
| `unavailable` | WebUI cannot inspect/control this profile gateway safely | Disabled; info available |

Compatibility rule: the frontend should continue to tolerate older responses that only contain `phase`, `last_error`, `phase_started_at`, and `pid`.

### 2. Status source order

For a profile home, backend status should use the first decisive signal:

1. **Canonical Hermes PID helper**
   - Use `gateway.status.get_running_pid(profile_home / "gateway.pid", cleanup_stale=False)` when importable.
   - This supports both current JSON PID records and legacy integer PID files.
   - Same PID namespace success means `phase = running`, `status_source = pid`.

2. **Runtime health file**
   - Read `{profile_home}/gateway_state.json`.
   - If `gateway_state == "running"` and `updated_at` is fresh enough, treat as `running` with `status_source = runtime_file`.
   - Use the existing `api.agent_health` freshness semantics: fresh means within two gateway cron ticks, currently 120 seconds.
   - This is the preferred split-container/shared-volume path.

3. **Configured remote health probe**
   - If the profile has an explicit server-side health target, probe `/health/detailed` then `/health` with a short timeout.
   - This should be backend-only, with loopback/container-service allowlisting sufficient to avoid SSRF.
   - Successful detailed health with `gateway_state == "running"` means `running`, `status_source = remote_health`.
   - This covers isolated Docker layouts without shared PID namespace, and no browser CORS is involved.

4. **WebUI transition state**
   - Read `.gateway-state.json` for desired/transient phase and sanitized `last_error`.
   - `starting` may remain transient for the existing grace window.
   - If transient exceeds grace and no decisive alive signal exists, become `failed` with a sanitized stderr tail/detail.
   - If `running` was only WebUI state and no decisive live signal exists, downgrade to `unknown` or `stopped` depending on runtime evidence:
     - stale `gateway_state.json == running` => `unknown`, not stopped;
     - no runtime evidence => `stopped`.

5. **No evidence**
   - Return `stopped`, `status_source = none`.

### 3. Desired state and transition file

Keep `.gateway-state.json` as WebUI-owned profile control state, but make its role explicit:

```json
{
  "desired_enabled": true,
  "phase": "starting",
  "phase_started_at": "...Z",
  "last_error": null,
  "last_run_at": "...Z",
  "last_control_source": "webui"
}
```

Rules:

- Starting sets `desired_enabled = true`, `phase = starting`, clears `last_error`.
- Stopping sets `desired_enabled = false`, `phase = stopping`, clears `last_error`.
- Successful observed running sets `phase = running` but preserves the original `phase_started_at` if it came from a start transition.
- Successful observed stopped clears transient phase and keeps `desired_enabled = false`.
- Failed start sets `phase = failed`, `desired_enabled = false`, and stores a sanitized `last_error`.
- A gateway already running outside WebUI must be displayed as running even if `.gateway-state.json` has no desired state yet.

### 4. Control adapter contract

`POST /api/profile/gateway` should remain the tile lifecycle endpoint:

```json
{ "name": "default", "action": "start" }
```

Allowed actions remain only `start` and `stop`. Do not resurrect `restart` for this tile.

Backend implementation should dispatch through a small control adapter abstraction:

```text
GatewayControlAdapter
- name: local | docker_exec | remote | unavailable
- capabilities: { can_status, can_start, can_stop }
- status(profile_home, profile_name) -> status evidence
- start(profile_home, profile_name) -> control result
- stop(profile_home, profile_name) -> control result
```

Initial adapters:

1. **local**
   - Used when WebUI and gateway should run in the same runtime namespace.
   - Start: current detached `hermes gateway run --replace`, with `HERMES_HOME` scoped to profile.
   - Stop: current `stop_profile_gateway()` scoped to profile.
   - Must use a resolved Hermes binary, not a bare `hermes` that may be missing from `PATH`.

2. **docker_exec**
   - Used only when explicitly configured for deployments where the intended gateway runtime is inside a Docker container/service.
   - Start/stop execute inside the target container so WebUI does not accidentally start a second gateway in its own container.
   - This is the required first implementation path for "WebUI on host/WSL, Hermes isolated in Docker" and split-container Compose deployments until Hermes Agent exposes official remote lifecycle endpoints.
   - Configuration should be server-side and profile-aware, for example under WebUI config:

     ```yaml
     webui:
       gateway:
         control:
           mode: docker_exec
           container: hermes-agent
     ```

   - This adapter should be optional in the codebase, but Docker-isolated install guides/Compose examples that advertise WebUI gateway controls must configure it. If Docker CLI/socket is unavailable at runtime, return `unavailable` with a clear sanitized error.

3. **remote health/status**
   - Status-only until Hermes Agent exposes authenticated lifecycle endpoints.
   - Uses `/health/detailed` and `/health` for detection.
   - If configured as the only available adapter, tile status can be accurate but controls must be disabled with an info message rather than faking start/stop.

4. **future remote lifecycle**
   - If Hermes Agent later adds official gateway lifecycle endpoints, the adapter can swap to those without changing the frontend contract.

Adapter selection must be explicit and deterministic. Do not infer Docker control from the mere presence of `/.dockerenv`; single-container WebUI deployments still need local control.

### 5. Profile independence

Every status/control call must resolve the profile home from the `name` parameter, not from the active WebUI profile. This preserves the requirement that each Agent/Profile can enable its gateway independently from the profile section.

Expected behaviors:

- Starting `researcher` does not switch the chat active profile.
- Stopping `default` does not stop `coder`.
- Opening a non-active profile detail view still polls and controls that selected profile's gateway.
- `/api/profiles` may include a lightweight `gateway_running` hint, but the detail tile must call the canonical status endpoint before trusting it.

### 6. Frontend tile behavior

#### Initial render

When profile detail opens:

1. Render immediately from cached/seeding state for visual stability.
2. Immediately call `_refreshGatewayStatus(profileName)`.
3. Start a stable poller while the profile detail remains visible.

#### Polling cadence

- Transient phases (`starting`, `stopping`): 1.5s, matching current behavior.
- Stable visible phases (`running`, `stopped`, `failed`, `unknown`, `unavailable`): 10-15s.
- Stop polling when leaving the Profiles panel, closing detail, switching selected profile, or `document.visibilityState === "hidden"`.
- Backend may cache health probes for a short TTL, e.g. 1-2s per profile, so rapid UI re-renders do not repeatedly hit Docker/HTTP probes.

#### State labels

| Phase | Status label | Switch label |
|---|---|---|
| `running` | Running | Gateway: On |
| `starting` | Starting | Gateway: Starting… |
| `stopping` | Stopping | Gateway: Stopping… |
| `stopped` | Stopped | Gateway: Off |
| `failed` | Start Failed | Gateway: Failed — click to retry |
| `unknown` | Unknown | Gateway: Check status |
| `unavailable` | Unavailable | Gateway: Unavailable |

For `unknown` and `unavailable`, the switch must reflect `aria-checked="false"` or `aria-checked="mixed"` only if the component remains semantically a switch. If `mixed` is not well-supported for `role="switch"`, render the main control disabled and expose a separate retry/check button.

#### Error/info affordance

Failed, unknown, and unavailable states should render a real interactive info control inside the status pill:

```html
<button type="button" class="profile-gateway-info" aria-label="View gateway status details">
  ⓘ
</button>
```

Requirements:

- Hover and keyboard focus show a short sanitized summary.
- Click/Enter/Space opens a modal/dialog using existing WebUI dialog patterns.
- Dialog content includes:
  - profile name,
  - phase,
  - status source,
  - health reason,
  - sanitized detail/error text,
  - Copy button,
  - Close button.
- Error text should be selectable/copyable. A read-only `<textarea>` or `<pre>` inside a dialog is acceptable.
- Do not rely on CSS `::after` tooltips as the only error surface; they are not copyable and are weak for accessibility.

### 7. Security and safety

- Sanitize all backend error/detail strings before returning them to the browser.
- Continue redacting obvious secret patterns (`api_key`, `token`, `secret`, `password`).
- Return bounded detail strings; keep stderr tails short.
- Server-side remote health probes must not become SSRF primitives:
  - prefer explicit config,
  - validate URL scheme and host,
  - allow Docker service names only when the deployment config opted into that mode,
  - use short timeouts.
- Do not expose raw process `argv`, environment, executable path, or credential-bearing logs in status payloads.
- Starting/stopping remains a same-origin POST protected by existing CSRF checks.

## Requirement mapping

| Requirement | Design response |
|---|---|
| Gateway can be enabled independently for each Agent from Profiles | Profile-scoped status/control endpoint resolves by `name`, not active profile; control writes profile-local desired state and starts/stops that profile's `HERMES_HOME` gateway. |
| Tile accurately detects current state, including already-running gateway | Initial status refresh, stable visible polling, JSON PID support, runtime-file freshness, and optional remote health probe. |
| Efficient health check | Visible-only frontend polling plus short backend TTL; transient phases poll faster than stable phases. |
| Functional when Hermes is isolated in Docker | Status supports shared runtime files and backend remote health. Lifecycle control uses explicit `docker_exec` adapter when the intended gateway runtime is a container. If no control adapter exists, tile reports unavailable instead of lying. |
| Failed enable shows info icon with hover and click-copy dialog | Replace passive title/CSS-only tooltip with a real interactive info button and dialog containing copyable sanitized detail. |

## Testing and verification

### Backend unit tests

Add or extend tests around `api.profiles.profile_gateway_status_api` and adapter helpers:

- JSON `gateway.pid` written by Hermes Agent is detected as running.
- Legacy integer `gateway.pid` still works.
- Fresh `gateway_state.json` with `gateway_state == "running"` reports `phase = running` when PID lookup returns none.
- Stale `gateway_state.json == running` reports `phase = unknown`, not `stopped` or `failed`.
- Fresh non-running runtime state reports stopped/down as appropriate.
- `.gateway-state.json` transient `starting` promotes to running when PID/runtime/remote health proves alive.
- Transient `starting` beyond grace becomes failed with sanitized stderr detail.
- Profile name resolution is independent of active WebUI profile.
- Start/stop target the selected profile's home.
- Unsupported action `restart` still returns 400.
- Adapter unavailable returns a safe `phase = unavailable`, `control_available = false`, and sanitized detail.
- Remote health probe rejects unsafe URLs and times out quickly.
- Docker adapter command construction is profile-scoped and testable without actually invoking Docker.

### Frontend tests / checks

Add JS/unit-style coverage where the project already has lightweight static tests, or cover through browser verification if no JS harness exists:

- Opening a profile detail triggers an immediate status refresh.
- Stable polling continues while the detail view is visible and stops when leaving Profiles.
- `unknown` and `unavailable` render distinct labels and do not show as stopped.
- Failed/unknown/unavailable info button is keyboard reachable.
- Clicking the info button opens a dialog with copyable text.
- The toggle does not send `restart`.
- The toggle is disabled or clearly unavailable when backend says `control_available = false`.

### Integration / manual verification

Run focused tests first, then broader profile/gateway coverage:

```bash
pytest tests/test_profile_gateway_status.py tests/test_profile_gateway_control.py tests/test_profile_gateway_routes.py tests/test_issue1879_cross_container_gateway_liveness.py -v --timeout=60
```

Then run browser verification in the WebUI:

1. Open Profiles.
2. Open a stopped profile; confirm tile shows stopped after initial refresh.
3. Start gateway; confirm starting -> running or failed with info detail.
4. Stop gateway; confirm stopping -> stopped.
5. Start a gateway externally before loading WebUI; confirm tile detects running.
6. Simulate a failed start; confirm info icon hover and click-copy dialog.
7. In split-container/shared-volume setup, confirm fresh `gateway_state.json` reports running even when PID lookup is not visible.
8. Confirm controls target the selected profile, not necessarily active chat profile.

## Rollout plan

1. Backend status correctness:
   - Replace int-only PID parsing with canonical Hermes status helper/fallback.
   - Add runtime-file freshness to profile gateway status.
   - Extend payload with health/detail/control fields while preserving old fields.

2. Backend control adapter:
   - Extract current local process control into `local` adapter.
   - Add explicit unavailable result when control cannot be performed safely.
   - Add optional `docker_exec` adapter behind config.
   - Update Docker/WSL install examples that run Hermes outside the WebUI process to configure the control adapter, or explicitly document status-only behavior when lifecycle control is not available.

3. Frontend tile state:
   - Teach tile about `unknown`, `unavailable`, `control_available`, and `detail`.
   - Start immediate refresh and stable visible poller.
   - Keep transient poller fast.

4. Error/detail UX:
   - Replace passive failed-state tooltip with interactive info button.
   - Add copyable dialog and keyboard support.

5. Verification:
   - Add backend regression tests first.
   - Add frontend/browser checks.
   - Run focused gateway/profile suite.
   - Manually verify local WSL path before Docker-specific follow-up.

## Open questions

No blocking product question remains for the local/WSL and shared-volume Docker behavior.

Non-blocking follow-up: choose the exact operator-facing config shape for `docker_exec` control before implementing that adapter. The status contract should be implemented so that adding this adapter does not require another frontend rewrite.

## Acceptance criteria

- A running profile gateway is detected on WebUI/profile-detail load without requiring the user to click the tile.
- Profile Gateway Tile status is accurate for current JSON PID files.
- Profile Gateway Tile status does not falsely show stopped in split-container layouts when fresh `gateway_state.json` proves the gateway is running.
- Stable polling updates externally changed gateway state while the profile detail is visible.
- Start/stop actions target the selected profile's `HERMES_HOME`.
- If start fails, the tile shows failed state, an info icon is visible, hover/focus reveal a summary, and clicking opens a copyable detail dialog.
- If control is unsafe/unavailable in an isolated deployment, the tile says unavailable with detail instead of starting a gateway in the wrong container.
- Docker-isolated setups documented by this repo either provide a working configured control adapter or clearly mark the tile as status-only/unavailable with copyable setup detail.
- Existing profile UI constraints remain intact: no duplicate settings panels, no active-profile conflation, no extra hero summary beam, and no `restart` action.
