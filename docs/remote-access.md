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

**Setup:**

1. Install [Tailscale](https://tailscale.com/download) on your server and
   your iPhone/Android.
2. Start the WebUI listening on all interfaces with password auth enabled:

```bash
HERMES_WEBUI_HOST=0.0.0.0 HERMES_WEBUI_PASSWORD=your-secret ./start.sh
```

3. Open `http://<server-tailscale-ip>:8787` in your phone's browser
   (find your server's Tailscale IP in the Tailscale app or with
   `tailscale ip -4` on the server).

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
