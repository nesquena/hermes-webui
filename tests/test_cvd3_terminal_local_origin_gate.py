"""Tests: embedded-terminal endpoints are gated to local origins when auth is disabled.

CVD #3 (A2): the embedded terminal endpoints (`/api/terminal/start|input|resize|close`
and `/api/terminal/output`) spawn / drive a PTY shell that runs arbitrary commands as the
server-process user. `check_auth()` returns True unconditionally when authentication is not
configured (the default out-of-the-box state), so without a network-scope gate ANY caller
able to reach the port — including an unauthenticated remote attacker on a passwordless
public bind — could obtain remote code execution.

These tests pin the local-origin gate (`_embedded_terminal_gate_allows`, mirroring the
onboarding/bootstrap trust model) on every terminal handler: public clients are refused with
403 when auth is disabled; loopback/private clients and auth-enabled sessions are admitted;
spoofed forwarded headers do not establish locality; and the explicit
HERMES_WEBUI_ONBOARDING_OPEN escape hatch is honored.
"""

import io
from types import SimpleNamespace


class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _Handler:
    def __init__(self, *, client_ip="8.8.8.8", headers=None, body=b"{}"):
        self.client_address = (client_ip, 12345)
        self.headers = _Headers(headers or {})
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.request = None
        self.status = None
        self.sent_headers = []

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def _no_auth(monkeypatch):
    """Default out-of-the-box state: no password, no passkey, no opt-outs."""
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    monkeypatch.delenv("HERMES_WEBUI_ONBOARDING_OPEN", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)


# --------------------------------------------------------------------------
# Gate predicate
# --------------------------------------------------------------------------

def test_terminal_gate_blocks_public_client_when_auth_disabled(monkeypatch):
    from api import routes

    _no_auth(monkeypatch)
    handler = _Handler(client_ip="8.8.8.8", headers={})
    assert routes._embedded_terminal_gate_allows(handler) is False


def test_terminal_gate_allows_loopback_client_when_auth_disabled(monkeypatch):
    from api import routes

    _no_auth(monkeypatch)
    handler = _Handler(client_ip="127.0.0.1", headers={})
    assert routes._embedded_terminal_gate_allows(handler) is True


def test_terminal_gate_allows_private_client_when_auth_disabled(monkeypatch):
    """Docker bridge / LAN (no forwarded header present) is treated as local."""
    from api import routes

    _no_auth(monkeypatch)
    handler = _Handler(client_ip="172.17.0.1", headers={})
    assert routes._embedded_terminal_gate_allows(handler) is True


def test_terminal_gate_ignores_spoofed_forwarded_header_from_public_socket(monkeypatch):
    """A public socket spoofing X-Forwarded-For: 127.0.0.1 must NOT pass the gate."""
    from api import routes

    _no_auth(monkeypatch)
    handler = _Handler(client_ip="8.8.8.8", headers={"X-Forwarded-For": "127.0.0.1"})
    assert routes._embedded_terminal_gate_allows(handler) is False


def test_terminal_gate_allows_any_client_when_auth_enabled(monkeypatch):
    """With auth enabled, check_auth() already verified the cookie upstream."""
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    monkeypatch.delenv("HERMES_WEBUI_ONBOARDING_OPEN", raising=False)
    handler = _Handler(client_ip="8.8.8.8", headers={})
    assert routes._embedded_terminal_gate_allows(handler) is True


def test_terminal_gate_honors_onboarding_open_escape_hatch(monkeypatch):
    """Deliberately-exposed passwordless server (secured elsewhere) opts out."""
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    monkeypatch.setenv("HERMES_WEBUI_ONBOARDING_OPEN", "1")
    handler = _Handler(client_ip="8.8.8.8", headers={})
    assert routes._embedded_terminal_gate_allows(handler) is True


# --------------------------------------------------------------------------
# Handler dispatch — the RCE-bearing endpoints refuse public clients (403)
# without ever reaching the PTY-spawning code.
# --------------------------------------------------------------------------

def test_terminal_start_refuses_public_client_without_spawning(monkeypatch):
    from api import routes

    _no_auth(monkeypatch)

    spawned = {"called": False}
    # If the gate fails to block, this would be the RCE primitive — assert it
    # is never reached. _terminal_session_lookup also must not run.
    monkeypatch.setattr(
        routes, "_terminal_session_lookup",
        lambda body: spawned.__setitem__("called", True) or ("s", SimpleNamespace(workspace="")),
    )

    handler = _Handler(client_ip="8.8.8.8", body=b'{"session_id":"s"}')
    routes._handle_terminal_start(handler, {"session_id": "s"})

    assert handler.status == 403
    assert spawned["called"] is False


def test_terminal_input_refuses_public_client_without_writing(monkeypatch):
    from api import routes

    _no_auth(monkeypatch)

    wrote = {"called": False}
    import api.terminal as term_mod
    monkeypatch.setattr(
        term_mod, "write_terminal",
        lambda sid, data: wrote.__setitem__("called", True),
    )

    handler = _Handler(client_ip="8.8.8.8")
    routes._handle_terminal_input(handler, {"session_id": "s", "data": "id\n"})

    assert handler.status == 403
    assert wrote["called"] is False


def test_terminal_output_refuses_public_client(monkeypatch):
    from api import routes

    _no_auth(monkeypatch)
    handler = _Handler(client_ip="8.8.8.8")
    routes._handle_terminal_output(handler, SimpleNamespace(path="/api/terminal/output", query="session_id=s"))
    assert handler.status == 403


def test_terminal_close_refuses_public_client(monkeypatch):
    from api import routes

    _no_auth(monkeypatch)
    handler = _Handler(client_ip="8.8.8.8")
    routes._handle_terminal_close(handler, {"session_id": "s"})
    assert handler.status == 403


def test_terminal_resize_refuses_public_client(monkeypatch):
    from api import routes

    _no_auth(monkeypatch)
    handler = _Handler(client_ip="8.8.8.8")
    routes._handle_terminal_resize(handler, {"session_id": "s", "rows": 24, "cols": 80})
    assert handler.status == 403


def test_terminal_start_loopback_client_passes_gate(monkeypatch):
    """A genuine same-host client clears the gate and proceeds to normal lookup."""
    from api import routes

    _no_auth(monkeypatch)

    reached = {"called": False}
    monkeypatch.setattr(
        routes, "_terminal_session_lookup",
        lambda body: reached.__setitem__("called", True) or (_ for _ in ()).throw(KeyError("Session not found")),
    )

    handler = _Handler(client_ip="127.0.0.1", body=b'{"session_id":"s"}')
    routes._handle_terminal_start(handler, {"session_id": "s"})

    # Gate passed → lookup ran → 404 (no such session), NOT 403.
    assert reached["called"] is True
    assert handler.status == 404


# ---------------------------------------------------------------------------
# #5764 — trusted-proxy forwarded-client trust model, full truth table.
# The gate honors a forwarded client IP ONLY when the un-spoofable raw socket
# peer is a trusted proxy (loopback, or in HERMES_WEBUI_TRUSTED_PROXY_CIDRS),
# and only when HERMES_WEBUI_TRUST_FORWARDED_FOR=1. It must (a) never let a
# direct public client spoof itself local, (b) never lock out a direct
# loopback/LAN client with no proxy header, and (c) fail closed on malformed
# chains. See api/routes.py::_onboarding_request_is_local.
# ---------------------------------------------------------------------------


class _MultiHeaders(dict):
    """Headers stub supporting repeated X-Forwarded-For via get_all()."""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v[-1] if isinstance(v, list) else v
        return default

    def get_all(self, key):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v if isinstance(v, list) else [v]
        return []


class _MHandler:
    def __init__(self, *, client_ip, headers=None):
        self.client_address = (client_ip, 12345)
        self.headers = _MultiHeaders(headers or {})


def _clear_fwd_env(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)


import pytest


@pytest.mark.parametrize(
    "name,client_ip,headers,env,expected",
    [
        # --- spoof attempts: direct client sets a forwarded header ---
        ("spoof_xff_loopback_default", "8.8.8.8", {"X-Forwarded-For": "127.0.0.1"}, {}, False),
        ("spoof_xrealip_default", "8.8.8.8", {"X-Real-IP": "127.0.0.1"}, {}, False),
        ("spoof_xff_loopback_trust_on", "8.8.8.8", {"X-Forwarded-For": "127.0.0.1"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, False),
        # --- direct clients, no proxy ---
        ("direct_loopback", "127.0.0.1", {}, {}, True),
        ("direct_lan", "192.168.1.50", {}, {}, True),
        ("direct_public", "8.8.8.8", {}, {}, False),
        ("direct_lan_trust_on_no_header", "192.168.1.50", {},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, True),
        # --- trusted loopback proxy, TRUST on ---
        ("loopback_proxy_public_client", "127.0.0.1", {"X-Forwarded-For": "8.8.8.8"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, False),
        ("loopback_proxy_private_client", "127.0.0.1", {"X-Forwarded-For": "192.168.1.50"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, True),
        # right-to-left: first non-trusted hop is the client
        ("chain_public_then_proxy", "127.0.0.1", {"X-Forwarded-For": "8.8.8.8, 127.0.0.1"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, False),
        # ATTACK: hide a public field behind a trusted first field
        ("attack_hide_public_behind_trusted", "127.0.0.1",
         {"X-Forwarded-For": "127.0.0.1, 8.8.8.8"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, False),
        # repeated XFF headers (get_all): "8.8.8.8" then "127.0.0.1"
        ("attack_repeated_xff_headers", "127.0.0.1",
         {"X-Forwarded-For": ["127.0.0.1", "8.8.8.8"]},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, False),
        # --- malformed chains fail closed ---
        ("malformed_empty_xff", "127.0.0.1", {"X-Forwarded-For": ","},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, False),
        ("malformed_garbage_xff", "127.0.0.1", {"X-Forwarded-For": "notanip"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, False),
        ("malformed_blank_hop_in_chain", "127.0.0.1", {"X-Forwarded-For": "192.168.1.5, , 127.0.0.1"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1"}, False),
        # --- remote proxy via CIDR allowlist ---
        ("remote_trusted_proxy_private_client", "10.9.9.9", {"X-Forwarded-For": "192.168.1.50"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1", "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": "10.9.9.0/24"}, True),
        ("remote_trusted_proxy_public_client", "10.9.9.9", {"X-Forwarded-For": "8.8.8.8"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1", "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": "10.9.9.0/24"}, False),
        # invalid CIDR is skipped (never widens trust); peer 10.9.9.9 is a direct
        # private LAN box with a forwarded header present but no trusted proxy →
        # denied (could be relaying an unseen client).
        ("invalid_cidr_private_peer_with_header", "10.9.9.9", {"X-Forwarded-For": "8.8.8.8"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1", "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": "not-a-cidr"}, False),
        # --- opt-in OFF: raw peer authoritative, header ignored ---
        ("trust_off_loopback_proxy_xff_public", "127.0.0.1", {"X-Forwarded-For": "8.8.8.8"}, {}, True),
        ("trust_off_lan_peer_with_header", "10.0.0.5", {"X-Real-IP": "203.0.113.7"}, {}, False),
        # --- #5764 re-gate: IPv4-mapped-IPv6 must be family-aware ---
        # mapped-IPv6 proxy peer matches an IPv4 CIDR allowlist -> trusted -> private client local
        ("mapped_ipv6_proxy_peer_in_ipv4_cidr", "::ffff:10.9.9.9", {"X-Forwarded-For": "192.168.1.50"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1", "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": "10.9.9.0/24"}, True),
        # mapped-IPv6 proxy peer, public client -> DENY
        ("mapped_ipv6_proxy_peer_public_client", "::ffff:10.9.9.9", {"X-Forwarded-For": "8.8.8.8"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1", "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": "10.9.9.0/24"}, False),
        # mapped-IPv6 TRUSTED HOP inside the chain must be skipped so the preceding
        # PUBLIC client is returned -> DENY (the security-critical case).
        ("mapped_ipv6_trusted_hop_hides_public", "127.0.0.1",
         {"X-Forwarded-For": "8.8.8.8, ::ffff:10.9.9.9"},
         {"HERMES_WEBUI_TRUST_FORWARDED_FOR": "1", "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": "10.9.9.0/24"}, False),
    ],
)
def test_onboarding_local_gate_trust_model_truth_table(
    monkeypatch, name, client_ip, headers, env, expected
):
    from api import routes

    _clear_fwd_env(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    handler = _MHandler(client_ip=client_ip, headers=headers)
    assert routes._onboarding_request_is_local(handler) is expected, name
