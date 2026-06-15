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
    assert "function _partitionSidebarSessionRows(allMatched, activeSidForSidebar)" in src
    assert "cliSessionCount" in src
    assert "const showCliOnly=_sessionSourceFilter==='cli';" in src
    assert "const webuiProfileFiltered=[];" in src
    assert "const cliProfileFiltered=[];" in src
    assert "const webuiSessionsRaw=[];" in src
    assert "const cliSessionsRaw=[];" in src
    assert "profileFiltered: showCliOnly ? cliProfileFiltered : webuiProfileFiltered," in src
    assert "sessionsRaw: showCliOnly ? cliSessionsRaw : webuiSessionsRaw," in src


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


def test_tui_state_db_rows_are_cli_sidebar_rows():
    """Hermes TUI state.db rows belong in the CLI/agent sidebar bucket.

    TUI sessions are projected from state.db with raw/source_tag='tui'. If they
    stay session_source='other' and is_cli_session=false, the two-tab sidebar
    partition can make active TUI continuations disappear from both the WebUI
    and CLI views.
    """
    from api.agent_sessions import is_cli_session_row, normalize_agent_session_source
    from api.routes import _normalize_sidebar_source_flags

    normalized_source = normalize_agent_session_source("tui")
    assert normalized_source["session_source"] == "cli"
    assert normalized_source["source_label"] == "TUI"

    tui_row = {
        "session_id": "tui-tip",
        "title": "Podcast work #17",
        "source_tag": "tui",
        "raw_source": "tui",
        "session_source": "other",
        "source_label": "Tui",
        "message_count": 281,
    }

    assert is_cli_session_row(tui_row) is True
    assert _normalize_sidebar_source_flags(tui_row)["is_cli_session"] is True


def test_tui_continuation_projection_uses_latest_tip_title():
    """TUI continuation rows should surface under the latest segment title."""
    from api.agent_sessions import _project_agent_session_rows

    rows = [
        {
            "id": "tui_parent",
            "source": "tui",
            "title": "Podcast work #6",
            "started_at": 100.0,
            "last_activity": 150.0,
            "message_count": 10,
            "actual_message_count": 10,
            "actual_user_message_count": 5,
            "parent_session_id": None,
            "ended_at": 199.0,
            "end_reason": "cli_close",
        },
        {
            "id": "tui_tip",
            "source": "tui",
            "title": "Podcast work #17",
            "started_at": 200.0,
            "last_activity": 250.0,
            "message_count": 8,
            "actual_message_count": 8,
            "actual_user_message_count": 4,
            "parent_session_id": "tui_parent",
            "ended_at": None,
            "end_reason": None,
        },
    ]

    projected = _project_agent_session_rows(rows)

    assert len(projected) == 1
    assert projected[0]["id"] == "tui_tip"
    assert projected[0]["title"] == "Podcast work #17"
    assert projected[0]["_lineage_root_id"] == "tui_parent"
    assert projected[0]["_lineage_tip_id"] == "tui_tip"
