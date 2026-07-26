"""Tests for temporary session creation and list/search exclusion."""

from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import urlparse

import api.routes as routes


def _post(path: str, body: dict, monkeypatch):
    cap = {}

    def _j(_handler, payload, *_, status=200, **__):
        cap["ok"] = payload
        cap["status"] = status
        return True

    def _bad(_handler, msg, status=400, **__):
        cap["bad"] = (msg, status)
        return True

    handler = MagicMock()
    handler.command = "POST"
    handler.headers = {}

    monkeypatch.setattr(routes, "read_body", lambda _h: body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _p: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)

    routes.handle_post(handler, urlparse(path))
    return cap


def test_session_new_accepts_temporary_flag(monkeypatch):
    captured = {}

    class _Session:
        def __init__(self):
            self.session_id = "tmp1"
            self.profile = "default"
            self.messages = []
            self.temporary = False

        def compact(self):
            return {
                "session_id": self.session_id,
                "temporary": self.temporary,
            }

    def _new_session(**kwargs):
        captured.update(kwargs)
        s = _Session()
        s.temporary = bool(kwargs.get("temporary"))
        return s

    monkeypatch.setattr(routes, "new_session", _new_session)
    monkeypatch.setattr(
        routes,
        "_session_id_visible_to_request_profile",
        lambda *_a, **_k: True,
    )

    cap = _post("/api/session/new", {"temporary": True}, monkeypatch)
    assert "bad" not in cap
    assert captured.get("temporary") is True
    assert cap["ok"]["session"]["temporary"] is True


def test_session_new_defaults_temporary_false(monkeypatch):
    captured = {}

    class _Session:
        def __init__(self):
            self.session_id = "norm1"
            self.profile = "default"
            self.messages = []
            self.temporary = False

        def compact(self):
            return {"session_id": self.session_id, "temporary": self.temporary}

    def _new_session(**kwargs):
        captured.update(kwargs)
        s = _Session()
        s.temporary = bool(kwargs.get("temporary"))
        return s

    monkeypatch.setattr(routes, "new_session", _new_session)

    cap = _post("/api/session/new", {}, monkeypatch)
    assert "bad" not in cap
    assert captured.get("temporary") is False
    assert cap["ok"]["session"]["temporary"] is False


def _webui_row(sid: str, *, temporary: bool = False, message_count: int = 1) -> dict:
    return {
        "session_id": sid,
        "title": sid,
        "profile": "default",
        "updated_at": 100,
        "last_message_at": 100,
        "message_count": message_count,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
        "temporary": temporary,
    }


def test_session_list_excludes_temporary_by_default(monkeypatch):
    """Default history/sidebar list must not surface temporary chats."""
    rows = [
        _webui_row("keep-me", temporary=False),
        _webui_row("tmp-me", temporary=True),
    ]
    monkeypatch.setattr(routes, "all_sessions", lambda **_k: list(rows))
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_k: [])
    monkeypatch.setattr(
        routes, "_reconcile_stale_stream_state_for_session_rows", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        routes, "_prune_orphaned_webui_zero_message_sessions", lambda rows, **_k: rows
    )

    excluded = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        exclude_temporary=True,
    )
    ids = [s["session_id"] for s in excluded["sessions"]]
    assert "keep-me" in ids
    assert "tmp-me" not in ids

    included = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        exclude_temporary=False,
    )
    ids_in = [s["session_id"] for s in included["sessions"]]
    assert "keep-me" in ids_in
    assert "tmp-me" in ids_in


def test_session_list_cache_key_includes_exclude_temporary():
    base = dict(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_ex = routes._session_list_cache_key(**base, exclude_temporary=True)
    key_in = routes._session_list_cache_key(**base, exclude_temporary=False)
    assert key_ex != key_in
    assert key_ex[-1] is True
    assert key_in[-1] is False


def test_sessions_search_excludes_temporary_by_default(monkeypatch):
    rows = [
        _webui_row("keep-search", temporary=False),
        _webui_row("tmp-search", temporary=True),
    ]
    # Titles match the query so both would hit without the temporary filter.
    rows[0]["title"] = "alpha keep"
    rows[1]["title"] = "alpha tmp"

    monkeypatch.setattr(routes, "all_sessions", lambda **_k: list(rows))
    monkeypatch.setattr(routes, "_all_profiles_enabled", lambda _p: False)
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "load_settings", lambda: {})

    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    captured = {}

    def _j(_handler, payload, *_, **__):
        captured["payload"] = payload
        return True

    handler = MagicMock()
    monkeypatch.setattr(routes, "j", _j)

    # Default: temporary excluded
    routes._handle_sessions_search(
        handler, urlparse("/api/sessions/search?q=alpha&content=0")
    )
    default_ids = [s["session_id"] for s in captured["payload"].get("sessions", [])]
    assert "keep-search" in default_ids
    assert "tmp-search" not in default_ids

    # Opt-in: temporary included
    routes._handle_sessions_search(
        handler,
        urlparse("/api/sessions/search?q=alpha&content=0&include_temporary=1"),
    )
    opt_ids = [s["session_id"] for s in captured["payload"].get("sessions", [])]
    assert "keep-search" in opt_ids
    assert "tmp-search" in opt_ids
