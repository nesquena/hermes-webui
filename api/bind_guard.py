"""Startup guard: refuse to expose a passwordless WebUI on a public address.

Salvaged from PR #3758 (GAP 2 — "public bind requires auth"). This lives in its
own module so server.py stays within its architectural line budget while the
user-facing refusal message can be as long and clear as it needs to be.

The decision (whether to refuse) and the ``sys.exit(1)`` still happen in
``server.main()``; this module only provides the predicate and the message text.
"""
from __future__ import annotations

import os

#: Env var that explicitly forces (``1``/on) or waives (``0``/off) the guard.
REQUIRE_AUTH_ENV = "HERMES_WEBUI_REQUIRE_AUTH_FOR_PUBLIC_BIND"

#: Loopback hosts are local-only and never trip the guard.
_LOOPBACK_HOSTS = ('127.0.0.1', '::1', 'localhost')


def _public_bind_requires_auth(host: str, *, within_container: bool, auth_enabled: bool) -> bool:
    """Whether startup should refuse a public (non-loopback) bind with no auth.

    Fails CLOSED for the dangerous case — a passwordless server bound to a
    public/network address — instead of merely warning, so a WebUI can't be
    unknowingly exposed to the network. Rules, in order:

    - If authentication is configured (password or passkey), never block.
    - Loopback binds (127.0.0.1 / ::1 / localhost) are local-only, never block.
    - ``HERMES_WEBUI_`` + ``REQUIRE_AUTH_FOR_PUBLIC_BIND`` is an explicit override:
        off (0/false/no/off) -> never block (operator secured access elsewhere)
        on  (1/true/yes/on)  -> always block a passwordless public bind
    - Otherwise default to ``within_container``: containers commonly publish
      0.0.0.0 beyond the local machine, so they fail closed by default, while
      bare-metal/dev hosts keep the historical warn-only behavior.
    """
    if auth_enabled:
        return False
    if host in _LOOPBACK_HOSTS:
        return False
    flag = os.getenv(REQUIRE_AUTH_ENV, "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return bool(within_container)


def public_bind_refusal_message(host: str) -> str:
    """Return the multi-line fatal message printed right before ``sys.exit(1)``.

    Crystal-clear by design: it states what was about to happen and why it is
    dangerous, then lays out the three concrete fix paths (set a password, bind
    to localhost, or explicitly opt out because another layer enforces access).
    """
    bar = "=" * 74
    lines = [
        "",
        bar,
        "  REFUSING TO START: passwordless server would be exposed on a public",
        "  address.",
        bar,
        "",
        "  WHAT HAPPENED",
        "    Hermes WebUI was about to bind to host %r with NO password" % (host,),
        "    and NO passkey configured. That address is public / reachable from",
        "    the network (it is not localhost), so anyone who can reach this",
        "    host could open the WebUI without logging in — reading your",
        "    sessions, files, and memory, and running commands as you.",
        "    Startup was stopped so this cannot happen by accident.",
        "",
        "  HOW TO FIX IT  (choose ONE, then restart)",
        "",
        "    1) Set a password  [RECOMMENDED]",
        "         export HERMES_WEBUI_PASSWORD=your-s...rd",
        "       (or configure a password in Settings)",
        "",
        "    2) Bind to localhost only  [if you only need local access,",
        "       e.g. reaching it over an SSH tunnel]",
        "         export HERMES_WEBUI_HOST=127.0.0.1",
        "",
        "    3) Already protected by another layer?  [a reverse proxy that",
        "       enforces auth, a private network, or a VPN] — explicitly",
        "       opt out to acknowledge you have secured access another way:",
        "         export HERMES_WEBUI_REQUIRE_AUTH_FOR_PUBLIC_BIND=0",
        "",
        bar,
        "",
    ]
    return "\n".join(lines)
