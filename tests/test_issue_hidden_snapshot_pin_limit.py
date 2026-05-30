from collections import OrderedDict
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlparse


def test_pin_limit_ignores_hidden_precompression_snapshots(monkeypatch):
    import api.routes as routes

    class DummySession:
        def __init__(self, sid: str):
            self.session_id = sid
            self.pinned = False
            self.archived = False
            self.saved = False

        def save(self):
            self.saved = True

        def compact(self):
            return {
                "session_id": self.session_id,
                "pinned": self.pinned,
                "archived": self.archived,
                "pre_compression_snapshot": False,
            }

    class DummyLock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    target = DummySession("target")
    hidden_snapshot = {
        "session_id": "hidden-snapshot",
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": True,
    }
    visible_pin_a = {
        "session_id": "visible-a",
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": False,
    }
    visible_pin_b = {
        "session_id": "visible-b",
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": False,
    }

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": "target", "pinned": True})
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "load_settings", lambda: {"pinned_sessions_limit": 3})
    monkeypatch.setattr(routes, "all_sessions", lambda: [hidden_snapshot, visible_pin_a, visible_pin_b])
    monkeypatch.setattr(routes, "get_session", lambda sid: target if sid == "target" else None)
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, session: session)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: DummyLock())
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"error": msg, "status": status})
    monkeypatch.setattr(routes, "SESSIONS", OrderedDict())

    handler = SimpleNamespace(
        headers={},
        rfile=BytesIO(b"{}"),
        client_address=("127.0.0.1", 0),
    )
    parsed = urlparse("/api/session/pin")

    payload = cast(Any, routes.handle_post(handler, parsed))

    assert payload["ok"] is True
    assert payload["session"]["pinned"] is True
    assert target.pinned is True
    assert target.saved is True
