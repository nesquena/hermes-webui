import io
import json
import pathlib
from urllib.parse import urlparse

import api.profiles as profiles
import api.routes as routes
import pytest


@pytest.fixture(autouse=True)
def _clear_session_list_cache_between_tests():
    routes._session_list_cache_clear()
    yield
    routes._session_list_cache_clear()


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


def _sessions_payload_rows():
    return [
        {
            "session_id": "visible-active",
            "title": "Visible active",
            "profile": "default",
            "archived": False,
            "message_count": 3,
            "updated_at": 30,
            "last_message_at": 30,
        },
        {
            "session_id": "archived-history",
            "title": "Archived history",
            "profile": "default",
            "archived": True,
            "message_count": 4,
            "updated_at": 20,
            "last_message_at": 20,
        },
        {
            "session_id": "other-profile",
            "title": "Other profile",
            "profile": "other",
            "archived": False,
            "message_count": 5,
            "updated_at": 10,
            "last_message_at": 10,
        },
    ]


def test_sessions_api_enriches_only_returned_rows_by_default(monkeypatch):
    all_sessions_kwargs = []
    enriched_batches = []

    def fake_all_sessions(**kwargs):
        all_sessions_kwargs.append(kwargs)
        return _sessions_payload_rows()

    def fake_enrich(rows):
        enriched_batches.append([row["session_id"] for row in rows])
        for row in rows:
            row["_lineage_root_id"] = row["session_id"]

    monkeypatch.setattr(routes, "all_sessions", fake_all_sessions)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", fake_enrich)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda rows: False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    routes._session_list_cache_clear()

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/sessions"))

    assert handler.status == 200
    body = handler.json_body()
    assert [row["session_id"] for row in body["sessions"]] == ["visible-active"]
    assert body["archived_count"] == 1
    assert body["archived_webui_count"] == 1
    assert body["include_archived"] is False
    assert enriched_batches == [["visible-active"]]
    assert all_sessions_kwargs[0]["include_lineage_metadata"] is False


def test_sessions_api_fetches_archived_rows_only_when_requested(monkeypatch):
    enriched_batches = []

    monkeypatch.setattr(routes, "all_sessions", lambda **_kwargs: _sessions_payload_rows())
    monkeypatch.setattr(
        routes,
        "_enrich_sidebar_lineage_metadata",
        lambda rows: enriched_batches.append([row["session_id"] for row in rows]),
    )
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda rows: False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    routes._session_list_cache_clear()

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/sessions?include_archived=1"))

    assert handler.status == 200
    body = handler.json_body()
    assert [row["session_id"] for row in body["sessions"]] == [
        "visible-active",
        "archived-history",
    ]
    assert body["archived_count"] == 1
    assert body["include_archived"] is True
    assert enriched_batches == [["visible-active", "archived-history"]]


def test_sessions_api_legacy_all_sessions_monkeypatch_fallback_is_narrow(monkeypatch):
    calls = []

    def legacy_all_sessions(*, diag=None):
        calls.append(diag)
        return _sessions_payload_rows()

    monkeypatch.setattr(routes, "all_sessions", legacy_all_sessions)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda rows: None)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda rows: False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/sessions"))

    assert handler.status == 200
    assert len(calls) == 1


def test_sessions_api_internal_typeerror_is_not_hidden_by_legacy_fallback(monkeypatch):
    def broken_all_sessions(**_kwargs):
        raise TypeError("internal include_lineage_metadata transformation failed")

    monkeypatch.setattr(routes, "all_sessions", broken_all_sessions)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda rows: False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    with pytest.raises(TypeError, match="internal include_lineage_metadata"):
        routes._build_session_list_cache_payload(
            active_profile="default",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
        )


def test_session_list_fetch_adds_include_archived_only_when_toggle_is_on():
    src = (pathlib.Path(__file__).parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "if(_showArchived) qs.set('include_archived','1');" in src
    assert "api('/api/sessions' + sessionListQS" in src
    assert "toggle.onclick=()=>{_showArchived=!_showArchived;renderSessionList();};" in src
    assert "_archivedWebuiCount" in src
    assert "sessData.archived_webui_count ?? sessData.archived_count ?? 0" in src
    assert "archived_webui_count" in src
