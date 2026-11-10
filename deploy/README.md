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
# 0. Create the backend service account and give it a writable checkout.
#    hermes-webui.service runs as User=hermeswebui; without the account,
#    systemd fails before start.sh runs. If you instead run the unit as
#    your existing installation owner, change User=/Group= in the unit.
sudo useradd --system --home /opt/hermes-webui --shell /usr/sbin/nologin hermeswebui
sudo chown -R hermeswebui:hermeswebui /opt/hermes-webui

# 1. Install the systemd unit (edit HERMES_WEBUI_ALLOWED_ORIGINS to your
#    external origin https://<server_name>:8787 — see Security notes)
sudo cp hermes-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-webui

# 2. Install the PAM service
sudo cp webui-auth.pam /etc/pam.d/webui-auth

# 3. Install the nginx config (adjust TLS cert paths and server_name)
sudo cp nginx-webui.conf /etc/nginx/sites-available/webui
sudo ln -sf /etc/nginx/sites-available/webui /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 3b. REQUIRED for PAM logins: nginx workers must run as root.
#     pam_unix verifies passwords against /etc/shadow, which only root
#     can read — and its setgid helper unix_chkpwd can only verify the
#     *caller's own* password, so a www-data worker rejects every
#     correct password for other users (e.g. deployer). Set the `user`
#     directive in /etc/nginx/nginx.conf to `user root;` and restart
#     nginx. See Security notes for the tradeoff.
#     The pattern must allow a hyphen in the account name: `\w` does not
#     match `-`, so `s/^\s*user\s+\w+;/…/` silently no-ops on Debian's
#     default `user www-data;` — the exact case this step targets.
sudo sed -i -E 's/^[[:space:]]*user[[:space:]]+[A-Za-z0-9_-]+;/user root;/' /etc/nginx/nginx.conf
sudo grep -qE '^[[:space:]]*user[[:space:]]+root;' /etc/nginx/nginx.conf || echo "WARNING: nginx worker user not set to root — PAM logins will fail" >&2
sudo systemctl restart nginx

# 4. Create system users that PAM will authenticate.
#    useradd alone leaves the account locked — set a password too,
#    otherwise pam_unix cannot log the user in.
sudo useradd -m -s /bin/bash deployer
sudo passwd deployer

# 5. Verify (the PAM gate prompts for the password; --fail surfaces
#    HTTP errors instead of printing an error page as success)
curl --fail --user deployer -k https://<your-host>:8787/health
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
- PAM requires nginx workers to run as root (`user root;` in `nginx.conf`,
  Installation step 3b): `pam_unix` checks passwords against `/etc/shadow`,
  which only root can read, and its `unix_chkpwd` helper verifies only the
  caller's own password. That widens the blast radius of a worker compromise,
  so prefer running this stack on a dedicated host/VM rather than a shared
  box. Granting the worker read access to `/etc/shadow` instead is **not**
  an acceptable alternative.
- TLS termination happens at nginx. The backend receives plain HTTP from
  `127.0.0.1`.
- `proxy_set_header Host 127.0.0.1;` is the deliberate Host-header rewrite — it
  is not a workaround. It is the correct pattern for loopback-bound backends.
  Because the Host is rewritten, the backend's CSRF/origin check sees
  `127.0.0.1` while the browser sends `Origin: https://<server_name>:8787`;
  the unit therefore sets `HERMES_WEBUI_ALLOWED_ORIGINS` to that external
  origin. Without it, browser writes fail with 403. (`X-Forwarded-Host`
  carries `$host` without the `:8787` port, so
  `HERMES_WEBUI_TRUST_FORWARDED_HOST` alone is not sufficient.)
- For publicly exposed setups with built-in per-user auth, use the
  `HERMES_WEBUI_PASSWORD` / OIDC approach documented in
  `docs/remote-access.md` instead of this PAM template.
