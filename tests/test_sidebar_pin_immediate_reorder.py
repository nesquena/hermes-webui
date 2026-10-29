"""Regression coverage for immediate sidebar pin/unpin reordering."""
from pathlib import Path


SESSIONS_JS = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(encoding="utf-8")


def test_pin_action_updates_local_cache_and_renders_before_refetch():
    """Pin/unpin should move rows immediately, not only after a browser refresh."""
    api_idx = SESSIONS_JS.find("await api('/api/session/pin'")
    assert api_idx != -1, "pin action API call not found"
    # The completion comments below the pin call grew the block; keep the
    # window generous so the ordering stays observable.
    snippet = SESSIONS_JS[api_idx:api_idx + 1200]

    cached_idx = snippet.find("const cached=(_allSessions||[]).find(s=>s&&s.session_id===session.session_id);")
    update_idx = snippet.find("if(cached) cached.pinned=newPinned;")
    active_idx = snippet.find("if(S.session&&S.session.session_id===session.session_id) S.session.pinned=newPinned;")
    # Pin's response lands after an await, so the cache render must ship
    # immediately through the routed keep-picker repaint (non-churn callers
    # proceed past the picker gate). Ending the touch idle cooldown first
    # keeps a scroll just before the pin from deferring the reorder ~1.25s
    # (#6426); an actively held gesture still defers and replays on release.
    cooldown_idx = snippet.find("_sessionListLastScrollAt=0;")
    helper_idx = snippet.find("_repaintSidebarKeepingProjectPicker();")
    refetch_idx = snippet.find("void renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});")

    assert cached_idx != -1, "pin action must find the cached sidebar row"
    assert update_idx != -1, "pin action must update cached row.pinned"
    assert active_idx != -1, "pin action must update the active session if pinned"
    assert cooldown_idx != -1, "pin action must end the touch idle cooldown before its repaint"
    assert helper_idx != -1, "pin action must repaint through the routed keep-picker render"
    assert refetch_idx != -1, "pin action should still reconcile with the server"
    assert cached_idx < update_idx < active_idx < cooldown_idx < helper_idx < refetch_idx
