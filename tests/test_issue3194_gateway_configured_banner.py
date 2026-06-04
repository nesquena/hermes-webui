"""Regression coverage for #3194 — two-container Docker first-deploy shows
"Gateway not configured" even though the gateway is running.

Reported by @chenghaopeng: after a fresh ``docker-compose.two-container.yml``
deploy, the WebUI banner says "Gateway not configured" while
``hermes gateway status`` reports the gateway is running. The trigger is an
empty ``identity_map`` (no conversation has happened yet, so no session
metadata exists) combined with an ``alive is None`` health payload whose
``details.reason`` is ``gateway_stale_running_state`` (the gateway is up but
hasn't ticked ``updated_at`` recently enough for the freshness check).

Before the fix, ``/api/gateway/status`` set ``configured = bool(identity_map)``
on the ``alive is None`` branch, so an empty identity_map → ``configured=False``
→ the misleading banner. The fix recognizes that an ``alive is None`` payload
which still carries gateway metadata (a ``gateway_state`` detail, or a stale-
running / stale-stopped reason) proves the gateway IS configured.

Mirrors the FakeHandler isolation pattern in
``tests/test_gateway_status_agent_health.py``.
"""
from __future__ import annotations

import json


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for routes.handle_get."""

    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


def _call_gateway_status(monkeypatch, *, health_payload, identity_map=None):
    """Invoke handle_get for /api/gateway/status with a stubbed health payload."""
    from urllib.parse import urlparse

    from api import routes

    monkeypatch.setattr(routes, "build_agent_health_payload", lambda: health_payload)
    monkeypatch.setattr(
        routes, "_load_gateway_session_identity_map", lambda: (identity_map or {})
    )

    handler = _FakeHandler()
    parsed = urlparse("/api/gateway/status")
    routes.handle_get(handler, parsed)
    return handler.get_json()


def test_stale_running_with_empty_identity_map_is_configured(monkeypatch):
    """#3194 core case: gateway up but not yet ticked, no sessions yet.

    alive=None + reason=gateway_stale_running_state + empty identity_map must
    NOT report 'Gateway not configured'.
    """
    payload = {
        "alive": None,
        "details": {"state": "unknown", "reason": "gateway_stale_running_state",
                    "gateway_state": "running"},
    }
    data = _call_gateway_status(monkeypatch, health_payload=payload, identity_map={})

    assert data["configured"] is True, (
        "A stale-running gateway with no conversations yet is still configured "
        "— the banner must not say 'Gateway not configured' (#3194)."
    )
    # No live tick / no sessions → not 'running' for the activity indicator,
    # but that's a separate signal from 'configured'.
    assert data["running"] is False


def test_gateway_state_running_detail_marks_configured(monkeypatch):
    """An alive=None payload whose details report gateway_state == 'running'
    is configured, even if the reason string differs — the running metadata
    is the signal."""
    payload = {
        "alive": None,
        "details": {"state": "unknown", "reason": "cross_container_freshness",
                    "gateway_state": "running"},
    }
    data = _call_gateway_status(monkeypatch, health_payload=payload, identity_map={})
    assert data["configured"] is True


def test_stale_stopped_with_empty_identity_map_not_configured(monkeypatch):
    """No-regression for #1944: a stale-STOPPED gateway must NOT report
    configured when there's no traffic. agent_health emits
    gateway_stale_stopped_state precisely so a stopped service the user isn't
    running reads like 'no root gateway configured' rather than nagging.
    Only stale-RUNNING metadata flips configured=True (#3194)."""
    payload = {
        "alive": None,
        "details": {"state": "unknown", "reason": "gateway_stale_stopped_state",
                    "gateway_state": "stopped"},
    }
    data = _call_gateway_status(monkeypatch, health_payload=payload, identity_map={})
    assert data["configured"] is False
    assert data["running"] is False


def test_truly_unconfigured_stays_unconfigured(monkeypatch):
    """No-regression guard: alive=None with reason=gateway_not_configured and
    no metadata and no identity_map → genuinely not configured."""
    payload = {
        "alive": None,
        "details": {"state": "unknown", "reason": "gateway_not_configured"},
    }
    data = _call_gateway_status(monkeypatch, health_payload=payload, identity_map={})
    assert data["configured"] is False
    assert data["running"] is False


def test_unconfigured_but_with_sessions_still_configured(monkeypatch):
    """Pre-existing behavior preserved: even with no gateway metadata, a
    non-empty identity_map implies a configured gateway."""
    payload = {
        "alive": None,
        "details": {"state": "unknown", "reason": "gateway_not_configured"},
    }
    idmap = {"sid-1": {"platform": "telegram", "raw_source": "telegram"}}
    data = _call_gateway_status(monkeypatch, health_payload=payload, identity_map=idmap)
    assert data["configured"] is True
    assert data["running"] is True


def test_alive_true_unaffected(monkeypatch):
    """A live gateway is configured + running regardless of identity_map."""
    payload = {"alive": True, "details": {"state": "alive"}}
    data = _call_gateway_status(monkeypatch, health_payload=payload, identity_map={})
    assert data["configured"] is True
    assert data["running"] is True


def test_alive_false_configured_not_running(monkeypatch):
    """alive=False (metadata exists, process down) stays configured-but-down."""
    payload = {"alive": False, "details": {"state": "down", "reason": "gateway_not_running"}}
    data = _call_gateway_status(monkeypatch, health_payload=payload, identity_map={})
    assert data["configured"] is True
    assert data["running"] is False
