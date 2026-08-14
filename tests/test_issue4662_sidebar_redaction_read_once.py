"""Phase 3 (#4662): sidebar session-list serialization must read the redaction
setting ONCE per response, not once per row. Regression guard for the per-row
settings.json reload that dominated /api/sessions response_write on large lists.
"""
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
    """Every /api/sessions/search response branch must call the shared title-field
    redactor so search rows can't leak a derived display_title the sidebar hides.
    Source-guard: the #6056 class-fix is easy to regress by adding a 4th branch."""
    import inspect
    src = inspect.getsource(routes._handle_sessions_search)
    # 3 response branches (empty-query list, title-match, content-match) each build
    # an `item` and must pass it through _redact_sidebar_title_fields.
    assert src.count("_redact_sidebar_title_fields(item") >= 3, (
        "a /api/sessions/search branch builds a row without redacting the derived "
        "title fields (display_title/_state_db_title/parent_title)"
    )
    # And it must read the setting once, not per-_redact_text call.
    assert "_search_redact_enabled" in src
