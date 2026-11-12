# Remote access

How to reach a self-hosted Hermes WebUI from another machine or your phone.

## Accessing from a remote machine

The server binds to `127.0.0.1` by default (loopback only). If you are running
Hermes on a VPS or remote server, use an SSH tunnel from your local machine:

```bash
ssh -N -L <local-port>:127.0.0.1:<remote-port> <user>@<server-host>
```

Example:

```bash
ssh -N -L 8787:127.0.0.1:8787 user@your.server.com
```

Then open `http://localhost:8787` in your local browser.

`start.sh` will print this command for you automatically when it detects you
are running over SSH.

---

## Accessing on your phone with Tailscale

[Tailscale](https://tailscale.com) is a zero-config mesh VPN built on
WireGuard. Install it on your server and your phone, and they join the same
private network -- no port forwarding, no SSH tunnels, no public exposure.

The Hermes Web UI is fully responsive with a mobile-optimized layout
(hamburger sidebar, sidebar top tabs in the drawer, touch-friendly controls),
so it works well as a daily-driver agent interface from your phone.

**Preferred setup: Tailscale Serve**

1. Install [Tailscale](https://tailscale.com/download) on your server and
   your iPhone/Android.
2. Keep the WebUI bound to localhost and enable password auth:

```bash
HERMES_WEBUI_PASSWORD=your-secret ./start.sh
```

3. Publish the local WebUI port through Tailscale Serve:

```bash
tailscale serve --bg 8787
```

4. Open the HTTPS MagicDNS URL that Tailscale prints in your phone's browser.

Tailscale Serve keeps WebUI on loopback while giving your tailnet an HTTPS
MagicDNS hostname. On Linux, changing Serve configuration may require elevated
permissions. If `tailscale serve --bg 8787` reports `Access denied: serve
config denied`, either run it with sudo:

```bash
sudo -S -p '' tailscale serve --bg 8787
```

Or allow the supervised non-root WebUI/Hermes user to manage Tailscale:

```bash
sudo -S -p '' tailscale set --operator=$USER
tailscale serve --bg 8787
```

**Fallback: direct tailnet IP**

Use direct tailnet access when Tailscale Serve is unavailable, disabled, or not
permitted. Because this binds WebUI beyond loopback, always enable password
auth:

```bash
HERMES_WEBUI_HOST=0.0.0.0 HERMES_WEBUI_PASSWORD=your-secret ./start.sh
```

Then open `http://<server-tailscale-ip>:8787` in your phone's browser (find
your server's Tailscale IP in the Tailscale app or with `tailscale ip -4` on
the server).

That's it. Traffic is encrypted end-to-end by WireGuard, and password auth
protects the UI at the application level. You can add it to your home screen
for an app-like experience.

### Community field report: ARM64 Android via AVF

A community report in [#2364](https://github.com/nesquena/hermes-webui/issues/2364)
documents Hermes Agent + WebUI running on a mid-range ARM64 Android phone inside
a Debian 12 VM via Android Virtualization Framework (AVF). The reported setup
used a Xiaomi Redmi Note 13 Pro 4G, 3.8 GiB RAM allocated to the VM, 8 visible
CPU cores, Chrome on Android at `localhost:8787`, and cloud-hosted inference.

This is not an official support baseline or provider/model benchmark, but it is
a useful compatibility signal for mobile ARM64 experiments: the WebUI rendered
smoothly in Chrome, ARM64 Debian worked for the agent stack, and the total local
footprint was about 1.7 GB. Practical caveats from the report: first install can
take longer when dependencies compile from source, Android browser tabs may
reload when switching apps, and disabling battery optimization for the terminal
or VM host may be needed for longer-running sessions.

> **Tip:** If using Docker, set `HERMES_WEBUI_HOST=0.0.0.0` in your
> `docker-compose.yml` environment (already the default) and set
> `HERMES_WEBUI_PASSWORD`.

---

## Running behind a reverse proxy

A reverse proxy on another host or container (a Docker bridge, a separate
nginx box, a cloud load balancer) must be allowlisted before WebUI will honor
any forwarded client-IP header from it. Set `HERMES_WEBUI_TRUSTED_PROXY_CIDRS`
with the **exact** address(es) of your proxy, comma-separated as CIDRs or bare
IPs:

```bash
HERMES_WEBUI_TRUSTED_PROXY_CIDRS=172.17.0.1,10.9.9.0/24 ./start.sh
```

Loopback (`127.0.0.0/8`, `::1`) is always in the trusted-proxy set, so a proxy
on the same host needs no allowlist entry for the request log or the
local-origin features. The local-origin features (onboarding, first password or
passkey setup, the passwordless terminal) still read the forwarded client
address only when `HERMES_WEBUI_TRUST_FORWARDED_FOR=1` is set. Without it,
every request through a same-host proxy arrives from a loopback address and is
treated as local, whoever the client is. Set `HERMES_WEBUI_TRUST_FORWARDED_FOR=1`
whenever a same-host proxy serves clients on other machines.

Login rate limiting is the exception. By default it keys on the socket peer,
so every user behind a proxy shares the proxy's single login bucket, and
repeated failed logins from one client can lock out the others. Per-client
buckets are an explicit opt-in, because they are only safe when the proxy
writes `X-Forwarded-For` (see below). They need all three of:

- `HERMES_WEBUI_LOGIN_RATE_LIMIT_PER_CLIENT=1`;
- `HERMES_WEBUI_TRUST_FORWARDED_FOR=1`;
- the proxy's address listed explicitly in `HERMES_WEBUI_TRUSTED_PROXY_CIDRS`
  (implicit loopback trust does not count).

```bash
HERMES_WEBUI_LOGIN_RATE_LIMIT_PER_CLIENT=1 HERMES_WEBUI_TRUST_FORWARDED_FOR=1 \
  HERMES_WEBUI_TRUSTED_PROXY_CIDRS=127.0.0.1 ./start.sh
```

Experimental builds from v0.52.546 to this change gave per-client buckets with
only the last two settings. If you relied on that, add the first one.

With only loopback listed, WebUI prints a startup warning that
`HERMES_WEBUI_TRUSTED_PROXY_CIDRS` lists no proxy outside this host. For this
setup that is expected, as the warning itself says. If WebUI listens on IPv6
(`HERMES_WEBUI_HOST` contains a colon), the proxy may connect from `::1`; list
`127.0.0.1,::1` instead.

Listing a loopback address trusts every local client (an SSH tunnel, another
process on the host) exactly like the proxy, because the address cannot tell
them apart. Only do it when the proxy is the sole local client that reaches
WebUI. With the opt-in, per-client buckets need `X-Forwarded-For`. A request carrying only
`X-Real-IP` keys on the proxy's shared bucket, because nginx relays a
client-supplied `X-Real-IP` unless the config overwrites it, and that would let
a client pick a fresh bucket on every attempt. The same holds for
`X-Forwarded-For` itself: list a proxy only if it appends to the chain (nginx's
`$proxy_add_x_forwarded_for`) or overwrites it. A bare `proxy_pass` relays the
client's own `X-Forwarded-For` unchanged, so a listed proxy that does not write
the header lets every client choose its login bucket. Appending is fine, because
WebUI walks the chain from the right. Have the proxy overwrite
`X-Real-IP` (`proxy_set_header X-Real-IP $remote_addr;`). If it relays a
client-supplied value that disagrees with the chain, or one that is not an
address, WebUI falls back to the shared bucket.

A proxy on another host without a matching allowlist entry is treated as an
ordinary client: forwarded headers are ignored, so the structured request log
records no `forwarded_for` field for requests arriving through it and
local-origin features (onboarding, passwordless terminal) do not treat it as
local. That is deliberate — an unallowlisted peer must never be able to assert
a client identity WebUI then trusts.

The same allowlist governs the trusted identity-header SSO setup
(`HERMES_WEBUI_TRUSTED_AUTH_HEADER`) described in `.env.example`.

> **Note:** nginx relays client-supplied request headers through by default
> (`proxy_pass_request_headers on`), which includes `X-Real-IP`. The request
> log therefore never trusts `X-Real-IP` for its `forwarded_for` field (only
> the right-to-left `X-Forwarded-For` walk is accepted, and only from an
> allowlisted peer). The local-origin gate still honors `X-Real-IP` when the
> peer is trusted and `HERMES_WEBUI_TRUST_FORWARDED_FOR=1` is set, as it did
> before; configure your proxy to overwrite it
> (`proxy_set_header X-Real-IP $remote_addr;`) to keep that path honest, or just
> for your own logging. Trusted-header SSO reads neither header: it only checks
> that the socket peer is an allowlisted proxy.

### Client IP in the request log (fail2ban)

Each structured request-log record always carries `remote`, the socket peer,
and at most one forwarded client-IP field:

- `forwarded_for`: the client address, present only when the socket peer is a
  trusted proxy and the `X-Forwarded-For` chain resolves to a valid address
  other than the peer itself. It is always a normalized address, never header
  text. Key fail2ban jails on this field when WebUI runs behind a proxy.
- `forwarded_for_ignored: true`: a forwarded header was present but WebUI did
  not record it, because the peer is not a trusted proxy or because the chain
  from a trusted proxy is empty or malformed, is an IPv6 address with a zone
  id, or names no hop beyond the trusted proxies themselves.
  The first such request from an untrusted peer also logs a one-time warning
  naming the peer and the setting to change.

Attribution relies on the proxy appending the address it received the request
from to `X-Forwarded-For` (nginx: `proxy_set_header X-Forwarded-For
$proxy_add_x_forwarded_for;`). A proxy that passes the client's own header
through unchanged lets the client choose the right-most hop, and therefore the
logged address.

---
