# Reverse-Proxy Deployment (nginx + systemd + PAM)

This directory provides templates for running the Hermes WebUI behind a TLS
terminating reverse proxy with **proxy-level authentication** (PAM) instead of
the built-in password/OIDC auth gate.

## When to use this pattern

| Scenario | Approach |
|---|---|
| Publicly exposed dashboard, per-user login (password / OIDC) | Set `HERMES_WEBUI_PASSWORD` or configure OIDC — built-in auth gate |
| Internal/VPN-only dashboard, system-account auth only | nginx Host rewrite + PAM (this directory) — auth gate stays OFF |
| Local-only, no proxy | `./start.sh` default (loopback, no auth gate) |

When the WebUI binds to `127.0.0.1` (the default), nginx rewrites the Host
header to `127.0.0.1` before forwarding so the loopback-bound server accepts
the request. Authentication is handled entirely by nginx via PAM at the proxy
level — no `HERMES_WEBUI_PASSWORD` is needed.

## Templates

1. **`hermes-webui.service`** — systemd unit that runs `./start.sh` on
   `127.0.0.1:8788` (loopback only, backend port differs from external port).
2. **`nginx-webui.conf`** — nginx TLS + PAM + Host-rewrite reverse proxy on
   port 8787.
3. **`webui-auth.pam`** — PAM service config for `auth_pam` module.

## Installation

```bash
# 1. Install the systemd unit (edit paths for your repo location)
sudo cp hermes-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-webui

# 2. Install the PAM service
sudo cp webui-auth.pam /etc/pam.d/webui-auth

# 3. Install the nginx config (adjust TLS cert paths and server_name)
sudo cp nginx-webui.conf /etc/nginx/sites-available/webui
sudo ln -sf /etc/nginx/sites-available/webui /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 4. Create system users that PAM will authenticate
sudo useradd -m -s /bin/bash deployer

# 5. Verify
curl -k https://<your-host>:8787/api/status
```

## Architecture

```
Browser  -->  nginx (port 8787, TLS, PAM auth)  -->  hermes-webui.service (port 8788, loopback)
                |
                |-- PAM auth (system user accounts)
                |-- Host header rewritten to 127.0.0.1
                |-- X-Forwarded-Host / X-Forwarded-Proto preserved
                |-- X-Forwarded-For set to client IP
```

## Security notes

- The backend **must** stay on `127.0.0.1` (`HERMES_WEBUI_HOST=127.0.0.1`) — never
  bind to `0.0.0.0` unless you also set `HERMES_WEBUI_PASSWORD` for built-in auth.
- Do **not** set `HERMES_WEBUI_PASSWORD` — PAM auth at the proxy replaces it.
- TLS termination happens at nginx. The backend receives plain HTTP from
  `127.0.0.1`.
- `proxy_set_header Host 127.0.0.1;` is the deliberate Host-header rewrite — it
  is not a workaround. It is the correct pattern for loopback-bound backends.
- For publicly exposed setups with built-in per-user auth, use the
  `HERMES_WEBUI_PASSWORD` / OIDC approach documented in
  `docs/remote-access.md` instead of this PAM template.
