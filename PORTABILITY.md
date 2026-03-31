# Portability Plan for Hermes Web UI MVP

This document describes what would need to change for this repository to be **download-and-run friendly** for the widest possible set of Hermes agent installations.

Assumption: the user already has a working Hermes agent somewhere, either:
- on their local machine, or
- on a VPS they can SSH into.

Goal: a new user should be able to clone this repo and have it discover their existing Hermes installation with as little manual setup as possible.

---

## What should be true in the portable version

A portable version should be able to:

1. Find the Hermes agent automatically, or via one small override.
2. Find the Python environment automatically, or use a user-supplied override.
3. Pick a sane state directory without hard-coded user paths.
4. Run locally or against a remote VPS with minimal configuration.
5. Avoid requiring repo-specific paths like `/home/hermes/...`.
6. Avoid assuming the repo lives inside a specific parent checkout.
7. Avoid assuming the user has the same workspace naming, session layout, or directory structure.
8. Keep tests isolated regardless of the user’s setup.
9. Document the bootstrap flow clearly enough that a first-time user can succeed.

---

## Current portability blockers

### 1. Hard-coded Hermes agent paths
Current code and scripts assume things like:
- `/home/hermes/.hermes/hermes-agent/venv/bin/python`
- `/home/hermes/.hermes/hermes-agent`
- `/home/hermes/webui-mvp/server.py`

These paths make the repo work for one specific machine layout, but not for other Hermes installs.

### 2. Hard-coded state locations
The repo currently expects state under something like:
- `~/.hermes/webui-mvp/`
- `~/.hermes/webui-mvp-test/`

That is reasonable as a default, but it should be configurable and auto-discoverable.

### 3. The start script is environment-specific
`start.sh` currently assumes:
- a specific Python venv path
- a specific agent checkout path
- a specific working directory

### 4. Tests assume a fixed install shape
The test fixture currently assumes:
- the Hermes agent repo exists at a known path
- the Python interpreter is in a known venv location
- the state directory lives under `~/.hermes`

### 5. Documentation reveals internal paths
The docs currently embed:
- absolute local paths
- a specific VPS IP example
- tunnel commands tailored to the current setup

That is fine for internal use, but a portable public version should replace those with discovery-based or example-only instructions.

---

## The portability changes that would be needed

### A. Add a real bootstrap/discovery layer

Create a small bootstrap routine whose job is to determine:
- where Hermes agent code lives,
- what Python executable should be used,
- what state directory should be used,
- whether the UI is running locally or remotely.

Suggested discovery order:

1. Explicit environment variables.
2. A nearby sibling Hermes checkout.
3. A parent directory that looks like the Hermes agent repo.
4. Common default paths.
5. A helpful error if nothing matches.

Suggested environment variables:
- `HERMES_WEBUI_AGENT_DIR`
- `HERMES_WEBUI_PYTHON`
- `HERMES_WEBUI_STATE_DIR`
- `HERMES_WEBUI_DEFAULT_WORKSPACE`
- `HERMES_WEBUI_HOST`
- `HERMES_WEBUI_PORT`
- `HERMES_CONFIG_PATH`
- `HERMES_HOME`

What this buys you:
- fewer instructions for the user,
- fewer assumptions in code,
- easier support for local and VPS installs.

---

### B. Make the start script generic

`start.sh` should:

1. Discover the repo root dynamically.
2. Discover the Python executable from one of:
   - `HERMES_WEBUI_PYTHON`
   - a local `.venv/bin/python`
   - the Hermes agent venv if found nearby
   - `python3` as a fallback if appropriate
3. Discover the Hermes agent directory from:
   - `HERMES_WEBUI_AGENT_DIR`
   - a sibling checkout
   - a parent checkout
4. Avoid hard-coded absolute paths.
5. Print the chosen paths before starting, so the user can see what was detected.
6. Fail with a clear message if Hermes cannot be found.

Optional nice-to-have:
- a `--dry-run` or `--print-config` mode that shows what would be used.

---

### C. Separate “repo location” from “Hermes agent location”

The repo should not care where it itself lives.
It should only care about:
- the repo root for its own code,
- the Hermes agent root for imports and runtime integration.

This means the code should stop assuming things like:
- this repo lives under `/home/hermes`,
- the agent repo is exactly one directory up or down from here.

Instead, resolve paths based on:
- runtime config,
- env vars,
- actual filesystem discovery.

---

### D. Add a dedicated config resolution module

Create one place in the code that resolves all runtime configuration.
That module should handle:
- host and port,
- state directory,
- agent directory,
- default workspace,
- model,
- config file loading,
- any future public/private toggles.

Why this matters:
- fewer scattered `Path.home()` and `os.getenv()` calls,
- easier support for Linux, macOS, containers, and VPS installs,
- one clear place to document defaults.

---

### E. Make Hermes agent import discovery robust

Right now the server injects a parent directory into `sys.path` so Hermes modules can be imported.
That should become more flexible.

Suggested improvements:
- try an explicit Hermes agent path first,
- support a user-provided `PYTHONPATH` or equivalent override,
- check for the required Hermes modules before startup,
- show a clear error if the expected modules are unavailable.

This is important because different users may have Hermes arranged as:
- a monorepo,
- separate sibling checkouts,
- a virtualenv-only install,
- a deployed VPS layout.

---

### F. Make state storage portable and isolated

The UI should continue keeping state outside the repo, but the location should be configurable.

State should include:
- sessions
- workspaces
- last workspace
- cron/job state
- skills/memory references, if relevant
- any UI-specific caches

Recommended behavior:
1. Default to a stable user-scoped directory.
2. Allow override via `HERMES_WEBUI_STATE_DIR` or `HERMES_HOME`.
3. Create missing directories automatically.
4. Never write state into the source tree unless explicitly requested.
5. Keep test state isolated from production state.

---

### G. Make workspace selection auto-detectable

The UI should not require the user to manually point at a workspace every time.

Good portable behavior:
- restore the last workspace if it still exists,
- fall back to a user-configurable default workspace,
- fall back to the agent’s standard workspace if one exists,
- if nothing is known, prompt once and remember the selection.

Possible discovery sources:
- `HERMES_WEBUI_DEFAULT_WORKSPACE`
- `HERMES_HOME`
- a workspace list file
- a sibling workspace directory if present

For VPS setups, the UI should work whether the workspace is:
- inside the Hermes agent repo,
- in a separate workspace directory,
- or mounted from another location.

---

### H. Support both local and remote access patterns

The portable version should work in either of these situations:

1. Local Hermes agent, browser on same machine.
2. Hermes running on a VPS, browser connected over SSH tunnel or reverse proxy.

To support both:
- keep binding configurable,
- keep the default bind address safe (`127.0.0.1`),
- document how to override it when the user intentionally wants remote binding,
- avoid assuming a specific SSH hostname or IP.

The docs should show examples like:
- local: `http://127.0.0.1:<port>`
- remote tunnel: `ssh -N -L <localport>:127.0.0.1:<remoteport> user@host`

but never hard-code one user’s VPS details.

---

### I. Make tests path-independent

The test suite should not rely on the current user’s machine layout.

Tests should:
- discover the repo root dynamically,
- discover the Hermes agent location dynamically,
- use temporary isolated state directories,
- avoid touching real sessions or real cron jobs,
- work on macOS, Linux, and VPS hosts where possible.

Recommended test changes:
1. Replace fixed `~/.hermes/hermes-agent` assumptions with environment-based discovery.
2. Replace fixed `~/webui-mvp` assumptions with repo-relative discovery.
3. Use temporary directories or test-specific state roots.
4. Make the test server startup verify the discovered Hermes runtime before proceeding.
5. Keep production state fully untouched.

---

### J. Add a first-run setup flow

A public, portable release should include a minimal first-run flow.

Best-case flow:
1. User clones the repo.
2. User runs `./start.sh` or `python server.py`.
3. The app auto-detects Hermes.
4. If detection succeeds, it starts.
5. If detection fails, it prints one short fix-it block.

If auto-detection fails, the app should ask for only the missing pieces, ideally one at a time.

Examples:
- “I found Python, but not the Hermes agent directory.”
- “I found the agent, but not a valid virtualenv.”
- “I found Hermes state, but no default workspace.”

The goal is to avoid making users edit config files unless absolutely necessary.

---

### K. Make the public README setup-oriented, not machine-oriented

The README should explain:
- what Hermes Web UI expects from an existing Hermes install,
- how auto-detection works,
- what the fallback env vars are,
- how to override detection only if needed,
- how to run locally or through a VPS tunnel.

It should not be written as if everyone has the exact same `/home/hermes` layout.

---

### L. Clean up docs and examples for public release

The following should be sanitized or made generic:
- local file paths,
- specific VPS IPs,
- workspace names that reveal internal structure,
- tunnel instructions that reference one machine,
- any file names that imply private content or historical internal testing.

Replace them with placeholders like:
- `<HERMES_AGENT_DIR>`
- `<STATE_DIR>`
- `<SERVER_HOST>`
- `<SERVER_PORT>`
- `<WORKSPACE_PATH>`

---

## Recommended portability architecture

The simplest portable architecture would be:

### 1. One detection module
A single module that resolves:
- repo root,
- agent root,
- python executable,
- state directory,
- default workspace,
- host and port.

### 2. One configuration contract
Environment variables plus a small optional config file.

### 3. One startup path
`start.sh` should call the same resolution logic as `server.py` so the CLI path and the runtime path match.

### 4. One test isolation story
Tests should use their own discovered state root and not care where Hermes was installed.

### 5. One public-facing bootstrap doc
The README should explain the portable flow in terms of discovery and overrides, not in terms of your machine.

---

## Suggested user experience after portability work

A user should be able to do something like:

```bash
git clone <repo>
cd <repo>
./start.sh
```

and, if their Hermes setup is already valid, it should just work.

If their setup differs, they should only need to provide one or two overrides, for example:

```bash
HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent \
HERMES_WEBUI_PYTHON=/path/to/python \
./start.sh
```

That is the target experience.

---

## Practical order of implementation

If we were actually making this portable, I would do it in this order:

1. Remove hard-coded paths from `start.sh`.
2. Add config discovery in `api/config.py` or a new module.
3. Make agent import resolution dynamic.
4. Make tests use repo-relative and env-driven discovery.
5. Replace public docs with generic instructions.
6. Add a clear startup error path for missing Hermes components.
7. Add a short “first run” section to README.
8. Optionally add a `--print-config` mode for troubleshooting.

---

## Summary

To make this repo truly portable, the main work is not feature work, it is **bootstrapping and discovery**.

The repo currently works best when:
- Hermes is already installed,
- the directory layout matches your machine,
- and the user is comfortable with some manual path knowledge.

To make it work for the widest variety of Hermes setups, we need to:
- remove hard-coded paths,
- centralize config discovery,
- make state locations configurable,
- make tests isolated and path-independent,
- and rewrite the onboarding docs around auto-detection.

That would turn it from “works in my Hermes environment” into “clone it and it mostly figures itself out.”
