"""
Test that non-WebUI (messaging) sessions from state.db appear in the session
list when ``show_cli_sessions=True``, even when ``sidebar_source=webui``.

Regression test for #6542: Non-WebUI sessions (Discord, Telegram, etc.) were
silently excluded from the session list even with the preference enabled.
"""
import json
from urllib.parse import urlparse
from unittest.mock import patch

import pytest

from api import routes
from api import profiles


class _FakeHandler:
    def __init__(self):
        self.status = 200
        self.headers = {}
        self.body = b""
        self.client_address = ("127.0.0.1", 0)
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data

    def json_body(self):
        return json.loads(self.body)

    def flush(self):
        pass


def test_messaging_sessions_appear_with_show_cli_sessions_enabled(monkeypatch):
    """Verify that messaging (Telegram/Discord) sessions appear in the session
    list when ``show_cli_sessions=True`` and the user is on the default sidebar
    source tab (``sidebar_source=webui``).
    """
    # ── Mock the core dependencies ──────────────────────────────────────
    # Return ONE WebUI-native session and ONE Telegram session (from state.db).
    webui_rows = [
        {
            "session_id": "webui-native-1",
            "title": "My WebUI Chat",
            "profile": "default",
            "archived": False,
            "message_count": 3,
            "updated_at": 100,
            "last_message_at": 100,
        },
    ]

    def fake_all_sessions(**kwargs):
        return list(webui_rows)

    cli_rows = [
        {
            "session_id": "telegram-session-1",
            "title": "Telegram Discussion",
            "profile": "default",
            "archived": False,
            "message_count": 5,
            "updated_at": 90,
            "last_message_at": 90,
            "source_tag": "telegram",
            "raw_source": "telegram",
            "session_source": "messaging",
            "source_label": "Telegram",
            "is_cli_session": False,
            "user_message_count": 3,
            "parent_session_id": None,
            "end_reason": None,
        },
        {
            "session_id": "discord-chat-1",
            "title": "Discord General",
            "profile": "default",
            "archived": False,
            "message_count": 8,
            "updated_at": 80,
            "last_message_at": 80,
            "source_tag": "discord",
            "raw_source": "discord",
            "session_source": "messaging",
            "source_label": "Discord",
            "is_cli_session": False,
            "user_message_count": 4,
            "parent_session_id": None,
            "end_reason": None,
        },
    ]

    monkeypatch.setattr(routes, "all_sessions", fake_all_sessions)
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda rows: False,
    )
    monkeypatch.setattr(
        routes,
        "_enrich_sidebar_lineage_metadata",
        lambda rows: None,
    )
    monkeypatch.setattr(
        routes,
        "_session_attention_summary",
        lambda session_id: None,
    )

    # Return Telegram + Discord sessions from get_cli_sessions.
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda source_filter=None, all_profiles=False, include_claude_code=True: list(
            cli_rows
        ),
    )
    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing",
        lambda ids, profile=None: set(ids),
    )
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    routes._session_list_cache_clear()

    # ── Request the WebUI-tab session list ─────────────────────────────
    handler = _FakeHandler()
    routes.handle_get(
        handler,
        urlparse("http://example.com/api/sessions?sidebar_source=webui&exclude_hidden=1"),
    )

    assert handler.status == 200
    body = handler.json_body()
    session_ids = {row["session_id"] for row in body["sessions"]}

    # The WebUI-native session MUST be present.
    assert "webui-native-1" in session_ids, (
        "WebUI-native session should always be in the list"
    )

    # The Telegram and Discord sessions MUST be present — this is the bug
    # reported in #6542.
    assert "telegram-session-1" in session_ids, (
        "Telegram session must appear when show_cli_sessions=True "
        "and sidebar_source='webui' (regression #6542)"
    )
    assert "discord-chat-1" in session_ids, (
        "Discord session must appear when show_cli_sessions=True "
        "and sidebar_source='webui' (regression #6542)"
    )

    # Verify source metadata is preserved in the response.
    for row in body["sessions"]:
        if row["session_id"] == "telegram-session-1":
            assert row.get("session_source") == "messaging"
            assert row.get("source_tag") == "telegram"
        if row["session_id"] == "discord-chat-1":
            assert row.get("session_source") == "messaging"
            assert row.get("source_tag") == "discord"

    # Verify the tab counts include messaging sessions as non-CLI.
    assert body.get("webui_session_count", 0) >= 2, (
        "webui_session_count should count messaging sessions as non-CLI"
    )


def test_messaging_sessions_excluded_when_show_cli_sessions_disabled(monkeypatch):
    """When ``show_cli_sessions=False``, messaging sessions from state.db
    should NOT appear (they come through the CLI bridge).
    """
    monkeypatch.setattr(routes, "all_sessions", lambda **kwargs: [
        {"session_id": "webui-1", "title": "WebUI", "profile": "default",
         "message_count": 1, "updated_at": 100, "last_message_at": 100},
    ])
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda rows: False,
    )
    monkeypatch.setattr(
        routes,
        "_enrich_sidebar_lineage_metadata",
        lambda rows: None,
    )
    monkeypatch.setattr(
        routes,
        "_session_attention_summary",
        lambda session_id: None,
    )
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda source_filter=None, all_profiles=False, include_claude_code=True: [
            {
                "session_id": "telegram-hidden",
                "title": "Hidden Telegram",
                "profile": "default",
                "message_count": 5, "updated_at": 90, "last_message_at": 90,
                "source_tag": "telegram", "raw_source": "telegram",
                "session_source": "messaging", "source_label": "Telegram",
                "is_cli_session": False,
            },
        ],
    )
    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing",
        lambda ids, profile=None: set(ids),
    )
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    routes._session_list_cache_clear()

    handler = _FakeHandler()
    routes.handle_get(
        handler,
        urlparse("http://example.com/api/sessions?sidebar_source=webui"),
    )

    assert handler.status == 200
    body = handler.json_body()
    session_ids = {row["session_id"] for row in body["sessions"]}

    assert "telegram-hidden" not in session_ids, (
        "Telegram session must NOT appear when show_cli_sessions=False"
    )
