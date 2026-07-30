"""Regression checks for per-conversation action menu click stability."""
from pathlib import Path

SESSIONS_JS = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    assert brace != -1, f"{name} body not found"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return src[start:i]


def test_session_list_refresh_does_not_close_open_conversation_actions():
    """Sidebar refreshes must not eat the three-dot menu before users can click it."""
    body = _function_block(SESSIONS_JS, "renderSessionListFromCache")

    assert "if(_renamingSid) return;" in body
    assert "if(_sessionActionMenu) return;" in body
    assert body.index("if(_sessionActionMenu) return;") < body.index("closeSessionActionMenu();")


def test_archive_action_repaints_sidebar_before_full_refresh():
    """Archive should hide the row from cached sidebar state before /api/sessions returns."""
    menu_body = _function_block(SESSIONS_JS, "_openSessionActionMenu")
    helper_body = _function_block(SESSIONS_JS, "_archiveSession")

    api_call = "const response=await api('/api/session/archive'"
    optimistic = "if(cached) cached.archived=archived;"
    cached_render = "renderSessionListFromCache();"
    full_refresh = "void renderSessionList();"

    assert "await _archiveSession(session,!session.archived);" in menu_body
    assert optimistic in helper_body
    assert helper_body.index(api_call) < helper_body.index(optimistic) < helper_body.index(cached_render) < helper_body.index(full_refresh)


def test_archive_action_clears_saved_session_pointer_for_archived_current_session():
    """Archiving the saved active session should not leave boot localStorage stale."""
    helper_body = _function_block(SESSIONS_JS, "_archiveSession")
    stale_saved_pointer_guard = (
        "try{ if(archived&&session.session_id&&localStorage.getItem('hermes-webui-session')===session.session_id) "
        "localStorage.removeItem('hermes-webui-session'); }catch(_){ }"
    )

    assert stale_saved_pointer_guard in helper_body
    assert helper_body.index("if(S.session&&S.session.session_id===session.session_id) S.session.archived=archived;") < helper_body.index(stale_saved_pointer_guard)
    assert helper_body.index(stale_saved_pointer_guard) < helper_body.index("showToast(session.archived?_sessionArchiveToast(response,session):t('session_restored'));")


def test_delete_action_repaints_sidebar_before_loading_remaining_sessions():
    """Delete should remove the row locally before loading replacement session data."""
    body = _function_block(SESSIONS_JS, "deleteSession")

    api_call = "const deleteRequest=api('/api/session/delete'"
    optimistic = "_optimisticallyRemoveSessionFromList(sid);"
    remaining_fetch = "const remaining=await api('/api/sessions'+_sessionListQueryString());"
    full_refresh = "await renderSessionList();"

    assert optimistic in body
    assert body.index(api_call) < body.index(optimistic) < body.index(full_refresh)
    assert body.index(optimistic) < body.index(remaining_fetch)

def test_batch_delete_remaining_session_fetch_uses_sidebar_query_string():
    """Batch delete must reload the default sidebar through the same query builder."""
    assert SESSIONS_JS.count("const remaining=await api('/api/sessions'+_sessionListQueryString());") >= 2
