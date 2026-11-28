from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_sessions_js_resyncs_tool_calls_after_history_window_replacement():
    """History paging replaces S.messages with a larger window.

    Legacy sessions keep tool card data in session.tool_calls, so that side data
    must be refreshed alongside the message window. Otherwise renderMessages()
    can keep stale anchors and show unloaded/thinking placeholders while the
    user scrolls through history.
    """
    assert "function _syncToolCallsForLoadedMessages(messages, sessionToolCalls)" in SESSIONS_JS
    assert "_syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);" in SESSIONS_JS
    # 2026-10-31 re-gate, finding 2: the prepend merges the page's cards into
    # the held ones (owner indices shifted by the prepended length) BEFORE the
    # sync. Handing the sync the raw page list dropped every card the held view
    # owned, and a later focus reload's empty append — whose window carries no
    # cards — could not restore them.
    assert "S.messages = nextMessages;\n" in SESSIONS_JS
    assert (
        "_mergeSessionToolCards(\n"
        "      S.session && S.session.tool_calls,\n"
        "      responseSession.tool_calls,\n"
        "      olderMsgs.length,\n"
        "      0\n"
        "    );" in SESSIONS_JS
    )
    assert "_syncToolCallsForLoadedMessages(nextMessages, _prependedToolCalls);" in SESSIONS_JS
    # The full-document path assigns the window, resets the cursor, and syncs
    # the session-level tool cards. #7925 by-construction inserted the
    # proof-gated provenance bookkeeping between the cursor reset and the
    # sync, so this asserts the steps and their ORDER rather than one
    # contiguous run of source text (which would break on any inserted line
    # while proving nothing extra).
    _full_load = SESSIONS_JS[SESSIONS_JS.index("S.messages = _msgsToAssign;") :]
    _assign = _full_load.index("S.messages = _msgsToAssign;")
    _truncated = _full_load.index("_messagesTruncated = false;", _assign)
    _cursor = _full_load.index("_oldestIdx = 0;", _truncated)
    _sync = _full_load.index(
        "_syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);", _cursor
    )
    assert _assign < _truncated < _cursor < _sync, (
        "the full-document path must assign S.messages, reset the truncation "
        "flag and the cursor, and only then sync the session tool calls"
    )


def test_sessions_js_clears_session_tool_calls_when_messages_have_own_metadata():
    assert "const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;" in SESSIONS_JS
    assert "const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');" in SESSIONS_JS
    assert "windowOffset" not in SESSIONS_JS
    assert "copy.assistant_msg_idx=idx-offset;" not in SESSIONS_JS
    assert "S.toolCalls=sessionToolCalls.map(tc=>({...tc,done:true}));" in SESSIONS_JS
    assert "S.toolCalls=[];" in SESSIONS_JS
