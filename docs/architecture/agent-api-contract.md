# WebUI to hermes-agent source dependency contract

This document records the current WebUI dependency on the hermes-agent source
tree for issue #2491. It is an audit and replacement contract only; it does not
change runtime behavior or Docker wiring.

Run the deterministic audit with:

```powershell
python scripts/audit_agent_source_dependencies.py
python scripts/audit_agent_source_dependencies.py --format markdown
```

The JSON output is repo-relative and grouped by stable dependency class IDs so
follow-up PRs can remove one class at a time without relying on exact line
fixtures.

## Current boundary

The multi-container setup still shares the agent source tree with the WebUI:

- `docker-compose.two-container.yml` mounts `hermes-agent-src` at `/opt/hermes`
  in the agent service and read-only at
  `/home/hermeswebui/.hermes/hermes-agent` in the WebUI service.
- `docker-compose.three-container.yml` uses the same source volume pattern.
- `docker_init.bash` documents that the WebUI-side mount is read-only and uses
  a staged copy when installing from the source checkout.
- `docs/docker.md` explicitly documents that this is not a filesystem trust
  boundary: the WebUI cannot write the mount, but it still installs and imports
  code from it.

The durable target is that multi-container WebUI should not need a direct
`hermes-agent-src` mount. The WebUI should communicate with hermes-agent through
HTTP endpoints and a small versioned client/shared-schema package where pure
helpers are genuinely shared code.

## Dependency classes

| Audit class | Current surface | Replacement classification |
| --- | --- | --- |
| `docker_agent_source_volume` | Compose files and Docker docs expose `hermes-agent-src` and `/opt/hermes` to make the agent checkout visible to WebUI. | Remove the WebUI source mount only after startup install and runtime imports have migrated. This needs Docker/compose follow-up work, not a runtime behavior change in this audit PR. |
| `startup_dependency_install` | `api/startup.py` discovers `HERMES_WEBUI_AGENT_DIR` or `$HERMES_HOME/hermes-agent`; `server.py` calls `auto_install_agent_deps()` after import verification fails; `docker_init.bash` installs from the staged agent source. | Replace source-tree pip installs with a packaged hermes-agent WebUI client plus an agent health/version capability contract. Keep `HERMES_WEBUI_AGENT_DIR` during migration as an override/debug path, but it should stop being required in normal multi-container startup. |
| `runtime_auxiliary_model_metadata` | `api/streaming.py`, `api/routes.py`, `api/config.py`, and `api/providers.py` import `agent.auxiliary_client`, `agent.model_metadata`, `agent.models_dev`, `hermes_cli.models`, and `agent.account_usage`. | Existing provider/model WebUI endpoints can keep serving UI data where they already wrap agent helpers. Missing surfaces need hermes-agent endpoints or a client package for auxiliary task config, text auxiliary calls, context length, token estimate, provider catalog, and account usage. |
| `runtime_session_state` | `api/streaming.py`, `api/goals.py`, and `api/state_sync.py` import `hermes_state.SessionDB` directly. | Move cross-container state reads and writes behind hermes-agent session/state endpoints. WebUI-only presentation state can remain local, but agent session storage should not be opened from the WebUI container. |
| `runtime_gateway_provider` | `api/streaming.py` and `api/routes.py` import `hermes_cli.runtime_provider`; adapter helpers such as `agent.anthropic_adapter` are also imported for gateway normalization. | Provider resolution, runtime routing, and gateway invocation should be hermes-agent API calls. WebUI can keep request validation and display formatting, but it should not import runtime provider internals from the agent checkout. |
| `webui_local_or_client_package` | WebUI imports `hermes_cli.auth`, `hermes_cli.config`, `hermes_cli.plugins`, `hermes_cli.profiles`, `hermes_cli.goals`, `agent.skill_utils`, `agent.credential_pool`, and `hermes_constants`. | Pure schemas, constants, and parsing helpers can move into a small versioned client/shared package. Privileged data such as credential pools, auth status, profile mutation, plugin discovery, and goal persistence need hermes-agent endpoints. UI-only formatting can remain in WebUI. |

## Replacement contract

### Existing endpoint candidates

The WebUI already exposes provider, model, profile, route, and streaming
handlers that callers use today. Those handlers can remain as WebUI HTTP routes
when they only format UI responses, but their implementations should stop
loading agent modules directly. Good candidates for reusing the current WebUI
route shape while changing its backend dependency are:

- Provider/model catalog routes currently backed by `hermes_cli.models`.
- Auxiliary title/compression paths currently backed by `agent.auxiliary_client`.
- Context-window and token-estimate paths currently backed by
  `agent.model_metadata`.
- Runtime-provider choices currently backed by `hermes_cli.runtime_provider`.

### New hermes-agent endpoints needed

These surfaces require an agent-owned endpoint because they read agent state,
perform provider/runtime decisions, or expose privileged data:

- SessionDB/session state read and write operations now using
  `hermes_state.SessionDB`.
- Runtime provider resolution and gateway normalization now using
  `hermes_cli.runtime_provider` and `agent.anthropic_adapter`.
- Auxiliary task execution and configuration now using `agent.auxiliary_client`.
- Credential/auth/account usage access now using `agent.credential_pool`,
  `hermes_cli.auth`, and `agent.account_usage`.
- Profile, plugin, goal, and skill operations that mutate or discover
  agent-owned resources.

### Client/shared package candidates

These items can be kept out of the live agent API if they are pure, versioned,
and safe to import without the agent source tree:

- Shared constants currently imported from `hermes_constants`.
- Provider/model schema names and non-privileged catalog shape definitions.
- Pure skill/profile parsing helpers that do not touch agent-owned state.
- Typed response/request models for the new hermes-agent endpoints.

### WebUI-local items

The WebUI can keep code that is only presentation, validation, or routing glue:

- User-facing diagnostics that display whether `HERMES_WEBUI_AGENT_DIR` is set.
- Route-level request validation and response formatting.
- WebUI-only caches and client-facing state that do not open agent SessionDB.
- Docker documentation describing the transition while both paths are supported.

## Audit expectations

`tests/test_agent_source_dependency_audit.py` pins the contract shape:

- Docker/compose source sharing is reported.
- Startup dependency installation and `HERMES_WEBUI_AGENT_DIR` are reported.
- Runtime auxiliary/model metadata imports are reported.
- Runtime SessionDB/state imports are reported.
- Runtime provider/gateway imports are reported.
- The catch-all class for local/client-package candidates remains populated.

The tests intentionally check stable class IDs and representative anchors, not
exact full fixtures. Follow-up migration PRs should update this document and the
audit expectations when a dependency class is intentionally reduced or removed.
