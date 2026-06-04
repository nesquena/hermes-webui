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


def test_new_session_uses_set_based_origin_filter():
    # _activeOriginFilters always contains 'webui'; no reset needed on new session
    assert "_activeOriginFilters = new Set(['webui'])" in SESSIONS_JS


def test_sidebar_search_uses_active_ephemeral_rows_before_filtering():
    render_start = SESSIONS_JS.index("function renderSessionListFromCache()")
    render_end = SESSIONS_JS.index("function _showProjectPicker", render_start)
    render_body = SESSIONS_JS[render_start:render_end]

    assert "const sidebarRows=_sessionRowsWithActiveEphemeralSession(_allSessions);" in render_body
    assert "const searchMatches=_sessionSearchMergeMatches(sidebarRows,searchQueryRaw,_contentSearchResults);" in render_body
    assert "const allMatched=_ensureActiveSessionRowPresent(searchMatches,sidebarRows);" in render_body


def test_active_row_reinjection_gated_to_zero_message_ephemeral_only():
    """#3408 review (Codex): _ensureActiveSessionRowPresent must only re-add the
    active FRESHLY-CREATED 0-message chat after search-merge. An active conversation
    that already has messages and was filtered out by the search query must stay
    filtered — re-adding it would pollute unrelated search results with the current
    chat."""
    start = SESSIONS_JS.index("function _ensureActiveSessionRowPresent(rows, sourceRows)")
    end = SESSIONS_JS.index("function clearOptimisticSessionStreaming", start)
    body = SESSIONS_JS[start:end]

    # The reinjection is gated on a 0-message check, not an unconditional prepend.
    assert "Number(activeRow.message_count||0)<=0" in body
    assert "[activeRow,...rows]" in body
    # The unconditional return that shipped in the original PR must be gone.
    assert "return activeRow?[activeRow,...rows]:rows;" not in body
