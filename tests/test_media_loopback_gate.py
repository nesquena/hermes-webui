"""Defense-in-depth: passwordless /api/media is loopback-only."""

from io import BytesIO
from urllib.parse import urlparse


class _Handler:
    def __init__(self, *, client_ip="127.0.0.1", headers=None):
        self.client_address = (client_ip, 12345)
        self.headers = headers or {}
        self.status = None
        self.sent_headers = []
        self.wfile = BytesIO()
        self.command = "GET"
        self.path = "/api/media"

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def _no_auth(monkeypatch):
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    monkeypatch.delenv("HERMES_WEBUI_ONBOARDING_OPEN", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)


def test_media_gate_blocks_lan_when_auth_disabled(monkeypatch):
    from api import routes

    _no_auth(monkeypatch)
    handler = _Handler(client_ip="192.168.1.50")
    routes._handle_media(handler, urlparse("/api/media?path=/tmp/x.png"))
    assert handler.status == 403


def test_media_gate_allows_loopback_when_auth_disabled(monkeypatch, tmp_path):
    from api import routes

    _no_auth(monkeypatch)
    img = tmp_path / "x.png"
    # Minimal invalid file is fine — we only assert we passed the IP gate
    # (not 403). Mock path resolution by using /tmp under allowed roots.
    img.write_bytes(b"not-a-real-png")
    # Place under /tmp so allowed-roots check can proceed past the gate.
    import os
    import shutil

    target = f"/tmp/hermes-media-gate-test-{os.getpid()}.png"
    shutil.copy(img, target)
    try:
        handler = _Handler(client_ip="127.0.0.1")
        routes._handle_media(handler, urlparse(f"/api/media?path={target}"))
        assert handler.status != 403
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass


def test_media_gate_honors_onboarding_open(monkeypatch):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    monkeypatch.setenv("HERMES_WEBUI_ONBOARDING_OPEN", "1")
    handler = _Handler(client_ip="8.8.8.8")
    routes._handle_media(handler, urlparse("/api/media?path=/tmp/missing.png"))
    # Escape hatch admits the client; missing file → 404 (or 400), not 403.
    assert handler.status != 403
