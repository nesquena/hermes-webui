from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


def _run_search(query):
    import api.routes as routes
    sessions_meta = [
        {"session_id": "active-s", "title": "boring", "profile": "default"},
        {"session_id": "other-s", "title": "secret needle title", "profile": "other"},
        {"session_id": "other-content-s", "title": "boring", "profile": "other"},
    ]
    sessions = {
        "other-content-s": SimpleNamespace(messages=[{"role": "user", "content": "secret needle body"}]),
        "active-s": SimpleNamespace(messages=[{"role": "user", "content": "nothing"}]),
        "other-s": SimpleNamespace(messages=[]),
    }
    captured = {}
    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), \
         patch("api.routes.get_session", side_effect=lambda sid: sessions[sid]), \
         patch("api.profiles.get_active_profile_name", return_value="default"), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def test_empty_session_search_scopes_to_active_profile():
    captured = _run_search("/api/sessions/search")
    assert [s["session_id"] for s in captured["payload"]["sessions"]] == ["active-s"]


def test_title_search_should_not_return_other_profile_rows():
    captured = _run_search("/api/sessions/search?q=needle&content=0")
    assert captured["payload"]["count"] == 0


def test_content_search_should_not_return_other_profile_rows():
    captured = _run_search("/api/sessions/search?q=needle&content=1&depth=0")
    assert captured["payload"]["count"] == 0


def test_all_profiles_opt_in_keeps_aggregate_session_search():
    captured = _run_search("/api/sessions/search?all_profiles=1")
    assert captured["payload"]["all_profiles"] is True
    assert [s["session_id"] for s in captured["payload"]["sessions"]] == [
        "active-s",
        "other-s",
        "other-content-s",
    ]
