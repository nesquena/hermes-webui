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
