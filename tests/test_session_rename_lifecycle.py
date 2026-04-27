"""Structural regressions for sidebar session inline rename (#1153).

The first Enter after double-click rename could appear to revert because the
frontend cleared `_renamingSid` before `/api/session/rename` completed. That
let normal session-list refreshes re-render stale cached data and destroy the
input while the save was still in flight.
"""

import pathlib


REPO = pathlib.Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _session_rename_block():
    start = SESSIONS_JS.find("const startRename=()=>{")
    assert start >= 0, "session inline rename startRename() block not found"
    end = SESSIONS_JS.find("// (Project dot is appended above", start)
    assert end > start, "session inline rename block end marker not found"
    return SESSIONS_JS[start:end]


def test_session_rename_finish_is_idempotent():
    block = _session_rename_block()
    assert "let finishDone=false;" in block, (
        "session rename finish() must track completion so Enter, blur, Escape, "
        "and delayed pointer paths cannot complete twice"
    )
    guard_pos = block.find("if(finishDone) return;")
    set_pos = block.find("finishDone=true;")
    assert guard_pos >= 0 and set_pos > guard_pos, (
        "session rename finish() must return early after the first completion"
    )


def test_session_rename_guard_releases_after_save_path_completes():
    block = _session_rename_block()
    assert block.count("_renamingSid = null;") == 1, (
        "_renamingSid must be cleared from one release helper only, not at the "
        "top of finish() before the async rename save settles"
    )
    release_pos = block.find("const releaseRename=()=>{")
    clear_pos = block.find("_renamingSid = null;")
    assert release_pos >= 0 and clear_pos > release_pos, (
        "_renamingSid should be cleared inside releaseRename(), after the "
        "selected finish path has completed"
    )
    api_pos = block.find("await api('/api/session/rename'")
    finally_pos = block.find("finally{")
    release_call_pos = block.find("releaseRename();", finally_pos)
    assert api_pos >= 0 and finally_pos > api_pos and release_call_pos > finally_pos, (
        "save path must await /api/session/rename before releaseRename() clears "
        "the render guard"
    )


def test_session_rename_success_updates_cache_and_active_session_title():
    block = _session_rename_block()
    assert "_allSessions.find(item=>item&&item.session_id===s.session_id)" in block
    assert "if(cached) cached.title=nextTitle;" in block
    assert "S.session.title=nextTitle;syncTopbar();" in block, (
        "successful session rename must keep cached and active titles coherent"
    )


def test_session_rename_failure_restores_state_and_surfaces_error():
    block = _session_rename_block()
    catch_pos = block.find("}catch(err){")
    assert catch_pos >= 0, "session rename save path must handle API failures"
    catch_block = block[catch_pos:block.find("}finally{", catch_pos)]
    assert "applyTitle(oldTitle,false);" in catch_block, (
        "failed session rename must restore the cached/active title instead of "
        "silently appearing successful"
    )
    assert "setStatus(msg);" in catch_block
    assert "showToast(msg,3000,'error')" in catch_block
