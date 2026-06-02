from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_active_empty_session_is_injected_into_sidebar_rows():
    assert "function _sessionRowsWithActiveEphemeralSession(rows)" in SESSIONS_JS
    helper_start = SESSIONS_JS.index("function _sessionRowsWithActiveEphemeralSession(rows)")
    helper_end = SESSIONS_JS.index("function renderSessionListFromCache()", helper_start)
    helper = SESSIONS_JS[helper_start:helper_end]

    assert "S.session" in helper
    assert "message_count:0" in helper
    assert "title:S.session.title||'New Chat'" in helper
    assert "rows.some(s=>s&&s.session_id===sid)" in helper


def test_new_session_switches_sidebar_back_to_webui_source():
    new_session = SESSIONS_JS[SESSIONS_JS.index("async function newSession"):SESSIONS_JS.index("async function loadSession")]
    assert "if(_sessionSourceFilter==='cli') _sessionSourceFilter='webui';" in new_session


def test_sidebar_search_uses_active_ephemeral_rows_before_filtering():
    render_start = SESSIONS_JS.index("function renderSessionListFromCache()")
    render_end = SESSIONS_JS.index("function _showProjectPicker", render_start)
    render_body = SESSIONS_JS[render_start:render_end]

    assert "const sidebarRows=_sessionRowsWithActiveEphemeralSession(_allSessions);" in render_body
    assert "const searchMatches=_sessionSearchMergeMatches(sidebarRows,searchQueryRaw,_contentSearchResults);" in render_body
    assert "const allMatched=_ensureActiveSessionRowPresent(searchMatches,sidebarRows);" in render_body
