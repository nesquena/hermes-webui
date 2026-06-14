"""Host-header validation to defend the WebUI against DNS-rebinding attacks.

The WebUI binds to 127.0.0.1 by default and ships with password auth off so
the localhost first-run experience stays frictionless. The README documents
that posture and instructs operators to set ``HERMES_WEBUI_PASSWORD`` before
exposing the port outside 127.0.0.1.

That guidance defends against external network attackers, but it does not
defend against DNS rebinding through the operator's own browser. An attacker
who can have the operator load ``http://victim.attacker.com:8787`` (a malicious
hostname whose DNS first resolves to the attacker's IP and then rebinds to
``127.0.0.1`` after the page is loaded) ends up with same-origin JavaScript
issuing fetches against the local WebUI. The browser's same-origin policy
keys on hostname, not on the resolved IP, so the localhost bind alone is not
a trust boundary — and the same-origin / CSRF-token check in
``api.routes._check_csrf`` does not catch this because the attacker's Origin
and Host both reference the same rebound hostname.

This module closes that gap by validating the Host header against a curated
allowlist before any auth, CSRF, or route logic runs. The allowlist permits
loopback hostnames, private LAN ranges, Tailscale CGN (the documented remote
deploy), and IPv6 link-local / unique-local — every shape the README ships
out of the box. Operators who terminate the WebUI behind a public hostname
opt in via the ``HERMES_WEBUI_ALLOWED_HOSTS`` env var.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from typing import Iterable

logger = logging.getLogger(__name__)


# Tailscale's CGNAT range (RFC 6598) — Tailscale assigns 100.x.y.z addresses
# to every node, and the README's documented remote-access path uses one of
# these as the Host. ``IPv4Address.is_private`` does NOT include this range,
# so we check it explicitly.
_TAILSCALE_CGN = ipaddress.ip_network("100.64.0.0/10")


def _strip_port(host_header: str) -> str | None:
    """Return the hostname portion of an HTTP Host header, lowercased.

    Returns ``None`` for malformed input. Accepts:
      - ``"127.0.0.1"`` / ``"127.0.0.1:8787"``
      - ``"localhost:8787"`` / ``"example.com"``
      - ``"[::1]"`` / ``"[::1]:8787"`` (RFC 7230 bracketed IPv6)
      - ``"::1"`` (unbracketed IPv6 — non-standard but tolerated for safety)
    """
    if not isinstance(host_header, str):
        return None
    raw = host_header.strip().lower()
    if not raw:
        return None

    # Bracketed IPv6: [addr] or [addr]:port
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            return None
        name = raw[1:end]
        rest = raw[end + 1 :]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                return None
        return name or None

    # Hostname or IPv4 with optional :port (single colon)
    if raw.count(":") <= 1:
        if ":" in raw:
            name, port = raw.split(":", 1)
            if not port.isdigit():
                return None
            return name or None
        return raw

    # Multiple colons without brackets — likely raw IPv6. Tolerate it if it
    # parses as IPv6; otherwise reject as malformed.
    try:
        ipaddress.IPv6Address(raw)
        return raw
    except ValueError:
        return None


def _is_loopback_hostname(name: str) -> bool:
    """Hostnames that always resolve to a loopback address."""
    if not name:
        return False
    if name == "localhost":
        return True
    if name.endswith(".localhost"):
        return True
    # IETF-reserved loopback aliases that some Docker / systemd setups use.
    if name in {"ip6-localhost", "ip6-loopback"}:
        return True
    return False


def _is_loopback_or_lan_ip(name: str) -> bool:
    """True if ``name`` is a loopback / LAN / link-local / ULA / Tailscale IP."""
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_private:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _TAILSCALE_CGN:
        return True
    return False


def parse_allowed_hosts(env_value: str | None) -> set[str]:
    """Parse a comma- or whitespace-separated allowed-hosts list.

    Each entry is lowercased and stripped of any optional scheme / trailing
    slash / port — Host validation is on hostname only. Empty input returns
    an empty set.
    """
    if not env_value:
        return set()
    out: set[str] = set()
    for raw in re.split(r"[,\s]+", env_value):
        entry = raw.strip().lower()
        if not entry:
            continue
        entry = re.sub(r"^https?://", "", entry)
        # Drop any path component (and trailing slash) so a pasted URL like
        # ``https://webui.example.com/path`` reduces to the bare host instead
        # of an unmatchable ``webui.example.com/path`` token. Splitting at the
        # first "/" subsumes the trailing-slash case; bracketed IPv6 and
        # ``host:port`` forms contain no "/" so they pass through untouched.
        entry = entry.split("/", 1)[0]
        stripped = _strip_port(entry)
        if stripped:
            out.add(stripped)
    return out


def get_allowed_hosts_from_env() -> set[str]:
    """Read ``HERMES_WEBUI_ALLOWED_HOSTS`` and return the parsed allowlist."""
    return parse_allowed_hosts(os.getenv("HERMES_WEBUI_ALLOWED_HOSTS", ""))


def is_host_allowed(host_header: str, *, explicit: Iterable[str] = ()) -> bool:
    """Return True if ``host_header`` is in the trusted set.

    Always allowed:
      - Loopback addresses (``127.0.0.0/8``, ``::1``, ``localhost``, ``*.localhost``)
      - Private LAN ranges (``10/8``, ``172.16/12``, ``192.168/16``)
      - IPv4 link-local (``169.254/16``) and IPv6 link-local (``fe80::/10``)
      - IPv6 unique-local (``fc00::/7``)
      - Tailscale CGNAT (``100.64/10``)

    Anything else must appear in ``explicit`` (operator-supplied allowlist).
    """
    name = _strip_port(host_header)
    if name is None:
        return False
    if _is_loopback_hostname(name):
        return True
    if _is_loopback_or_lan_ip(name):
        return True
    allowed = {e.lower() for e in explicit if e}
    return name in allowed


def enforce_host_guard(handler) -> bool:
    """Return True if the request's Host header is allowed.

    On rejection, sends a 400 response and returns False so the caller can
    short-circuit. Call before any auth, CSRF, or route logic so a rebound
    request never reaches business logic.
    """
    host = handler.headers.get("Host", "") if getattr(handler, "headers", None) else ""
    if is_host_allowed(host, explicit=get_allowed_hosts_from_env()):
        return True
    logger.warning(
        "[host-guard] rejected request with Host header %r — set "
        "HERMES_WEBUI_ALLOWED_HOSTS to allow it.",
        host,
    )
    body = b'{"error":"Host header not in allowlist"}'
    try:
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except Exception:
        # If the client disconnected mid-response, swallow the error — the
        # caller will see False and short-circuit; logging the reject is what
        # matters for diagnostics.
        logger.debug("host-guard reject failed to send", exc_info=True)
    return False
