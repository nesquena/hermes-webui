"""Phase 3 (#4662): sidebar session-list serialization must read the redaction
setting ONCE per response, not once per row. Regression guard for the per-row
settings.json reload that dominated /api/sessions response_write on large lists.
"""
import json
import api.routes as routes


def test_sidebar_payload_reads_redaction_setting_once(monkeypatch):
    calls = {"n": 0}

    def _counting_load_settings():
        calls["n"] += 1
        return {"api_redact_enabled": True}

    # _redact_text() falls back to load_settings() only when _enabled is None,
    # so a per-row read shows up as a load_settings() call per row. After the
    # fix the caller reads it once and threads redact_enabled to every row.
    monkeypatch.setattr("api.config.load_settings", _counting_load_settings)
    monkeypatch.setattr("api.helpers.load_settings", _counting_load_settings, raising=False)

    payload = {
        "sessions": [
            {"session_id": f"s{i}", "title": f"title {i}", "preview": f"preview {i}"}
            for i in range(12)
        ],
        "cli_count": 0,
    }
    # Pass rows straight through — avoid runtime-overlay noise from the cache layer.
    monkeypatch.setattr(routes, "_session_list_cache_overlay_runtime_rows", lambda rows: rows)

    routes._session_list_payload_to_response(payload)

    # Before the fix: ~1 read per row (>=12). After: exactly 1 for the whole response.
    assert calls["n"] <= 1, f"settings read {calls['n']}x; expected <=1 (read-once per response)"


def test_sidebar_payload_still_redacts_titles(monkeypatch):
    """The read-once optimization must not disable redaction: a title that looks
    like a credential is still redacted when api_redact_enabled is True."""
    monkeypatch.setattr("api.config.load_settings", lambda: {"api_redact_enabled": True})
    monkeypatch.setattr("api.helpers.load_settings", lambda: {"api_redact_enabled": True}, raising=False)
    monkeypatch.setattr(routes, "_session_list_cache_overlay_runtime_rows", lambda rows: rows)

    secret = "sk-ant-api03-" + ("A" * 40)
    payload = {"sessions": [{"session_id": "s1", "title": f"key {secret}"}], "cli_count": 0}
    resp = routes._session_list_payload_to_response(payload)
    title = resp["sessions"][0]["title"]
    assert secret not in title, f"credential leaked into sidebar title: {title!r}"


def test_sidebar_payload_no_redaction_when_disabled(monkeypatch):
    """When api_redact_enabled is False, titles pass through unchanged (and we
    still only read the setting once)."""
    calls = {"n": 0}

    def _load():
        calls["n"] += 1
        return {"api_redact_enabled": False}

    monkeypatch.setattr("api.config.load_settings", _load)
    monkeypatch.setattr("api.helpers.load_settings", _load, raising=False)
    monkeypatch.setattr(routes, "_session_list_cache_overlay_runtime_rows", lambda rows: rows)

    payload = {"sessions": [{"session_id": "s1", "title": "plain title"}], "cli_count": 0}
    resp = routes._session_list_payload_to_response(payload)
    assert resp["sessions"][0]["title"] == "plain title"
    assert calls["n"] <= 1, f"settings read {calls['n']}x with redaction disabled; expected <=1"


def test_sidebar_payload_redacts_display_and_parent_titles(monkeypatch):
    """#6056 derives a delegated subagent's display_title from raw user-message
    content, so display_title / _state_db_title / parent_title must go through the
    SAME redaction as title — a credential in a delegated goal must not leak to
    the sidebar even though only `title` was redacted before."""
    monkeypatch.setattr("api.config.load_settings", lambda: {"api_redact_enabled": True})
    monkeypatch.setattr("api.helpers.load_settings", lambda: {"api_redact_enabled": True}, raising=False)
    monkeypatch.setattr(routes, "_session_list_cache_overlay_runtime_rows", lambda rows: rows)

    secret = "sk-" + ("A" * 44)
    payload = {"sessions": [{
        "session_id": "s1",
        "title": "clean",
        "display_title": f"debug {secret}",
        "_state_db_title": f"goal {secret}",
        "parent_title": f"parent {secret}",
    }], "cli_count": 0}
    resp = routes._session_list_payload_to_response(payload)
    row = resp["sessions"][0]
    for field in ("display_title", "_state_db_title", "parent_title"):
        assert secret not in str(row.get(field, "")), (
            f"credential leaked into sidebar {field}: {row.get(field)!r}"
        )


def test_redact_sidebar_title_fields_helper():
    """The shared helper redacts every user-content-derived title field, honors the
    enabled flag, and never raises on missing/non-str fields."""
    secret = "sk-" + ("B" * 44)
    item = {
        "title": "kept-by-caller",
        "display_title": f"a {secret}",
        "_state_db_title": f"b {secret}",
        "parent_title": f"c {secret}",
        "session_id": "s1",
    }
    routes._redact_sidebar_title_fields(item, True)
    for field in ("display_title", "_state_db_title", "parent_title"):
        assert secret not in item[field], f"{field} not redacted"
    # Disabled → pass through unchanged.
    item2 = {"display_title": f"a {secret}"}
    routes._redact_sidebar_title_fields(item2, False)
    assert item2["display_title"] == f"a {secret}"
    # Missing / non-str fields must not raise.
    routes._redact_sidebar_title_fields({"display_title": None, "parent_title": 42}, True)


def test_sessions_search_branches_redact_derived_titles():
    """Every /api/sessions/search response branch must redact the derived
    title fields so search rows can't leak a derived display_title the sidebar
    hides (#6056).

    Behavioral, not source-shaped: the original version counted literal
    `_redact_sidebar_title_fields(item` occurrences in the handler source. That
    oracle broke the moment #6985 round 6 routed all three branches through the
    single bounded `_sidebar_session_response_item` serializer (which redacts
    internally) — even though redaction was strictly preserved. Asserting the
    response bodies instead keeps the same guarantee (add a 4th unredacted
    branch and this still fails) without coupling it to one call shape.
    """
    import inspect
    from types import SimpleNamespace
    from unittest.mock import patch
    from urllib.parse import urlparse

    secret = "sk-" + ("C" * 44)
    row = {
        "session_id": "leaky",
        "title": "needle title",
        "display_title": f"d {secret}",
        "_state_db_title": f"s {secret}",
        "parent_title": f"p {secret}",
        "profile": "default",
        "message_count": 1,
    }

    def _run(url, *, scan_text=""):
        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload

        with patch.object(routes, "all_sessions", return_value=[dict(row)]), \
             patch.object(
                 routes, "get_session_for_scan",
                 side_effect=lambda sid: SimpleNamespace(
                     messages=[{"role": "user", "content": scan_text}]
                 ),
             ), \
             patch("api.profiles.get_active_profile_name", return_value="default"), \
             patch.object(routes, "load_settings", return_value={"api_redact_enabled": True}), \
             patch.object(routes, "j", side_effect=fake_j):
            routes._handle_sessions_search(SimpleNamespace(), urlparse(url))
        return captured["payload"]["sessions"]

    branches = {
        "empty-query": _run("/api/sessions/search"),
        "title-match": _run("/api/sessions/search?q=needle"),
        "content-match": _run(
            "/api/sessions/search?q=haystack", scan_text="a haystack here",
        ),
    }
    for label, rows in branches.items():
        assert rows, f"{label} branch returned no row to assert on"
        blob = json.dumps(rows)
        assert secret not in blob, (
            f"the /api/sessions/search {label} branch leaked an unredacted "
            "derived title field (display_title/_state_db_title/parent_title)"
        )

    # And it must still read the setting once, not per-_redact_text call.
    assert "_search_redact_enabled" in inspect.getsource(routes._handle_sessions_search)
