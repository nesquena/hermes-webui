"""Regression coverage for issue #2351 CLI session list separation."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
STYLE_CSS = ROOT / "static" / "style.css"


def test_sidebar_has_separate_webui_and_cli_session_source_tabs():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    assert "let _sessionSourceFilter = 'webui'" in src
    assert "hermes-session-source-filter" in src
    assert "session-source-tabs" in src
    assert "WebUI sessions" in src
    assert "CLI sessions" in src
    assert "_sessionSourceFilter==='cli'" in src


def test_cli_filter_keeps_cli_rows_out_of_default_webui_list():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    assert "const webuiSessionCount = withMessages.filter(s=>!_isCliSession(s)).length" in src
    assert "const cliSessionCount = withMessages.filter(s=>_isCliSession(s)).length" in src
    assert "? withMessages.filter(s=>_isCliSession(s))" in src
    assert ": withMessages.filter(s=>!_isCliSession(s))" in src


def test_session_source_tabs_have_dedicated_sidebar_styles():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".session-source-tabs" in css
    assert ".session-source-tab.active" in css
    assert ".session-empty-note" in css


def test_webui_state_db_mirror_does_not_become_cli_sidebar_row():
    from api.routes import _merge_cli_sidebar_metadata

    merged = _merge_cli_sidebar_metadata(
        {"session_id": "webui-tip", "title": "Long WebUI session", "source_tag": "webui"},
        {
            "session_id": "webui-tip",
            "source_tag": "webui",
            "session_source": "webui",
            "message_count": 1740,
        },
    )

    assert merged["is_cli_session"] is False
    assert merged["source_tag"] == "webui"
    assert merged["session_source"] == "webui"
    assert merged["message_count"] == 1740


def test_real_cli_state_db_mirror_stays_cli_sidebar_row():
    from api.routes import _merge_cli_sidebar_metadata

    merged = _merge_cli_sidebar_metadata(
        {"session_id": "cli-tip", "title": "CLI session", "source_tag": "cli"},
        {
            "session_id": "cli-tip",
            "source_tag": "cli",
            "session_source": "cli",
            "message_count": 12,
        },
    )

    assert merged["is_cli_session"] is True
    assert merged["session_source"] == "cli"


def test_stale_webui_sidebar_cli_flag_is_cleared_before_frontend_response():
    from api.routes import _normalize_sidebar_source_flags

    normalized = _normalize_sidebar_source_flags(
        {
            "session_id": "webui-tip",
            "title": "Long WebUI session",
            "source_tag": "webui",
            "session_source": "webui",
            "is_cli_session": True,
            "message_count": 1740,
        }
    )

    assert normalized["is_cli_session"] is False
    assert normalized["source_tag"] == "webui"
    assert normalized["session_source"] == "webui"



def test_webui_source_overrides_stale_cli_flag_even_with_default_title():
    from api.agent_sessions import is_cli_session_row
    from api.routes import _normalize_sidebar_source_flags

    stale_webui = {
        "session_id": "webui-default-title",
        "title": "CLI Session",
        "source_tag": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": True,
        "message_count": 23,
    }

    assert is_cli_session_row(stale_webui) is False
    assert _normalize_sidebar_source_flags(stale_webui)["is_cli_session"] is False


def test_real_cli_sidebar_cli_flag_is_preserved_before_frontend_response():
    from api.routes import _normalize_sidebar_source_flags

    normalized = _normalize_sidebar_source_flags(
        {
            "session_id": "cli-tip",
            "title": "CLI session",
            "source_tag": "cli",
            "session_source": "cli",
            "is_cli_session": True,
            "message_count": 12,
        }
    )

    assert normalized["is_cli_session"] is True
