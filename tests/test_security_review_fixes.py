import io
from types import SimpleNamespace
from urllib.parse import urlsplit
from pathlib import Path


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
        self.status = None
        self.sent_headers = []

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def test_onboarding_local_gate_ignores_forwarded_ip_unless_trusted(monkeypatch):
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    handler = _Handler(
        client_ip="8.8.8.8",
        headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "10.0.0.2"},
    )

    assert routes._onboarding_request_is_local(handler) is False


def test_onboarding_local_gate_uses_forwarded_ip_when_explicitly_trusted(monkeypatch):
    from api import routes

    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _Handler(
        client_ip="8.8.8.8",
        headers={"X-Forwarded-For": "10.0.0.2", "X-Real-IP": "203.0.113.11"},
    )

    assert routes._onboarding_request_is_local(handler) is True


def test_onboarding_trusted_forwarded_for_uses_proxy_appended_rightmost_ip(monkeypatch):
    from api import routes

    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _Handler(
        client_ip="10.0.0.10",
        headers={"X-Forwarded-For": "127.0.0.1, 8.8.8.8"},
    )

    assert routes._onboarding_request_is_local(handler) is False


def test_docker_env_log_obfuscates_password_and_secret_names():
    src = Path("docker_init.bash").read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if l.startswith("export ENV_OBFUSCATE_PART="))

    assert "PASSWORD" in line
    assert "SECRET" in line
    assert "TOKEN" in line
    assert "API" in line
    assert "KEY" in line


def test_get_update_check_returns_cache_without_fetch(monkeypatch):
    from api import routes, updates

    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    monkeypatch.setattr(updates, "cached_update_status", lambda include_agent=True: {"checked_at": 123, "webui": None, "agent": None, "include_agent": include_agent})
    monkeypatch.setattr(updates, "check_for_updates", lambda *a, **k: (_ for _ in ()).throw(AssertionError("GET must not fetch")))

    handler = _Handler(client_ip="127.0.0.1")
    routes.handle_get(handler, urlsplit("/api/updates/check?force=1"))
    assert handler.status == 200


def test_cached_update_status_does_not_drop_agent_info_when_reenabled(monkeypatch):
    from api import updates

    cached_agent = {"name": "agent", "behind": 2}
    monkeypatch.setattr(
        updates,
        "_update_cache",
        {
            "webui": {"name": "webui", "behind": 0},
            "agent": cached_agent,
            "checked_at": 123,
            "include_agent": False,
        },
    )

    result = updates.cached_update_status(include_agent=True)

    assert result["agent"] == cached_agent


def test_post_update_check_performs_forced_fetch(monkeypatch):
    from api import routes

    calls = []
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    def fake_check(*, force=False, include_agent=True):
        calls.append((force, include_agent))
        return {"checked_at": 456, "webui": None, "agent": None}

    monkeypatch.setattr("api.updates.check_for_updates", fake_check)
    body = b'{"force": true}'
    handler = _Handler(client_ip="127.0.0.1", body=body, headers={"Content-Length": str(len(body))})
    routes.handle_post(handler, SimpleNamespace(path="/api/updates/check", query=""))
    assert handler.status == 200
    assert calls == [(True, True)]


def test_onboarding_untrusted_forwarded_header_denies_lan_proxy_socket(monkeypatch):
    """Reverse-proxy regression (release-gate CORE fix): when forwarded headers
    are present but HERMES_WEBUI_TRUST_FORWARDED_FOR is NOT set, the spoofable
    header is ignored and locality is judged by the raw socket — but a PRIVATE/LAN
    raw socket (a separate proxy box that could be forwarding an arbitrary public
    client) is NOT treated as local. A loopback raw socket is still genuine
    same-host and remains allowed (a remote attacker cannot forge a 127.0.0.1 TCP
    source). Operators with a LAN proxy must set HERMES_WEBUI_TRUST_FORWARDED_FOR=1.
    """
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)

    # LAN proxy box (private raw socket) forwarding a public client → DENY
    handler = _Handler(client_ip="10.0.0.5", headers={"X-Real-IP": "203.0.113.7"})
    assert routes._onboarding_request_is_local(handler) is False
    handler2 = _Handler(client_ip="172.20.0.1", headers={"X-Forwarded-For": "8.8.8.8"})
    assert routes._onboarding_request_is_local(handler2) is False

    # Genuine same-host: loopback raw socket is local even if a forwarded header
    # is present (the TCP source genuinely came from localhost; unspoofable).
    handler3 = _Handler(client_ip="127.0.0.1", headers={"X-Forwarded-For": "8.8.8.8"})
    assert routes._onboarding_request_is_local(handler3) is True


def test_onboarding_spoofed_forwarded_header_from_public_socket_denied(monkeypatch):
    """The original spoof hole: a public client setting X-Forwarded-For=127.0.0.1
    must NOT bypass the gate. The forwarded header is ignored; the public raw
    socket governs → denied.
    """
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    handler = _Handler(client_ip="8.8.8.8", headers={"X-Forwarded-For": "127.0.0.1"})
    assert routes._onboarding_request_is_local(handler) is False


def test_onboarding_direct_loopback_without_forwarded_headers_is_local(monkeypatch):
    """A genuine direct local client (no proxy headers) is still allowed."""
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    handler = _Handler(client_ip="127.0.0.1", headers={})
    assert routes._onboarding_request_is_local(handler) is True

    handler_public = _Handler(client_ip="8.8.8.8", headers={})
    assert routes._onboarding_request_is_local(handler_public) is False
