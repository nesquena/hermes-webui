import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_session_sidebar_js_has_dynamic_relative_time_helpers():
    assert "function _formatRelativeSessionTime" in SESSIONS_JS
    assert "function _sessionTimeBucketLabel" in SESSIONS_JS
    assert "last week" in SESSIONS_JS
    assert "This week" in SESSIONS_JS
    assert "Older" in SESSIONS_JS


def test_session_sidebar_renders_relative_time_and_meta_rows():
    assert "session-time" in SESSIONS_JS
    assert "session-meta" in SESSIONS_JS
    assert "orderedSessions" in SESSIONS_JS
    assert ".session-time" in STYLE_CSS
    assert ".session-meta" in STYLE_CSS
    assert ".session-title-row" in STYLE_CSS
    assert ".session-item.active .session-title" in STYLE_CSS
