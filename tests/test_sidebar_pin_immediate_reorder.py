"""Regression coverage for immediate sidebar pin/unpin reordering."""
from pathlib import Path


SESSIONS_JS = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(encoding="utf-8")


def test_pin_action_updates_local_cache_and_renders_before_refetch():
    """Pin/unpin should move rows immediately, not only after a browser refresh."""
    api_idx = SESSIONS_JS.find("await api('/api/session/pin'")
    assert api_idx != -1, "pin action API call not found"
    # #6426: touch-cooldown comments above the forced cache render pushed the
    # calls just past the old 700-char window.
    snippet = SESSIONS_JS[api_idx:api_idx + 800]

    cached_idx = snippet.find("const cached=(_allSessions||[]).find(s=>s&&s.session_id===session.session_id);")
    update_idx = snippet.find("if(cached) cached.pinned=newPinned;")
    active_idx = snippet.find("if(S.session&&S.session.session_id===session.session_id) S.session.pinned=newPinned;")
    # #6426: the cache render passes {force:true} so a touch-cooldown cannot
    # defer the immediate pin/unpin reorder this test protects.
    cache_render_idx = snippet.find("renderSessionListFromCache({force:true});")
    refetch_idx = snippet.find("void renderSessionList();")

    assert cached_idx != -1, "pin action must find the cached sidebar row"
    assert update_idx != -1, "pin action must update cached row.pinned"
    assert active_idx != -1, "pin action must update the active session if pinned"
    assert cache_render_idx != -1, "pin action must render from cache immediately"
    assert refetch_idx != -1, "pin action should still reconcile with the server"
    assert cached_idx < update_idx < active_idx < cache_render_idx < refetch_idx
