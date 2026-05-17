import io
import json
from urllib.parse import urlparse

import api.profiles as profiles
import api.routes as routes


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_sessions_list_reconciles_stale_stream_state_before_serializing(monkeypatch):
    repaired = {"value": False}
    all_sessions_calls = {"count": 0}

    class _Session:
        def __init__(self):
            self.session_id = "stale-session"
            self.active_stream_id = "stale-stream"

    def fake_all_sessions(diag=None):
        all_sessions_calls["count"] += 1
        if repaired["value"]:
            active_stream_id = None
            is_streaming = False
        else:
            active_stream_id = "stale-stream"
            is_streaming = False
        return [
            {
                "session_id": "stale-session",
                "title": "Stale Session",
                "profile": "default",
                "active_stream_id": active_stream_id,
                "is_streaming": is_streaming,
                "updated_at": 1,
                "last_message_at": 1,
            }
        ]

    def fake_get_session(session_id, metadata_only=False):
        assert session_id == "stale-session"
        assert metadata_only is True
        return _Session()

    def fake_clear_stale_stream_state(session):
        repaired["value"] = True
        session.active_stream_id = None
        return True

    monkeypatch.setattr(routes, "all_sessions", fake_all_sessions)
    monkeypatch.setattr(routes, "get_session", fake_get_session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", fake_clear_stale_stream_state)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/sessions")
    routes.handle_get(handler, parsed)

    assert handler.status == 200
    payload = handler.json_body()
    sessions = payload["sessions"]
    assert all_sessions_calls["count"] == 2
    assert repaired["value"] is True
    assert sessions[0]["active_stream_id"] is None
    assert sessions[0]["is_streaming"] is False


def test_reconcile_stale_stream_state_skips_live_stream_rows(monkeypatch):
    loaded = []

    def fake_get_session(session_id, metadata_only=False):
        loaded.append((session_id, metadata_only))
        raise AssertionError("live stream rows should not be loaded for cleanup")

    monkeypatch.setattr(routes, "get_session", fake_get_session)

    changed = routes._reconcile_stale_stream_state_for_session_rows([
        {
            "session_id": "live-session",
            "active_stream_id": "live-stream",
            "is_streaming": True,
        }
    ])

    assert changed is False
    assert loaded == []
