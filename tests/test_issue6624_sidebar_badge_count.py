"""Regression coverage for issue #6624: sidebar count badge vs session list.

The badge count and the sidebar list must never diverge. The fix adds a
bounded ``cli_visible_session_limit`` setting (default 20, range 20-100) that
is threaded through ``get_cli_sessions()`` into ``_load_cli_sessions_uncached``
so the imported CLI/TUI/Desktop rows the sidebar carries obey the SAME budget
as the badge count. The value is resolved once per request, shared with the
initial gateway SSE snapshot, and participates in the session-list cache key so
a limit change cannot serve a stale payload.
"""

from __future__ import annotations

import io
import json
from urllib.parse import urlparse

import api.profiles as profiles
import api.routes as routes
import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
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


def _session_rows(
    webui_count,
    cli_count,
    archived_webui_count=0,
    archived_cli_count=0,
    start=0,
):
    rows = []
    for index in range(webui_count):
        rows.append(
            {
                "session_id": f"webui-{start + index}",
                "title": "WebUI Session",
                "profile": "default",
                "archived": index < archived_webui_count,
                "message_count": 1,
                "updated_at": 1000 + index,
                "last_message_at": 1000 + index,
                "source": "webui",
                "raw_source": "webui",
                "session_source": "webui",
                "source_tag": "webui",
            }
        )
    for index in range(cli_count):
        rows.append(
            {
                "session_id": f"cli-{start + index + 10000}",
                "title": "Imported CLI session",
                "profile": "default",
                "archived": index < archived_cli_count,
                "message_count": 1,
                "updated_at": 2000 + index,
                "last_message_at": 2000 + index,
                "source": "cli",
                "raw_source": "cli",
                "session_source": "cli",
                "source_tag": "cli",
            }
        )
    return rows


def _install_common_monkeypatches(monkeypatch, rows, settings=None):
    enriched = []
    row_ids = {str(row["session_id"]) for row in rows if row.get("session_id")}
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda rows: enriched.append([r["session_id"] for r in rows]))
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False, include_claude_code=True, visible_session_limit=None: [])
    monkeypatch.setattr(routes, "agent_session_rows_existing", lambda ids, profile=None: set(row_ids & {str(sid) for sid in ids}))
    effective = {"show_cli_sessions": True}
    if settings:
        effective.update(settings)
    monkeypatch.setattr(routes, "load_settings", lambda: effective)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    return enriched


def _handle_sessions(url, handler=None):
    if handler is None:
        handler = _FakeHandler()
    routes.handle_get(handler, urlparse(url))
    return handler


def test_cli_visible_session_limit_threads_to_get_cli_sessions(monkeypatch):
    """#6624: the configured cli_visible_session_limit must reach
    get_cli_sessions so the imported CLI rows obey the same budget as the
    badge count."""
    captured = {}

    def fake_get_cli_sessions(source_filter=None, all_profiles=False, include_claude_code=True, visible_session_limit=None):
        captured["visible_session_limit"] = visible_session_limit
        return []

    rows = _session_rows(webui_count=2, cli_count=25)
    _install_common_monkeypatches(monkeypatch, rows)
    monkeypatch.setattr(routes, "get_cli_sessions", fake_get_cli_sessions)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=cli")
    assert handler.status == 200
    assert captured["visible_session_limit"] == 20  # default

    # Configured value flows through.
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True, "cli_visible_session_limit": 50})
    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=cli")
    assert handler.status == 200
    assert captured["visible_session_limit"] == 50


def test_cli_visible_session_limit_threads_legacy_signature(monkeypatch):
    """#6624: a get_cli_sessions monkeypatch without visible_session_limit
    (legacy test double) must still be called without the new kwarg."""
    captured = {}

    def fake_get_cli_sessions(source_filter=None, all_profiles=False):
        captured["kwargs"] = (source_filter, all_profiles)
        return []

    rows = _session_rows(webui_count=2, cli_count=3)
    _install_common_monkeypatches(monkeypatch, rows)
    monkeypatch.setattr(routes, "get_cli_sessions", fake_get_cli_sessions)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=cli")
    assert handler.status == 200
    assert captured["kwargs"] == (None, False)


def test_cli_visible_session_limit_normalization(monkeypatch):
    """#6624: out-of-range stored values are clamped to the 20-100 window."""
    assert routes._resolve_cli_visible_session_limit({}) == 20
    assert routes._resolve_cli_visible_session_limit({"cli_visible_session_limit": 5}) == 20
    assert routes._resolve_cli_visible_session_limit({"cli_visible_session_limit": 500}) == 100
    assert routes._resolve_cli_visible_session_limit({"cli_visible_session_limit": "abc"}) == 20
    assert routes._resolve_cli_visible_session_limit({"cli_visible_session_limit": 50}) == 50


def test_session_list_cache_key_includes_cli_visible_limit(monkeypatch):
    """#6624: the cache key must change when cli_visible_session_limit
    changes, otherwise a stale 20-row payload would be served after the user
    raises the limit."""
    rows = _session_rows(webui_count=2, cli_count=30)
    _install_common_monkeypatches(monkeypatch, rows)

    key_default = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        cli_visible_session_limit=None,
    )
    key_50 = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        cli_visible_session_limit=50,
    )
    assert key_default != key_50


def test_cli_visible_session_limit_default_in_settings():
    """#6624: the setting ships with a sane bounded default."""
    from api.config import _SETTINGS_DEFAULTS, _SETTINGS_INT_RANGES

    assert _SETTINGS_DEFAULTS.get("cli_visible_session_limit") == 20
    assert _SETTINGS_INT_RANGES.get("cli_visible_session_limit") == (20, 100)


def test_gateway_sse_snapshot_uses_resolved_limit(monkeypatch):
    """#6624: the initial gateway SSE snapshot must use the same resolved
    limit as the sidebar route so polling and SSE never alternate between
    different windows."""
    captured = {}

    def fake_get_cli_sessions(source_filter=None, all_profiles=False, include_claude_code=True, visible_session_limit=None):
        captured["visible_session_limit"] = visible_session_limit
        return []

    from api import models as models
    monkeypatch.setattr(models, "get_cli_sessions", fake_get_cli_sessions)

    settings = {"show_cli_sessions": True, "cli_visible_session_limit": 75}
    assert routes._resolve_cli_visible_session_limit(settings) == 75
    # The gateway SSE handler resolves from load_settings; verify the resolved
    # value is what get_cli_sessions receives.
    monkeypatch.setattr(routes, "load_settings", lambda: settings)
    resolved = routes._resolve_cli_visible_session_limit(routes.load_settings())
    fake_get_cli_sessions(visible_session_limit=resolved)
    assert captured["visible_session_limit"] == 75
