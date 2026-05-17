# Hermes WebUI — Docker setup guide

This is the comprehensive Docker reference. For a 5-minute quickstart, see the [README Docker section](../README.md#docker).

## TL;DR — pick one

| Setup | When to use | File |
|---|---|---|
| **Single-container** (recommended) | You just want chat working. WebUI runs the agent in-process. | `docker-compose.yml` |
| **Two-container** | You want isolation between gateway (CLI/Telegram/cron) and chat UI. | `docker-compose.two-container.yml` |
| **Three-container** | Two-container PLUS the dashboard for monitoring. | `docker-compose.three-container.yml` |
| **All-in-one image** (community fork — third-party, not maintained by us) | Podman 3.4 / multi-arch / supervisord-style preference. | [sunnysktsang/hermes-suite](https://github.com/sunnysktsang/hermes-suite) — see [#1399](https://github.com/nesquena/hermes-webui/issues/1399) for the original discussion |

If something stops working, **start with the single-container setup** — it's the simplest path and fixes most permission/UID/path-mismatch issues by construction.

## Production image security model

The production Docker image is hardened for the normal single-tenant container threat model:
Hermes WebUI assumes one operator controls the container, mounted Hermes home, and workspace.
The image does **not** install `sudo`, does not add runtime users to a sudo group, and does not
grant `NOPASSWD` escalation. If an agent/tool process gains a shell as `hermeswebui`, it should
not be able to become root with a passwordless sudo command.

The entrypoint still starts as `root` for a narrow init phase because Docker bind mounts often need
UID/GID alignment and ownership preparation before the app can read `~/.hermes`, `/workspace`,
`/app`, and `/uv_cache`. After that setup, `docker_init.bash` re-execs itself as the unprivileged
`hermeswebui` user and starts the server there. Init scratch files under `/tmp/hermeswebui_init`
are owner-only (`0700` directory, `0600` files), not world-writable.

For multi-tenant or hostile-container environments, rebuild with your own runtime user, mount policy,
and supervisor assumptions. Development images that need package-manager convenience should add
those tools in a dev-only Dockerfile instead of reintroducing passwordless sudo to production.

## 5-minute quickstart (single container)

```bash
git clone https://github.com/nesquena/hermes-webui
cd hermes-webui
cp .env.docker.example .env
# Edit .env if needed (most users can skip this on Linux)
docker compose up -d
open http://localhost:8787
```

That's it. Your existing `~/.hermes` directory is mounted, your `~/workspace` is browsable, and the WebUI auto-detects your UID/GID from the mounted volume.

## What goes wrong (and how to fix it)

### 1. "Permission denied" at startup

**Symptom**: Container starts but immediately crashes, logs show:
```
PermissionError: [Errno 13] Permission denied: '/home/hermeswebui/.hermes/...'
```

**Cause**: The container's user (UID 1000 by default) can't read your bind-mounted directory because your host files are owned by a different UID.

**Fix**: Set `UID` and `GID` in `.env` to match your host:
```bash
echo "UID=$(id -u)" >> .env
echo "GID=$(id -g)" >> .env
docker compose down && docker compose up -d
```

On macOS, host UIDs start at 501. On Linux, the first interactive user is usually UID 1000.

> **macOS Docker Desktop**: if UID mapping still misbehaves after the env fix, try toggling **Settings → General → File sharing implementation** between VirtioFS and gRPC-FUSE. Different implementations preserve UIDs across the host/container boundary differently.

### 2. ".env file mode 0640 → permission denied" (#1389)

**Symptom**: You set `HERMES_HOME_MODE=0640` (or some other group-readable mode) on your host `.env` file, container starts, then errors out:
```
[security] fixed permissions on .env (0o640 -> 0600)
failed to load .env: open .env: permission denied
```

**Cause**: WebUI's `fix_credential_permissions()` startup hook enforces 0600 by default. This is the right thing for a clean install but conflicts with operator-set modes.

**Fix**: Set one of these env vars in your `.env`:
- `HERMES_SKIP_CHMOD=1` — bypass the fixer entirely
- `HERMES_HOME_MODE=0640` — allow group bits, only strip world-readable

Both are documented in `api/startup.py::fix_credential_permissions()`.

> ⚠️ **Multi-container warning**: `HERMES_HOME_MODE` has DIFFERENT semantics in the agent image vs. the WebUI:
> - **WebUI**: credential FILE mode threshold (`0640` allows group bits on `.env`)
> - **Agent**: `HERMES_HOME` *directory* mode (default `0700`)
>
> `0640` on a directory has no owner-execute bit, so the agent can't traverse its own home → bricked. For multi-container setups, use `HERMES_HOME_MODE=0750` (group-traversable) or `0701` (x-only). The compose files have per-service comments that match each side's semantics.

### 3. "Workspace appears empty even though my files are there"

**Symptom**: WebUI loads but `/workspace` shows no files.

**Cause**: Same as #1 — UID mismatch on the bind mount.

**Fix**: Same as #1 — match host UID/GID via `.env`.

### 4. "Two-container setup: WebUI can't find agent source" (#858)

**Symptom**: WebUI logs at startup:
```
!! WARNING: hermes-agent source not found.
!!   Looked in: /home/hermeswebui/.hermes/hermes-agent
!!              /opt/hermes
```

**Cause**: The agent's source (`/opt/hermes` inside the agent container) needs to be exposed to the WebUI container via a shared volume. The two-container compose file does this via `hermes-agent-src` named volume, but if you're using bind mounts incorrectly the path won't resolve.

**Fix**: Use the named volumes that ship with `docker-compose.two-container.yml` — don't replace them with bind mounts unless you know what you're doing. The agent container writes its source to `/opt/hermes`, and the WebUI mounts that volume at `/home/hermeswebui/.hermes/hermes-agent`.

If you must use a bind mount: pick a host path, then mount it to `/opt/hermes` in the agent container AND `/home/hermeswebui/.hermes/hermes-agent` in the WebUI container.

### 5. "Tools (git, node, etc.) missing in two-container setup" (#681)

**Symptom**: You ask the agent to run `git status` in chat and it errors with `command not found`.

**Cause**: This is **architectural, not a bug**. In the two-container setup, agent processes started by the WebUI run **inside the WebUI container**, not the agent container. The WebUI image doesn't include git/node by design (it's a UI image, not a tool host).

**Workarounds**:
- **Single-container setup** (`docker-compose.yml`) — everything in one container, no boundary
- **Custom WebUI image** — extend the `Dockerfile` to install the tools you need
- **Combined image** ([sunnysktsang/hermes-suite](https://github.com/sunnysktsang/hermes-suite)) — community fork that ships agent+webui+dashboard in one container

### 6. "config.yaml not loaded"

**Symptom**: You have a `config.yaml` in your host `~/.hermes/`, but the WebUI shows "no model configured" or doesn't pick up your custom providers.

**Cause**: Either the file isn't readable (UID/GID issue, see #1) or it's not in the expected path inside the container.

**Fix**:
- Verify: `docker exec hermes-webui ls -la /home/hermeswebui/.hermes/config.yaml`
- If it doesn't exist: your host bind mount is pointing at the wrong directory.
- If it exists but is unreadable: see #1 for the UID/GID fix.

### 7. "On Podman: can't share .hermes between containers"

**Symptom**: Two-container setup works on Docker but fails on Podman with permission errors no matter what UID/GID you set.

**Cause**: Podman 3.4 (Ubuntu 22.04 default) has limited support for `userns_mode: keep-id` across multiple containers — files written by one container appear with a different UID in the other.

**Fix**: Either upgrade to Podman 4+ (which fixes this), or use the [single-container setup](#5-minute-quickstart-single-container), or use the [community all-in-one image](https://github.com/sunnysktsang/hermes-suite).

## Multi-container architecture

The two- and three-container setups use **named Docker volumes** (not bind mounts) by default for a reason: named volumes solve the UID/GID problem by construction. Docker creates the volume's root directory with the correct ownership, all containers reading/writing to it see the same files, no host-side permission setup required.

```
                 ┌─────────────────────────────────┐
                 │      hermes-home (volume)       │
                 │  (config, sessions, state, ...)  │
                 └─────────────────────────────────┘
                          ↑              ↑
                          │ rw           │ rw
                          │              │
      ┌──────────────┐    │              │    ┌──────────────┐
      │ hermes-agent │────┘              └────│ hermes-webui │
      │  (port 8642) │                        │  (port 8787) │
      └──────────────┘                        └──────────────┘
              │                                       ↑
              │ rw                                    │ ro
              ↓                                       │
      ┌─────────────────────────┐                     │
      │ hermes-agent-src (vol)  │─────────────────────┘
      │ (agent's Python source) │
      └─────────────────────────┘
```

The WebUI container doesn't ship with the agent's Python deps — at startup it runs `uv pip install /home/hermeswebui/.hermes/hermes-agent` to install them from the shared volume. The WebUI mount is read-only; the agent container is the only writer.

## Upgrading the agent container

The `hermes-agent-src` named volume is initialised from the agent image's `/opt/hermes` on first `up`. Docker reuses the volume verbatim on every subsequent `up` — **even after `docker pull` of a newer agent image**. The cached volume content masks the new image's source tree, so a fresh `docker pull` of `nousresearch/hermes-agent:latest` does not by itself give you the new agent code, dependencies, or entrypoint.

This is the root cause of [#1416](https://github.com/nesquena/hermes-webui/issues/1416): the symptom looked like a missing entrypoint, but the entrypoint was actually present in the new image and hidden behind the stale named volume.

To upgrade the agent image cleanly, drop the source volume before recreating:

```bash
# Two-container setup
docker compose -f docker-compose.two-container.yml down
docker volume rm <project>_hermes-agent-src
docker compose -f docker-compose.two-container.yml pull
docker compose -f docker-compose.two-container.yml up -d

# Three-container setup
docker compose -f docker-compose.three-container.yml down
docker volume rm <project>_hermes-agent-src
docker compose -f docker-compose.three-container.yml pull
docker compose -f docker-compose.three-container.yml up -d
```

Replace `<project>` with your Compose project name (the parent directory by default; check with `docker volume ls`). The `hermes-home` volume (config, sessions, state) is left untouched — only `hermes-agent-src` (the agent's installed Python source) is recreated.

> The single-container setup (`docker-compose.yml`) does not use `hermes-agent-src` and is not affected by this upgrade pattern — pulling a newer WebUI image and `docker compose up -d --force-recreate` is sufficient.

## What the multi-container setup isolates (and what it doesn't)

The two- and three-container setups give you **process, network, and resource isolation** between the gateway and the chat UI:

- Each service has its own PID namespace and lifecycle — the agent process can crash without taking down the chat UI and vice versa.
- The gateway API (port 8642) is bound by the agent service only; the WebUI cannot bind it. Other containers reach the gateway via the `hermes-net` Docker network.
- Resource limits (`deploy.resources.limits` in `docker-compose.three-container.yml`) apply per service, so you can cap the agent independently of the dashboard.
- Restart policies, log streams, and container health checks are scoped per service.

What multi-container does **not** isolate:

- **Filesystem boundary.** Both services share `hermes-home` (config, sessions, state), and the WebUI mounts the agent's installed source from `hermes-agent-src`. The WebUI mount is read-only (since v0.51.84), but the agent service still has write access, and both services share the home volume.
- **UID/GID boundary.** Both services default to `${UID:-1000}` so files written by one are readable by the other. If you align them to different UIDs you'll get permission errors on the shared volume.
- **Trust boundary on the agent source.** The WebUI installs Python dependencies from the shared `hermes-agent-src` volume at startup. The read-only mount means a compromised WebUI cannot rewrite the agent source, but it does run code from that volume.

If you need **filesystem isolation** between the chat UI and the agent (e.g. you don't trust the WebUI to read agent state), the multi-container setup is not enough — run the agent on a separate host and connect the WebUI to it via the gateway HTTP API. If you don't need any boundary, the single-container setup is simpler.

## Bind-mount migration (advanced)

If you really need to bind-mount an existing host `~/.hermes` (e.g. you're keeping config in dotfiles, sharing with a non-Docker `hermes` install, etc.):

```yaml
volumes:
  hermes-home:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /home/youruser/.hermes
  hermes-agent-src:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /opt/hermes-agent-source
```

**Critical requirements**:

1. The host directory MUST be readable by your container UID. Run `id -u` on the host and ensure `~/.hermes` is owned by that UID (or readable via group bits).
2. ALL containers sharing the volume must run as the SAME UID/GID. Set `UID=$(id -u)` and `GID=$(id -g)` in `.env`.
3. If your host `.env` is mode 0640, set `HERMES_SKIP_CHMOD=1` or `HERMES_HOME_MODE=0640` so the startup hook doesn't try to enforce 0600.

## Reference

- [`docker-compose.yml`](../docker-compose.yml) — single container (recommended)
- [`docker-compose.two-container.yml`](../docker-compose.two-container.yml) — agent + webui
- [`docker-compose.three-container.yml`](../docker-compose.three-container.yml) — agent + dashboard + webui
- [`.env.docker.example`](../.env.docker.example) — environment variable template
- [`Dockerfile`](../Dockerfile) — single-container build
- [`docker_init.bash`](../docker_init.bash) — container entrypoint script

## Related issues

- #1416 — agent-image upgrade requires removing `hermes-agent-src` named volume (see [Upgrading the agent container](#upgrading-the-agent-container))
- #1389 — `HERMES_HOME_MODE` override (fixed in v0.50.254 — agent honors `HERMES_SKIP_CHMOD` and `HERMES_HOME_MODE`)
- #1399 — UID alignment in compose files (fixed in v0.50.260 via PR #1428 + this guide)
- #858 — two-container `/opt/hermes` path confusion
- #681 — tools running in WebUI container, not agent container (architectural)
- #668 — auto-detect UID/GID from mounted volume
- #569 — UID/GID detection priority order

If you hit a new failure mode not covered here, please [open an issue](https://github.com/nesquena/hermes-webui/issues/new) with:

1. Which compose file you used
2. The error from `docker logs hermes-webui`
3. `docker exec hermes-webui id` output
4. `docker exec hermes-webui ls -la /home/hermeswebui/.hermes` output
