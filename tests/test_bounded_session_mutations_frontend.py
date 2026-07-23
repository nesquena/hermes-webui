from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_block(source, declaration, next_declaration):
    start = source.index(declaration)
    end = source.index(next_declaration, start + len(declaration))
    return source[start:end]


def test_undo_and_retry_use_bounded_session_reload():
    retry = _function_block(COMMANDS_JS, "async function cmdRetry()", "async function cmdUndo()")
    undo = _function_block(COMMANDS_JS, "async function cmdUndo()", "async function undoLastExchange()")

    assert "await loadSession(activeSid,{force:true,keepStaleUntilLoaded:true,externalRefreshReason:'retry',skipExtHooks:true});" in retry
    assert "await loadSession(activeSid,{force:true,keepStaleUntilLoaded:true,externalRefreshReason:'undo',skipExtHooks:true});" in undo
    assert "await send();" in retry

    for block in (retry, undo):
        assert "api('/api/session?session_id='+encodeURIComponent(activeSid))" not in block
        assert "S.messages=data.session.messages||[]" not in block
        assert "clearLiveToolCards" not in block
        assert "renderMessages()" not in block
        assert "_messagesTruncated=false" not in block


def test_older_message_window_refreshes_canonical_count_and_topbar():
    fn = _function_block(
        SESSIONS_JS,
        "async function _loadOlderMessages()",
        "// Ensure the full message history is loaded",
    )

    assert "const serverMessageCount = Number(responseSession.message_count);" in fn
    assert "S.session.message_count = serverMessageCount;" in fn
    assert "if (typeof syncTopbar === 'function') syncTopbar();" in fn

    # Both the normal prepend and the already-at-the-end path must refresh the
    # header; otherwise it remains stuck at the initial 30-message window.
    sync = "if (typeof syncTopbar === 'function') syncTopbar();"
    assert fn.count(sync) == 2
    no_older_idx = fn.index("if (!olderMsgs.length)")
    assert no_older_idx < fn.index(sync, no_older_idx) < fn.index("return;", no_older_idx)
    assert fn.index("renderMessages({ preserveScroll: true });") < fn.rindex(sync)


def test_duplicate_same_session_force_reload_is_coalesced():
    assert "const activeLoad=_activeSessionLoad;" in SESSIONS_JS
    assert "_queueSessionLoadAfterActive(sid,opts,activeLoad)" in SESSIONS_JS
    assert "return activeLoad.promise;" in SESSIONS_JS
    assert "if(sameSessionForceReload&&_loadingSessionId===sid) return;" not in SESSIONS_JS
