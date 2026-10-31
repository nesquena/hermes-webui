# Public exposure: putting the WebUI in front of other people

The WebUI is a single-operator tool: it starts on `127.0.0.1`, has no
password, and gives whoever reaches it full access to your filesystem and
your agent. That is the right default for a laptop.

This guide is for the other case — you want to run the WebUI for a team, for
your users, or for anyone else, and you need to know exactly which trust
boundary you are opening. Everything below is written against the code in
`api/` and `server.py`; the variables named here are read at request time
from the process environment, and the behaviour they enable is described
exactly as implemented.

**The first rule, before anything else: set a password, or turn on SSO,
*before* you widen the bind address.** The server does not refuse to start
without authentication — it prints a warning and binds anyway:

```
[!!] WARNING: Binding to 0.0.0.0 with NO PASSWORD SET.
     Anyone on the network can access your filesystem and agent.
```

See [`docs/remote-access.md`](remote-access.md) for reaching your own
instance over Tailscale or an SSH tunnel, and [`docs/docker.md`](docker.md)
for running the WebUI in containers.

---

## Choose an exposure scope first

The scope decides how much of the security burden you have to carry yourself.
There are three, and they are not equally safe.

| Scope | Who can reach it | Transport | What you must do |
|---|---|---|---|
| **Tailscale (private)** | Only your tailnet | WireGuard end-to-end | Set a password anyway; bind to the tailnet IP if you can |
| **LAN** | Anyone on the same network | Plain HTTP | Set a password; do not use it on public Wi-Fi |
| **Public internet** | Anyone on the internet | Plain HTTP unless you add TLS | Password **and** TLS, or a reverse proxy that does both for you |

Binding is controlled by `HERMES_WEBUI_HOST` (default `127.0.0.1`) and
`HERMES_WEBUI_PORT` (default `8787`).

Binding to `0.0.0.0` opens the port on *every* interface. If your intent is
"only my tailnet may reach this", prefer binding to the Tailscale interface
address over `0.0.0.0` plus a firewall rule — a firewall rule that is later
widened or disabled silently turns a private deployment into a public one.

**In every scope, set `HERMES_WEBUI_PASSWORD` (or configure SSO) before
changing `HERMES_WEBUI_HOST`.** The password is the only thing standing
between a reachable port and your filesystem.

---

## Option A — one shared password

The simplest public deployment. One password, one session, one default
profile; every user shares the same Hermes profile and the same workspace.

```bash
export HERMES_WEBUI_PASSWORD='a-long-passphrase'
export HERMES_WEBUI_HOST='127.0.0.1'   # if a reverse proxy fronts it
export HERMES_WEBUI_PORT='8787'
```

`HERMES_WEBUI_PASSWORD` is read from the environment and takes precedence
over the password stored in `settings.json`. If the environment variable is
set, the Settings screen refuses password changes outright (it answers
`409` and tells you to unset the variable and restart), rather than
silently saving a password that would never take effect.

**The rate limiter is per connection, not per person.** Failed logins are
limited to **5 attempts per 60 seconds**, and the key is the address the
WebUI sees for the connection. When a reverse proxy fronts the WebUI, that
address is the *proxy's*, so every user behind the proxy shares a single
budget: one person's repeated typos can lock out everyone else. Upstream
[#7850](https://github.com/nesquena/hermes-webui/pull/7850) tracks keying
this limit on the forwarded client IP instead. Until that lands, do not
treat the limiter as a per-user control in front of a shared proxy — treat
it as a speed bump against password guessing, and use a strong password.

## Option B — OIDC login (per-user identity)

If your identity provider can issue OIDC tokens, the WebUI can run its own
browser login and give each user their own session. Configure it entirely
through the environment:

```bash
export HERMES_WEBUI_OIDC_ISSUER='https://id.example.com/realms/main'
export HERMES_WEBUI_OIDC_CLIENT_ID='hermes-webui'
export HERMES_WEBUI_OIDC_CLIENT_SECRET='...'
export HERMES_WEBUI_OIDC_REDIRECT_URI='https://hermes.example.com/api/auth/oidc/callback'
export HERMES_WEBUI_OIDC_ALLOW_CLAIM='groups'
export HERMES_WEBUI_OIDC_ALLOW_VALUES='hermes-users'
```

All four of issuer, client id, allow-claim and allow-values must be present
or OIDC stays off and the WebUI falls back to password auth. `allow-claim`
names the claim to read from the token; `allow-values` lists the claim
values that are permitted to sign in. `HERMES_WEBUI_OIDC_SCOPES` and
`HERMES_WEBUI_OIDC_REDIRECT_URI` are optional. The same settings can be
written as a `webui_oidc` block in the config file, with the environment
variables taking precedence.

## Option C — SSO proxy asserts the user (per-group profiles)

If you already run an authenticating proxy (Authentik, oauth2-proxy, Cloudflare
Access, an internal gateway), let it authenticate the user and pass the
result in a request header. The WebUI trusts that header **only** from a
proxy it has been told to trust, and binds the session to a Hermes profile
based on the user's groups.

```bash
export HERMES_WEBUI_TRUSTED_AUTH_HEADER='X-Forwarded-User'
export HERMES_WEBUI_TRUSTED_GROUPS_HEADER='X-Forwarded-Groups'
export HERMES_WEBUI_GROUP_PROFILE_MAP='{"admins":"default","team-a":"projecta"}'
export HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL='https://sso.example.com/logout'
export HERMES_WEBUI_TRUSTED_PROXY_CIDRS='10.0.0.5/32'
```

How it behaves:

- **The header name must be a valid HTTP header name.** An invalid value is
  ignored with a warning, and — importantly — trusted-header auth then
  rejects *every* request. A typo here fails closed and locks everyone out.
- **Only a trusted proxy may assert the header.** The check is made against
  the raw socket address of the connection, so it cannot be spoofed by a
  header. Loopback is trusted implicitly (the common same-host reverse
  proxy). Any other proxy must be listed in `HERMES_WEBUI_TRUSTED_PROXY_CIDRS`.
  If the peer is not trusted, the request is treated as unauthenticated.
- **Your proxy must overwrite the header, not forward it.** The WebUI trusts
  the header value it receives from the trusted peer. If your proxy passes a
  client-supplied `X-Forwarded-User` through without replacing it, a client
  can simply set that header and choose their own identity. Configure the
  proxy to *set* the identity header itself.
- **Group-to-profile binding.** `HERMES_WEBUI_GROUP_PROFILE_MAP` is a JSON
  object mapping a group name to a Hermes profile. The first entry in the
  map whose group the user belongs to wins. A user who matches no group
  falls back to the `default` profile. If the map is unset entirely, no
  profile is bound at all.
- **Group list format.** The groups header is split on commas and newlines.
  Some proxies emit a pipe-separated list instead; set
  `HERMES_WEBUI_TRUSTED_GROUPS_PIPE_SEPARATOR=1` to split on `|` as well.
  This is opt-in because a group name may legitimately contain `|`.
- **Log out where your users log out.** `HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL`
  points the sign-out action at your identity provider.

---

## Behind a reverse proxy

A reverse proxy in front of the WebUI lets you terminate TLS, and — if it
runs on the same host — keeps the WebUI itself bound to loopback.

**Caddy** (TLS automatically):

```caddyfile
hermes.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

**nginx** (TLS terminated by nginx):

```nginx
server {
    listen 443 ssl;
    server_name hermes.example.com;

    ssl_certificate     /etc/letsencrypt/live/hermes.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hermes.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE: do not buffer the event stream.
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

Then tell the WebUI what to believe:

```bash
export HERMES_WEBUI_ALLOWED_ORIGINS='https://hermes.example.com'
export HERMES_WEBUI_TRUST_FORWARDED_HOST=1
export HERMES_WEBUI_TRUST_FORWARDED_PROTO=1
export HERMES_WEBUI_SECURE=1
```

### `HERMES_WEBUI_ALLOWED_ORIGINS` — the origin allowlist

The WebUI rejects browser requests whose `Origin` does not match the host
it believes it is serving. Behind a proxy that rewrites `Host` to the
upstream address, the browser's origin and the `Host` header no longer
match, and the request is refused as a cross-site request.

`HERMES_WEBUI_ALLOWED_ORIGINS` is the escape hatch: a comma-separated list
of origins that are allowed explicitly, each including its scheme, for
example `https://hermes.example.com,https://hermes.example.com:8443`.
Entries without a scheme are skipped with a warning — the scheme is part of
the origin, so `hermes.example.com` is not a valid entry.

### `HERMES_WEBUI_TRUST_FORWARDED_HOST` — trusting the proxy's host header

With this set, the origin check also accepts `X-Forwarded-Host` and
`X-Real-Host` as candidate hosts. Use it when your proxy sets those
headers; do not set it if it does not. A port in the allowed host is
compared port-aware, and an absent port is treated as the scheme default
(80 for `http`, 443 for `https`).

### `HERMES_WEBUI_TRUST_FORWARDED_FOR` — trusting the forwarded client IP

With this set, the WebUI consults the forwarded chain for the client IP
instead of using the socket address alone. It walks `X-Forwarded-For`
right-to-left, skipping hops that are themselves trusted-proxy addresses,
to find the first hop that is a real client.

This only takes effect when the immediate peer is a trusted proxy (see
`HERMES_WEBUI_TRUSTED_PROXY_CIDRS` below) — the check is made against the
raw socket address, so the chain cannot be spoofed by a header. A client
that sends `X-Forwarded-For: 127.0.0.1` from a public address is still
classified as public.

The forwarded IP decides whether a request counts as *local* — that is, one
that may reach the first-run onboarding, the first-password and passkey
setup, and the passwordless embedded terminal. Those gates stay closed for
a proxied public client unless you opt in here, which is the point: a
public deployment should not hand out onboarding or a passwordless
terminal. It does not change how the login rate limiter is keyed — that
is the raw peer address, as described under Option A.

### `HERMES_WEBUI_TRUSTED_PROXY_CIDRS` — which peers may assert a client IP

A comma-separated list of CIDRs or bare IP addresses. **Loopback is always
trusted implicitly**, which is why a same-host reverse proxy needs no
entry here. For a proxy on another machine, list its exact address:

```bash
export HERMES_WEBUI_TRUSTED_PROXY_CIDRS='10.0.0.5/32,192.168.1.10'
```

Use the **exact** address of the proxy. A malformed entry is skipped rather
than trusted, so a typo narrows trust instead of widening it — but a typo
also silently means "not trusted", which looks like the proxy being ignored.
There is no wildcard form: do not try to list a whole range.

### `HERMES_WEBUI_TRUST_FORWARDED_PROTO` and `HERMES_WEBUI_SECURE` — secure cookies

The session cookie only carries the `Secure` flag when the connection is
already known to be secure. In order of precedence, the WebUI decides this
by: the `HERMES_WEBUI_SECURE` environment variable (`1`/`true`/`yes` for
yes, `0`/`false`/`no` for no), then a direct TLS socket, then
`X-Forwarded-Proto: https` when `HERMES_WEBUI_TRUST_FORWARDED_PROTO` is
set. Otherwise it assumes plain HTTP.

`X-Forwarded-Proto` is only meaningful behind a proxy, so it is ignored
unless you opt in explicitly — otherwise a client could claim HTTPS over a
plain connection. Set `HERMES_WEBUI_SECURE=1` when your proxy terminates
TLS but does not send `X-Forwarded-Proto`, and remember that `Secure`
cookies will not be sent back over plain HTTP.

### Terminating TLS in the WebUI itself

If you have no reverse proxy, the WebUI can serve TLS directly. Both
variables are required; setting only one leaves TLS off:

```bash
export HERMES_WEBUI_TLS_CERT='/path/to/fullchain.pem'
export HERMES_WEBUI_TLS_KEY='/path/to/privkey.pem'
```

### Streaming behind a proxy

Long-lived event streams send `X-Accel-Buffering: no` so nginx-style
proxies pass events through unbuffered, and emit keepalive comments every
few seconds so idle proxy timeouts do not cut the connection. If your proxy
buffers until the response closes, set `HERMES_WEBUI_SSE_CHUNKED=1` to
frame each event as an HTTP/1.1 chunk. See
[`docs/sse-streams.md`](sse-streams.md) for the full stream reference.

---

## Checklist before you open it to the internet

- [ ] `HERMES_WEBUI_PASSWORD` is set, or OIDC / trusted-header auth is
      configured and verified.
- [ ] `HERMES_WEBUI_HOST` is the narrowest value that works — loopback
      behind a same-host proxy, or the tailnet interface address, not
      `0.0.0.0` unless something genuinely requires it.
- [ ] Traffic is HTTPS, either terminated in the WebUI
      (`HERMES_WEBUI_TLS_CERT` + `HERMES_WEBUI_TLS_KEY`) or by a reverse
      proxy.
- [ ] `HERMES_WEBUI_ALLOWED_ORIGINS` lists your public origin, with the
      scheme, if a proxy rewrites `Host`.
- [ ] `HERMES_WEBUI_TRUSTED_PROXY_CIDRS` lists the exact address of any
      proxy that is not on loopback — or is empty, if the proxy is local.
- [ ] If using trusted-header auth, the proxy **overwrites** the identity
      header rather than forwarding a client-supplied one.
- [ ] You know that behind a shared proxy the login rate limit is shared,
      not per user.
- [ ] Your own session works end to end: login, a chat turn, and a stream.

## Related

- [`docs/remote-access.md`](remote-access.md) — reaching your own instance
  (SSH tunnel, Tailscale, phone).
- [`docs/docker.md`](docker.md) — container deployment and bind mounts.
- [`docs/sse-streams.md`](sse-streams.md) — event-stream reference and proxy
  behaviour.
- [`docs/troubleshooting.md`](troubleshooting.md) — diagnostic flows.
