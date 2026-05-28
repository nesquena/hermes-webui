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
    body = _function_block(SESSIONS_JS, "_openSessionActionMenu")

    api_call = "const response=await api('/api/session/archive'"
    optimistic = "_optimisticallyArchiveSessionInList(session.session_id,!session.archived);"
    full_refresh = "void renderSessionList();"

    assert optimistic in body
    assert body.index(api_call) < body.index(optimistic) < body.index(full_refresh)


def test_delete_action_repaints_sidebar_before_loading_remaining_sessions():
    """Delete should remove the row locally before loading replacement session data."""
    body = _function_block(SESSIONS_JS, "deleteSession")

    api_call = "response=await api('/api/session/delete'"
    optimistic = "_optimisticallyRemoveSessionFromList(sid);"
    remaining_fetch = "const remaining=await api('/api/sessions');"
    full_refresh = "void renderSessionList();"

    assert optimistic in body
    assert body.index(api_call) < body.index(optimistic) < body.index(full_refresh)
    assert body.index(optimistic) < body.index(remaining_fetch)
