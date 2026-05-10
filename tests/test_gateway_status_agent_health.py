"""Regression coverage: /api/gateway/status uses agent_health payload as
the authoritative 'running' signal (#d0568682 / parent review t_9098e3db).

Before the fix, the handler called gateway.status.get_running_pid() directly
and fell back to bool(identity_map) when the module was unavailable. The fix
makes it consult agent_health.build_agent_health_payload() so the tri-state
`alive` field is the single source of truth for gateway process health.

Tests use handle_get + monkeypatched build_agent_health_payload() and
_load_gateway_session_identity_map() to isolate the gateway status route
from real filesystem state.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse


# ── FakeHandler (mirrors test_1560_password_env_var_no_op._FakeHandler) ────────


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for routes.handle_get."""

    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        """Accumulate bytes written to wfile."""
        self.body.extend(
            data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8")
        )

    def get_json(self):
        """Parse the accumulated body as JSON."""
        return json.loads(self.body.decode("utf-8"))


# ── Helpers ──────────────────────────────────────────────────────────────────


def _call_gateway_status(monkeypatch, agent_health_alive, identity_map=None):
    """Invoke handle_get for /api/gateway/status and return the parsed JSON.

    monkeypatches build_agent_health_payload to return the given `alive` value
    and _load_gateway_session_identity_map to return the given identity_map.
    """
    from api import routes

    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {
            "alive": agent_health_alive,
            "checked_at": "2026-05-06T12:00:00+00:00",
            "details": {},
        },
    )

    if identity_map is not None:
        monkeypatch.setattr(
            routes,
            "_load_gateway_session_identity_map",
            lambda: identity_map,
        )

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/gateway/status")
    routes.handle_get(handler, parsed)
    return handler.get_json()


# ── Acceptance criteria tests ─────────────────────────────────────────────────


def test_gateway_status_running_true_when_agent_health_alive_and_no_sessions(
    monkeypatch,
):
    """AC1: alive=true + empty identity_map → running=true, configured=true, platforms=[]"""
    result = _call_gateway_status(monkeypatch, agent_health_alive=True, identity_map={})
    assert result["running"] is True
    assert result["configured"] is True
    assert result["platforms"] == []


def test_gateway_status_running_false_when_agent_health_alive_false_and_no_sessions(
    monkeypatch,
):
    """AC2: alive=false + empty identity_map → running=false, configured=true, platforms=[]"""
    result = _call_gateway_status(
        monkeypatch, agent_health_alive=False, identity_map={}
    )
    assert result["running"] is False
    assert result["configured"] is True
    assert result["platforms"] == []


def test_gateway_status_running_false_when_agent_health_alive_none_and_no_sessions(
    monkeypatch,
):
    """When alive=None (not configured): fall back to identity_map heuristic,
    and set configured=false so frontend can show 'not configured' state."""
    result = _call_gateway_status(monkeypatch, agent_health_alive=None, identity_map={})
    assert result["running"] is False
    assert result["configured"] is False
    assert result["platforms"] == []


def test_gateway_status_running_true_and_platforms_when_agent_health_alive_and_sessions(
    monkeypatch,
):
    """AC3: alive=true + sessions with platforms → running=true, configured=true, platforms populated"""
    identity_map = {
        "sess_a": {"raw_source": "telegram", "platform": "telegram"},
        "sess_b": {"raw_source": "discord", "platform": "discord"},
    }
    result = _call_gateway_status(
        monkeypatch, agent_health_alive=True, identity_map=identity_map
    )
    assert result["running"] is True
    assert result["configured"] is True
    assert len(result["platforms"]) == 2
    names = {p["name"] for p in result["platforms"]}
    assert names == {"telegram", "discord"}


# ── Edge case tests ───────────────────────────────────────────────────────────


def test_gateway_status_alive_none_falls_back_to_identity_map_heuristic(monkeypatch):
    """When alive=None (not configured) but sessions exist, running reflects identity_map.
    configured=false tells the frontend to show 'not configured' state."""
    from api import routes

    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {
            "alive": None,
            "checked_at": "2026-05-06T12:00:00+00:00",
            "details": {},
        },
    )
    monkeypatch.setattr(
        routes,
        "_load_gateway_session_identity_map",
        lambda: {"sess_c": {"raw_source": "telegram", "platform": "telegram"}},
    )

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/gateway/status")
    routes.handle_get(handler, parsed)
    result = handler.get_json()
    # Fallback to identity_map: sessions exist → running=true
    assert result["running"] is True
    # But configured=false because alive was None (no gateway metadata)
    assert result["configured"] is False


def test_gateway_status_handles_corrupted_sessions_json(monkeypatch):
    """Edge: sessions.json is corrupted → identity_map empty, rely on agent_health alone."""
    from api import routes

    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {
            "alive": True,
            "checked_at": "2026-05-06T12:00:00+00:00",
            "details": {},
        },
    )
    # _load_gateway_session_identity_map already returns {} on JSON parse failure;
    # we monkeypatch it to return {} to simulate corrupted file.
    monkeypatch.setattr(routes, "_load_gateway_session_identity_map", lambda: {})

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/gateway/status")
    routes.handle_get(handler, parsed)
    result = handler.get_json()
    assert result["running"] is True
    assert result["platforms"] == []
    assert result["session_count"] == 0


def test_gateway_status_blank_platform_fields_empty_platforms_running_true(monkeypatch):
    """Edge: sessions exist but all have blank/missing platform fields → platforms=[], running=true."""
    from api import routes

    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {
            "alive": True,
            "checked_at": "2026-05-06T12:00:00+00:00",
            "details": {},
        },
    )
    monkeypatch.setattr(
        routes,
        "_load_gateway_session_identity_map",
        lambda: {
            "sess_d": {"raw_source": "", "platform": ""},
            "sess_e": {},  # no platform field at all
        },
    )

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/gateway/status")
    routes.handle_get(handler, parsed)
    result = handler.get_json()
    assert result["running"] is True
    assert result["platforms"] == []


# ── Existing behavior preservation tests ──────────────────────────────────────


def test_gateway_status_running_false_when_agent_health_down_even_with_sessions(
    monkeypatch,
):
    """When agent_health says alive=false, running should be false regardless of sessions."""
    from api import routes

    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {
            "alive": False,
            "checked_at": "2026-05-06T12:00:00+00:00",
            "details": {},
        },
    )
    monkeypatch.setattr(
        routes,
        "_load_gateway_session_identity_map",
        lambda: {"sess_f": {"raw_source": "telegram", "platform": "telegram"}},
    )

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/gateway/status")
    routes.handle_get(handler, parsed)
    result = handler.get_json()
    # Running should be false even though sessions exist — agent_health is authoritative
    assert result["running"] is False
    # But configured=true because alive=False means gateway metadata exists
    assert result["configured"] is True
    # But platforms should still be extracted from sessions
    assert len(result["platforms"]) == 1
    assert result["platforms"][0]["name"] == "telegram"


def test_gateway_status_missing_r_field_handled_by_frontend(monkeypatch):
    """Edge: response always has 'running' and 'configured' fields.
    Frontend handles missing field via catch block. This test verifies the backend
    always includes both fields in responses."""
    result = _call_gateway_status(monkeypatch, agent_health_alive=True, identity_map={})
    assert "running" in result
    assert "configured" in result


def test_gateway_status_last_active_empty_when_alive_and_no_sessions_path(monkeypatch):
    """Bonus: alive=true + identity_map={} → last_active is empty string.
    This guards the 'if running and sessions_path.exists()' guard from being
    silently removed in a future refactor that might expose a stale timestamp."""
    result = _call_gateway_status(monkeypatch, agent_health_alive=True, identity_map={})
    assert result["running"] is True
    assert result["configured"] is True
    # In test context, sessions_path won't exist (no real filesystem),
    # so last_active must be empty.
    assert result["last_active"] == ""
